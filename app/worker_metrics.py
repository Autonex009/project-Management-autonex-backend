"""Prometheus metrics for the arq worker process.

The worker runs as its own process with no HTTP server, so it serves its own
scrape endpoint on WORKER_METRICS_PORT rather than sharing the API's /metrics.

Per-job labels come from wrapping the task functions, not from arq's
on_job_start/on_job_end hooks: the ctx those receive carries only job_id,
job_try, enqueue_time and score — no function name and no success flag.

Encord syncs run for minutes, so the histograms below use explicit buckets.
prometheus_client's defaults stop at 10s, which would drop every real sync into
+Inf and leave the duration unreadable at exactly the moment it matters.
"""
import asyncio
import functools
import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from wsgiref.simple_server import make_server

from arq.constants import default_queue_name
from prometheus_client import Counter, Gauge, Histogram, make_wsgi_app
from prometheus_client.exposition import ThreadingWSGIServer, _SilentHandler

from app.observability import metrics_token_matches

logger = logging.getLogger(__name__)

_JOB_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1800, float("inf"))

# Labelled "task", not "job": Prometheus reserves "job" for the scrape job name
# and would quietly rename a colliding label to exported_job.
JOBS_TOTAL = Counter(
    "arq_jobs_total",
    "arq jobs that finished, by outcome",
    ["task", "outcome"],
)
JOB_DURATION = Histogram(
    "arq_job_duration_seconds",
    "Wall time spent executing an arq job",
    ["task"],
    buckets=_JOB_BUCKETS,
)
QUEUE_LATENCY = Histogram(
    "arq_job_queue_latency_seconds",
    "Time a job waited between being enqueued and being picked up",
    ["task"],
    buckets=_JOB_BUCKETS,
)
JOBS_IN_PROGRESS = Gauge(
    "arq_jobs_in_progress",
    "arq jobs currently executing",
    ["task"],
)
QUEUE_DEPTH = Gauge(
    "arq_queue_depth",
    "Jobs waiting in the arq queue",
)


def instrumented(task_fn):
    """Wrap an arq task so it reports queue wait, duration and outcome.

    functools.wraps matters beyond cosmetics here: arq registers a job under
    ``__qualname__`` and the API enqueues by that string name, so losing it
    would silently break dispatch rather than fail loudly.
    """

    @functools.wraps(task_fn)
    async def wrapper(ctx, *args, **kwargs):
        task = task_fn.__qualname__
        enqueue_time = ctx.get("enqueue_time")
        if enqueue_time is not None:
            QUEUE_LATENCY.labels(task).observe(
                (datetime.now(timezone.utc) - enqueue_time).total_seconds()
            )

        started = time.perf_counter()
        outcome = "failure"
        JOBS_IN_PROGRESS.labels(task).inc()
        try:
            result = await task_fn(ctx, *args, **kwargs)
            outcome = "success"
            return result
        finally:
            # finally, not except: a job killed by arq's timeout raises
            # CancelledError, and a sync that hangs until the timeout is the
            # failure most worth counting.
            JOBS_IN_PROGRESS.labels(task).dec()
            JOB_DURATION.labels(task).observe(time.perf_counter() - started)
            JOBS_TOTAL.labels(task, outcome).inc()

    return wrapper


async def watch_queue_depth(redis, interval: float = 15.0) -> None:
    """Keep the queue-depth gauge current.

    Job durations only produce samples once a job *starts*, so a worker that is
    wedged or outpaced looks identical to an idle one. Queue depth is what
    distinguishes them.
    """
    while True:
        try:
            QUEUE_DEPTH.set(await redis.zcard(default_queue_name))
        except Exception as e:
            # A transient Redis error must not kill the loop — that would
            # freeze the gauge at its last value and quietly mislead.
            logger.debug("Queue depth poll failed: %s", e)
        await asyncio.sleep(interval)


def start_metrics_server():
    """Serve the worker's metrics on a daemon thread. Returns the server.

    Railway's private network is IPv6, so set WORKER_METRICS_HOST=:: there for
    Prometheus to reach this; the IPv4 default keeps local scrapes working on
    Windows, where a v6 socket does not accept v4 clients.
    """
    host = os.getenv("WORKER_METRICS_HOST", "0.0.0.0")
    port = int(os.getenv("WORKER_METRICS_PORT", "9110"))

    server_class = ThreadingWSGIServer
    if ":" in host:
        class _IPv6Server(ThreadingWSGIServer):
            address_family = socket.AF_INET6

        server_class = _IPv6Server

    server = make_server(
        host, port, _gated(make_wsgi_app()), server_class, handler_class=_SilentHandler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("[worker] metrics exposed on %s:%s", host, server.server_port)
    return server


def _gated(app):
    """Same 404-on-bad-token policy the API applies to /metrics."""

    def gated(environ, start_response):
        if not metrics_token_matches(environ.get("HTTP_AUTHORIZATION", "")):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b""]
        return app(environ, start_response)

    return gated
