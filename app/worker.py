import os
import asyncio
import logging
from datetime import datetime
from typing import Optional
from arq.connections import RedisSettings

# Import your DB and sync service
from app.db.database import SessionLocal
from app.db.database import Base, engine
from app.models import project, allocation, leave, employee, parent_project, user, sub_project, guideline, side_project, skill, notification, wfh, signup_request, referral, payroll, performance_review, perf_eval, onboarding, company_settings, wifi_network, chat, encord_analytics, encord_activity, vendor
from app.services import encord_sync_service
from app.worker_metrics import instrumented, start_metrics_server, watch_queue_depth

logger = logging.getLogger(__name__)

# Railway provides the Redis connection string via the REDIS_URL environment variable
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379"

def perform_sync(start: Optional[datetime], end: Optional[datetime]):
    """Synchronous wrapper function to handle the SQLAlchemy session."""
    db = SessionLocal()
    try:
        encord_sync_service.run_sync(db, start=start, end=end)
    finally:
        db.close()

@instrumented
async def run_encord_sync(ctx, start: Optional[datetime], end: Optional[datetime]):
    """
    The async ARQ task. 
    It wraps the synchronous database call in a thread so ARQ's event loop isn't blocked.
    """
    logger.info(f"Worker picked up job. Start: {start} | End: {end}")
    
    # Offload the heavy synchronous work to a background thread
    await asyncio.to_thread(perform_sync, start, end)
    
    logger.info("Worker completed sync job successfully.")

def _perform_user_sync(log_id: str, employee_id: int, start: datetime, end: datetime):
    db = SessionLocal()
    try:
        return encord_sync_service.run_user_sync(db, log_id, employee_id, start, end)
    finally:
        db.close()

@instrumented
async def run_user_sync_task(ctx, log_id: str, employee_id: int, start: datetime, end: datetime):
    logger.info("Worker picked up user Encord sync — employee=%s start=%s end=%s", employee_id, start, end)
    result = await asyncio.to_thread(_perform_user_sync, log_id, employee_id, start, end)
    logger.info("Worker completed user Encord sync: %s", result)

    def _sync_user_badges():
        db = SessionLocal()
        try:
            from app.services.badge_award_jobs import _get_employee_hours
            from app.services.badge_service import award_weekly_badges, award_monthly_badges
            from datetime import timedelta
            
            s_date = start.date() if isinstance(start, datetime) else start
            e_date = end.date() if isinstance(end, datetime) else end
            
            # Sync weeks overlapping the selected period
            # Find the Monday on or before s_date
            curr_monday = s_date - timedelta(days=s_date.weekday())
            while curr_monday <= e_date:
                week_start = curr_monday
                week_end = curr_monday + timedelta(days=6)
                hours, emp_map = _get_employee_hours(db, week_start, week_end)
                award_weekly_badges(db, week_start, week_end, hours, emp_map, target_employee_id=employee_id)
                curr_monday += timedelta(weeks=1)
                
            # Sync months overlapping the selected period
            curr_month = s_date.replace(day=1)
            while curr_month <= e_date:
                month_start = curr_month
                next_month = curr_month.replace(day=28) + timedelta(days=4)
                month_end = next_month - timedelta(days=next_month.day)
                hours, emp_map = _get_employee_hours(db, month_start, month_end)
                award_monthly_badges(db, month_start, month_end, hours, emp_map, target_employee_id=employee_id)
                curr_month = next_month.replace(day=1)
                
            db.commit()
            logger.info("Targeted badge sync completed for employee=%s in period %s to %s", employee_id, s_date, e_date)
        except Exception as e:
            db.rollback()
            logger.error("Failed to sync badges for user %s: %s", employee_id, e)
        finally:
            db.close()
            
    await asyncio.to_thread(_sync_user_badges)
    return result


@instrumented
async def send_assessment_reminders_task(ctx, targets: list[dict]):
    """
    Asynchronous ARQ task for bulk assessment Slack reminders.
    Keeps 1.1s pacing and Slack 429 Retry-After backoff off FastAPI request threads.
    """
    from app.services.slack_service import try_send_assessment_reminder_dm, SlackRateLimitError

    sent_count = 0
    failed_count = 0
    target_list = targets or []
    for idx, item in enumerate(target_list):
        slack_uid = item.get("slack_user_id")
        emp_email = item.get("employee_email")
        emp_name = item.get("employee_name") or ""
        title = item.get("assessment_title") or "Assessment"
        deadline_str = item.get("deadline_str") or "As soon as possible"
        enr_id = item.get("enrollment_id")
        overdue = bool(item.get("overdue", False))
        try:
            sent = await asyncio.to_thread(
                try_send_assessment_reminder_dm,
                slack_uid,
                emp_name,
                title,
                deadline_str,
                enr_id,
                overdue=overdue,
                employee_email=emp_email,
            )
            if sent:
                sent_count += 1
            if sent and idx < len(target_list) - 1:
                await asyncio.sleep(1.1)
        except SlackRateLimitError as rate_exc:
            wait_secs = max(float(getattr(rate_exc, "retry_after_seconds", 2)), 1.0)
            logger.warning("[ARQ_REMINDERS] Slack rate limit on enrollment %s; sleeping %.1fs", enr_id, wait_secs)
            await asyncio.sleep(wait_secs)
            try:
                sent = await asyncio.to_thread(
                    try_send_assessment_reminder_dm,
                    slack_uid,
                    emp_name,
                    title,
                    deadline_str,
                    enr_id,
                    overdue=overdue,
                    employee_email=emp_email,
                )
                if sent:
                    sent_count += 1
            except Exception as retry_exc:
                failed_count += 1
                logger.warning("[ARQ_REMINDERS] Slack DM retry failed for enrollment %s: %s", enr_id, retry_exc)
        except Exception as exc:
            failed_count += 1
            logger.warning("[ARQ_REMINDERS] Slack DM dispatch failed for enrollment %s: %s", enr_id, exc)

    logger.info(
        "[ARQ_REMINDERS] Reminder batch completed: sent=%d failed=%d total=%d",
        sent_count,
        failed_count,
        len(target_list),
    )
    return {"sent_count": sent_count, "failed_count": failed_count}


async def startup(ctx):
    start_metrics_server()
    ctx["queue_depth_task"] = asyncio.create_task(watch_queue_depth(ctx["redis"]))


async def shutdown(ctx):
    task = ctx.get("queue_depth_task")
    if task:
        task.cancel()


# ARQ looks for this specific class name when starting up
class WorkerSettings:
    functions = [run_encord_sync, run_user_sync_task, send_assessment_reminders_task]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)

    # Optional: Prevent out-of-memory errors on Railway by limiting concurrent jobs
    max_jobs = 2
    on_startup = startup
    on_shutdown = shutdown