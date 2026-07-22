import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore

from app.db.database import SessionLocal
from app.services.hiring_sync_service import run_sync
from app.services.encord_sync_service import run_sync as run_encord_sync

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()

# How often to pull Encord analytics (minutes). Frequent + small window keeps
# the dashboard near-real-time; upsert makes re-pulling the same day idempotent.
ENCORD_SYNC_MINUTES = int(os.getenv("ENCORD_SYNC_MINUTES", "10"))


def _scheduled_hiring_sync() -> None:
    db = SessionLocal()
    try:
        result = run_sync(db)
        logger.info(
            "[scheduler] Hiring sync complete — imported=%s skipped=%s errors=%s",
            result["imported"], result["skipped"], result["errors"],
        )
    except Exception as exc:
        logger.error("[scheduler] Hiring sync failed: %s", exc)
    finally:
        db.close()


def _scheduled_encord_sync() -> None:
    db = SessionLocal()
    try:
        # Pull yesterday 00:00 → now every run: keeps today live and captures
        # late edits to yesterday. Upsert makes repeated same-day pulls idempotent.
        now = datetime.now()
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        result = run_encord_sync(db, start=start, end=now)
        logger.info(
            "[scheduler] Encord sync complete — projects=%s inserted=%s updated=%s errors=%s",
            result["projects"], result["inserted"], result["updated"], result["errors"],
        )
    except Exception as exc:
        logger.error("[scheduler] Encord sync failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    # Encord analytics pull every ENCORD_SYNC_MINUTES (default 10), running once on
    # startup. max_instances=1 + coalesce so slow runs never pile up.
    if not _scheduler.get_job("encord_sync"):
        _scheduler.add_job(
            _scheduled_encord_sync,
            trigger="interval",
            minutes=ENCORD_SYNC_MINUTES,
            id="encord_sync",
            replace_existing=True,
            next_run_time=datetime.now(),
            max_instances=1,
            coalesce=True,
        )

    # Legacy hiring-portal sync is opt-in (it used to be disabled entirely).
    if os.getenv("ENABLE_HIRING_SYNC") and not _scheduler.get_job("hiring_sync"):
        _scheduler.add_job(
            _scheduled_hiring_sync,
            trigger="interval",
            hours=12,
            id="hiring_sync",
            replace_existing=True,
            next_run_time=datetime.now(),
        )

    if not _scheduler.running:
        _scheduler.start()

    logger.info(
        "[scheduler] Started — Encord sync every %s min; hiring sync %s",
        ENCORD_SYNC_MINUTES,
        "ENABLED (every 12h)" if os.getenv("ENABLE_HIRING_SYNC") else "disabled",
    )


def shutdown_scheduler() -> None:
    # guard against double-shutdown if called more than once
    if _scheduler.running:
        _scheduler.shutdown()
        logger.info("[scheduler] Shut down cleanly")
