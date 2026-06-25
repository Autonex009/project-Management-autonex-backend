import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore

from app.db.database import SessionLocal
from app.services.hiring_sync_service import run_sync

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


def start_scheduler() -> None:
    # guard against double-registration if called more than once
    if not _scheduler.get_job("hiring_sync"):
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

    logger.info("[scheduler] Started — hiring sync scheduled every 12 hours, running immediately in background")


def shutdown_scheduler() -> None:
    # guard against double-shutdown if called more than once
    if _scheduler.running:
        _scheduler.shutdown()
        logger.info("[scheduler] Shut down cleanly")
