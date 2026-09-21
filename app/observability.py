"""Prometheus metrics, wired for the long-running (Railway) deployment only.

Pull-based scraping assumes a process that stays up and accumulates counters
between scrapes. On Vercel every request may land on a fresh, short-lived
instance, so in-memory counters reset constantly and the exported numbers say
more about cold starts than about the service — for the cost of extra boot work
on every invocation. Staging observability therefore stays with Vercel's own
function logs, and this module no-ops there.

Alongside the usual request rate/latency/status series, this exports the two
saturation signals behind the pool-exhaustion incident described in main.py:
how much of the DB connection pool is checked out, and how much of the request
threadpool is borrowed. Those are what separate "the service is slow" from
"the service has run out of connections".
"""
import logging
import os
import secrets

import anyio.to_thread
from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.pool import QueuePool

from app.db.database import engine

logger = logging.getLogger(__name__)


def metrics_token_matches(auth_header: str) -> bool:
    """Whether a caller may scrape. Shared by the API and the worker.

    An unset METRICS_TOKEN means open, which keeps local dev frictionless; the
    startup warning is what flags that it must not stay that way in production.
    """
    token = os.getenv("METRICS_TOKEN")
    if not token:
        return True
    return secrets.compare_digest(auth_header, f"Bearer {token}")


def setup_metrics(app: FastAPI, registry: CollectorRegistry = REGISTRY) -> None:
    """Attach request instrumentation and serve /metrics.

    Call this after the other middleware is registered: Starlette runs the most
    recently added middleware outermost, so the timing then covers the whole
    request including compression.

    Collector names are unique per registry, so a second call against the
    default registry in one process raises. Pass an isolated CollectorRegistry
    to instrument a second app (tests do this); the default registry is the
    right one for the real app because prometheus_client's process and GC
    collectors are already registered there.
    """
    if os.environ.get("VERCEL"):
        return
    if os.getenv("METRICS_ENABLED", "true").lower() != "true":
        logger.info("[startup] metrics disabled via METRICS_ENABLED")
        return

    # Exact status codes rather than 2xx/5xx buckets: the pool-timeout handler
    # answers 503 specifically, and grouping would bury that inside 5xx — which
    # is the one distinction worth having during a saturation incident.
    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/health", "/metrics"],
        registry=registry,
    ).instrument(app)

    _register_saturation_gauges(registry)

    if not os.getenv("METRICS_TOKEN"):
        logger.warning(
            "METRICS_TOKEN is unset — /metrics is readable by anyone who can reach "
            "this service, including over its public domain."
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # Checked per request so the token can be rotated without a redeploy.
        # 404 rather than 401 so an unauthenticated caller cannot even confirm
        # this deployment exports metrics.
        if not metrics_token_matches(request.headers.get("authorization", "")):
            return Response(status_code=404)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    logger.info("[startup] metrics exposed at /metrics")


def _register_saturation_gauges(registry: CollectorRegistry) -> None:
    """Gauges read at scrape time via set_function, so nothing polls in the background."""
    pool = engine.pool
    if isinstance(pool, QueuePool):
        Gauge(
            "db_pool_connections_in_use",
            "DB connections currently checked out of the SQLAlchemy pool",
            registry=registry,
        ).set_function(pool.checkedout)
        Gauge(
            "db_pool_connections_idle",
            "DB connections sitting idle in the SQLAlchemy pool",
            registry=registry,
        ).set_function(pool.checkedin)
        # No public accessor for max_overflow; _max_overflow is set in
        # QueuePool.__init__ and stable across SQLAlchemy 2.x. Exported so a
        # saturation alert can divide by real capacity instead of hardcoding the
        # number a dashboard was built against.
        Gauge(
            "db_pool_connections_capacity",
            "Maximum DB connections this process may check out (pool_size + max_overflow)",
            registry=registry,
        ).set(pool.size() + pool._max_overflow)

    registry.register(_ThreadLimiterCollector())


class _ThreadLimiterCollector:
    """Exports request-threadpool usage, or nothing at all.

    The limiter lives in an anyio run-var, so it is only readable from inside
    the event loop serving the API. Any other caller — notably the worker, whose
    scrape endpoint shares prometheus_client's default registry — would make a
    plain Gauge callback raise and take down the entire scrape with it, so the
    two threadpool series are dropped there instead of failing everything.
    """

    def collect(self):
        try:
            limiter = anyio.to_thread.current_default_thread_limiter()
        except RuntimeError:
            return
        yield GaugeMetricFamily(
            "request_threadpool_threads_in_use",
            "Worker threads currently running sync endpoint handlers",
            value=limiter.borrowed_tokens,
        )
        yield GaugeMetricFamily(
            "request_threadpool_threads_limit",
            "Worker thread ceiling for sync endpoint handlers (WEB_CONCURRENCY_LIMIT)",
            value=limiter.total_tokens,
        )
