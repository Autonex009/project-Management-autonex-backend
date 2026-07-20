import logging
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
from apscheduler.triggers.cron import CronTrigger  # type: ignore

from app.db.database import SessionLocal
from app.services.hiring_sync_service import run_sync
from app.services.encord_sync_service import run_sync as run_encord_sync

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()


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
        result = run_encord_sync(db)
        logger.info(
            "[scheduler] Encord sync complete — projects=%s inserted=%s updated=%s errors=%s",
            result["projects"], result["inserted"], result["updated"], result["errors"],
        )
    except Exception as exc:
        logger.error("[scheduler] Encord sync failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    # Daily Encord analytics pull at 02:00 server time.
    if not _scheduler.get_job("encord_sync"):
        _scheduler.add_job(
            _scheduled_encord_sync,
            trigger=CronTrigger(hour=2, minute=0),
            id="encord_sync",
            replace_existing=True,
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
        "[scheduler] Started — Encord sync daily @02:00; hiring sync %s",
        "ENABLED (every 12h)" if os.getenv("ENABLE_HIRING_SYNC") else "disabled",
    )


def shutdown_scheduler() -> None:
    # guard against double-shutdown if called more than once
    if _scheduler.running:
        _scheduler.shutdown()
        logger.info("[scheduler] Shut down cleanly")
