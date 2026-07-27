"""ARQ worker for background jobs.

Run alongside the API (separate process):

    arq worker.WorkerSettings

Requires a reachable Redis (REDIS_URL). The API enqueues jobs onto the same Redis
pool (see app/main.py lifespan); this process executes them.
"""
import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

from arq.connections import RedisSettings

from app.db.database import SessionLocal
# Import every model so SQLAlchemy mappers resolve when the worker runs standalone.
from app.models import (  # noqa: F401
    project, allocation, leave, employee, parent_project, user, sub_project,
    guideline, side_project, skill, notification, wfh, signup_request, referral,
    payroll, performance_review, perf_eval, onboarding, company_settings,
    wifi_network, chat, encord_analytics, encord_activity, vendor,
)
from app.services import encord_sync_service

logger = logging.getLogger(__name__)

# Railway/Redis connection string.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def _perform_sync(start: Optional[datetime], end: Optional[datetime]):
    """Synchronous DB work — runs the Encord pull with its own session."""
    db = SessionLocal()
    try:
        return encord_sync_service.run_sync(db, start=start, end=end)
    finally:
        db.close()


async def run_encord_sync(ctx, start: Optional[datetime] = None, end: Optional[datetime] = None):
    """ARQ task. Offloads the blocking DB/Encord work to a thread so the event loop
    isn't blocked. The returned dict becomes the job result."""
    logger.info("Worker picked up Encord sync — start=%s end=%s", start, end)
    result = await asyncio.to_thread(_perform_sync, start, end)
    logger.info("Worker completed Encord sync: %s", result)
    return result


class WorkerSettings:
    # ARQ looks for this class. `functions` names must match enqueue_job() calls.
    functions = [run_encord_sync]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 2
