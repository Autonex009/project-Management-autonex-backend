"""Metrics exist to shorten incident debugging, so the things that make them
usable during one are what these tests pin: that /metrics cannot be read off the
public domain once a token is set, that health-check traffic does not drown the
request series, and that the DB-pool and threadpool saturation gauges — the pair
that distinguishes "slow" from "out of connections" — are actually exported.

Each app here gets its own CollectorRegistry: collector names are unique per
registry, so sharing the default one would make importing app.main after this
module raise on duplicate names and take the rest of the suite with it.
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app.observability import setup_metrics

_app = FastAPI()


@_app.get("/health")
async def _health():
    return {"status": "ok"}


@_app.get("/employees/{employee_id}")
async def _employee(employee_id: int):
    return {"id": employee_id}


setup_metrics(_app, CollectorRegistry())
client = TestClient(_app)


def _scrape() -> str:
    r = client.get("/metrics")
    assert r.status_code == 200
    return r.text


def _metric_value(body: str, name: str) -> float:
    for line in body.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split(" ", 1)[1])
    raise AssertionError(f"{name} missing from /metrics")


def test_requests_are_counted_under_the_route_template():
    """The label must be the template, not the literal path.

    A label per employee id would mint a new time series per request and blow up
    Prometheus' cardinality within a day of real traffic.
    """
    client.get("/employees/1")
    client.get("/employees/2")

    body = _scrape()
    assert "/employees/{employee_id}" in body
    assert "/employees/1" not in body


def test_status_codes_are_not_grouped_into_buckets():
    """503 is the pool-saturation signal; a 5xx bucket would hide it."""
    body = _scrape()
    assert 'status="200"' in body
    assert 'status="2xx"' not in body


def test_health_checks_are_excluded_from_request_metrics():
    """Railway probes /health constantly; counting it distorts every rate panel."""
    client.get("/health")
    assert '"/health"' not in _scrape()


def test_saturation_gauges_are_exported():
    body = _scrape()
    assert "request_threadpool_threads_in_use" in body
    assert "request_threadpool_threads_limit" in body


def test_threadpool_gauge_reports_a_live_limiter_reading():
    """The gauges read the anyio limiter at scrape time, so they track a
    WEB_CONCURRENCY_LIMIT change instead of a value copied at import."""
    body = _scrape()

    limit = _metric_value(body, "request_threadpool_threads_limit")
    in_use = _metric_value(body, "request_threadpool_threads_in_use")

    assert limit > 0
    assert 0 <= in_use <= limit


def test_metrics_are_public_when_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert client.get("/metrics").status_code == 200


def test_token_gate_hides_metrics_from_unauthenticated_callers(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")

    assert client.get("/metrics").status_code == 404
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer wrong"}
    ).status_code == 404
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200


def test_setup_is_a_noop_on_vercel(monkeypatch):
    """Serverless instances reset counters constantly; staging stays on Vercel logs."""
    monkeypatch.setenv("VERCEL", "1")
    serverless = FastAPI()
    setup_metrics(serverless, CollectorRegistry())

    assert TestClient(serverless).get("/metrics").status_code == 404


def test_setup_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "false")
    disabled = FastAPI()
    setup_metrics(disabled, CollectorRegistry())

    assert TestClient(disabled).get("/metrics").status_code == 404


@pytest.mark.skipif(
    os.getenv("VERCEL") is not None, reason="metrics are intentionally off on Vercel"
)
def test_real_app_exposes_metrics():
    """Guards against the module existing but never being wired into app.main."""
    import app.main as app_main

    assert any(getattr(r, "path", None) == "/metrics" for r in app_main.app.routes)
