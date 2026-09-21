"""The worker is the half of the system with no request to watch, so when an
Encord sync wedges or starts failing there is nothing to notice it but these
metrics. What is pinned here is what an incident depends on: the job names arq
dispatches by survive instrumentation, failures are counted as failures, a
wedged job does not leave the in-progress gauge stuck, and queue depth keeps
reporting through a Redis blip.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from arq.worker import func
from prometheus_client import REGISTRY

import app.worker as live_worker
from app.worker_metrics import instrumented, start_metrics_server, watch_queue_depth


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# Defined at module level like real arq tasks: the metric label is __qualname__,
# which for a function nested inside a test would carry the "<locals>" path.
@instrumented
async def sample_job(ctx):
    return "done"


@instrumented
async def failing_job(ctx):
    raise RuntimeError("encord exploded")


@instrumented
async def hanging_job(ctx):
    await asyncio.sleep(60)


@instrumented
async def waited_job(ctx):
    return None


def test_instrumented_tasks_keep_the_names_arq_dispatches_by():
    """The API enqueues by string name, and arq registers jobs under
    __qualname__. If the decorator dropped it, every enqueued job would fail to
    resolve at runtime — with nothing failing at import to warn anyone."""
    registered = {func(f).name for f in live_worker.WorkerSettings.functions}

    assert registered == {"run_encord_sync", "run_user_sync_task"}


def test_the_live_worker_is_the_instrumented_one():
    """Guards against instrumenting the stale duplicate: app.worker is what the
    Railway service runs, and root worker.py is a diverged copy."""
    assert live_worker.WorkerSettings.on_startup is not None
    assert all(
        hasattr(f, "__wrapped__") for f in live_worker.WorkerSettings.functions
    ), "a worker task is missing @instrumented"


@pytest.mark.anyio
async def test_successful_job_is_counted_and_timed():
    before = _sample("arq_jobs_total", task="sample_job", outcome="success")
    assert await sample_job({}) == "done"

    assert _sample("arq_jobs_total", task="sample_job", outcome="success") == before + 1
    assert _sample("arq_job_duration_seconds_count", task="sample_job") == 1.0


@pytest.mark.anyio
async def test_failing_job_is_counted_as_failure_and_still_raises():
    """Instrumentation must not swallow the error arq needs for its retry."""
    with pytest.raises(RuntimeError, match="encord exploded"):
        await failing_job({})

    assert _sample("arq_jobs_total", task="failing_job", outcome="failure") == 1.0
    assert _sample("arq_jobs_total", task="failing_job", outcome="success") == 0.0


@pytest.mark.anyio
async def test_cancelled_job_releases_the_in_progress_gauge():
    """arq kills an over-running job with CancelledError. A gauge left at 1
    would read as 'still running' forever and hide the next real run."""
    task = asyncio.create_task(hanging_job({}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _sample("arq_jobs_in_progress", task="hanging_job") == 0.0
    assert _sample("arq_jobs_total", task="hanging_job", outcome="failure") == 1.0


@pytest.mark.anyio
async def test_queue_wait_is_measured_from_the_enqueue_timestamp():
    enqueued = datetime.now(timezone.utc) - timedelta(seconds=30)
    await waited_job({"enqueue_time": enqueued})

    assert _sample("arq_job_queue_latency_seconds_count", task="waited_job") == 1.0
    total = _sample("arq_job_queue_latency_seconds_sum", task="waited_job")
    assert 29 <= total <= 40


@pytest.mark.anyio
async def test_queue_depth_poll_survives_a_redis_error():
    class FlakyRedis:
        def __init__(self):
            self.calls = 0

        async def zcard(self, _key):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("redis blip")
            return 7

    redis = FlakyRedis()
    task = asyncio.create_task(watch_queue_depth(redis, interval=0.01))
    while redis.calls < 2:
        await asyncio.sleep(0.01)
    task.cancel()

    assert _sample("arq_queue_depth") == 7.0


def test_metrics_server_applies_the_same_token_gate(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "worker-secret")
    monkeypatch.setenv("WORKER_METRICS_PORT", "0")  # let the OS pick a free port
    server = start_metrics_server()
    url = f"http://127.0.0.1:{server.server_port}/"

    try:
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(url)
        assert unauthorized.value.code == 404

        authorized = urlopen(
            Request(url, headers={"Authorization": "Bearer worker-secret"})
        )
        assert authorized.status == 200
        assert b"arq_jobs_total" in authorized.read()
    finally:
        server.shutdown()
        server.server_close()
