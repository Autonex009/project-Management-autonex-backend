import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore

from app.db.database import SessionLocal
from app.services.hiring_sync_service import run_sync
from app.services.encord_sync_service import run_sync as run_encord_sync
from app.services.badge_award_jobs import (
    run_weekly_badge_job,
    run_monthly_badge_job,
    run_tenure_and_yearly_job,
)

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()

# Encord analytics are pulled once a day, at end of day. Hour is 24h local time
# (default 23:30). Upsert makes the pull idempotent if re-run.
ENCORD_SYNC_HOUR = int(os.getenv("ENCORD_SYNC_HOUR", "23"))
ENCORD_SYNC_MINUTE = int(os.getenv("ENCORD_SYNC_MINUTE", "30"))


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
        now = datetime.now()
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        result = run_encord_sync(db, start=start, end=now)
        logger.info(
            "[scheduler] Encord sync complete",
        )
        
        # --- NEW CODE ADDED: SLACK LEADERBOARD ---
        from app.models.encord_analytics import EncordDailyTimeSpent
        from app.api.analytics import is_autonex_email, _names_for, _hours
        from app.services.slack_service import send_channel_message
        from collections import defaultdict
        
        today = now.date()
        
        def get_top_users(db_session, start_date, end_date, limit=10):
            rows = db_session.query(EncordDailyTimeSpent).filter(
                EncordDailyTimeSpent.metric_date >= start_date,
                EncordDailyTimeSpent.metric_date <= end_date,
            ).all()
            user_seconds = defaultdict(int)
            for r in rows:
                if is_autonex_email(r.user_email):
                    user_seconds[r.user_email] += (r.time_spent_seconds or 0)
            name_by_email = _names_for(db_session, user_seconds.keys())
            top = [
                {"user_email": u, "employee_name": name_by_email.get(u), "hours": _hours(s)}
                for u, s in sorted(user_seconds.items(), key=lambda kv: kv[1], reverse=True)
            ]
            return top[:limit]
        
        # Monthly Top 10
        month_start = today.replace(day=1)
        monthly_users = get_top_users(db, month_start, today)
        
        # Weekly Top 10 (Last 7 Days)
        # Weekly Top 10 (Current Calendar Week: Monday to Today)
        week_start = today - timedelta(days=today.weekday()) # Monday
        weekly_users = get_top_users(db, week_start, today)
        
        # Daily Top 10 (Today)
        yesterday = today - timedelta(days=1)
        daily_users = get_top_users(db, yesterday, yesterday)
        
        def users_to_table_rows(users):
            def make_cell(text):
                return {"type": "raw_text", "text": str(text)}

            rows = [[make_cell("Rank"), make_cell("Employee Name"), make_cell("Hours")]]
            medals = ["🥇", "🥈", "🥉"]
            for index, user in enumerate(users):
                if index < 3:
                    rank = medals[index]
                else:
                    rank = str(index + 1)
                    
                name = user.get("employee_name") or user.get("user_email") or "Unknown"
                hours = f"{user.get('hours', 0)}h"
                
                rows.append([make_cell(rank), make_cell(name), make_cell(hours)])
            
            if len(rows) == 1:
                rows.append([make_cell("-"), make_cell("No data available"), make_cell("-")])
                
            return rows

        display_date = yesterday.strftime("%d %b")
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Autonex Leaderboard Update as of {display_date}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Monthly Top 10*"
                }
            },
            {
                "type": "table",
                "rows": users_to_table_rows(monthly_users),
                "column_settings": [
                    {"align": "center"},
                    {"align": "left", "is_wrapped": True},
                    {"align": "right"}
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Weekly Top 10 (This Week)*"
                }
            },
            {
                "type": "table",
                "rows": users_to_table_rows(weekly_users),
                "column_settings": [
                    {"align": "center"},
                    {"align": "left", "is_wrapped": True},
                    {"align": "right"}
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Daily Top 10 (Previous Day)*"
                }
            },
            {
                "type": "table",
                "rows": users_to_table_rows(daily_users),
                "column_settings": [
                    {"align": "center"},
                    {"align": "left", "is_wrapped": True},
                    {"align": "right"}
                ]
            }
        ]
        
        send_channel_message(
            channel="#encord-leaderboard",
            text="Autonex Leaderboard Update",
            blocks=blocks
        )
        logger.info("[scheduler] Successfully posted daily leaderboard to Slack.")
            
    except Exception as exc:
        logger.error("[scheduler] Encord sync failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    # Encord analytics pull once a day at end of day (ENCORD_SYNC_HOUR:MINUTE).
    # max_instances=1 + coalesce so a slow run never overlaps the next.
    if not _scheduler.get_job("encord_sync"):
        _scheduler.add_job(
            _scheduled_encord_sync,
            trigger="cron",
            hour=ENCORD_SYNC_HOUR,
            minute=ENCORD_SYNC_MINUTE,
            id="encord_sync",
            replace_existing=True,
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

    # Weekly badges – every Monday at 00:30
    if not _scheduler.get_job("weekly_badge_award"):
        _scheduler.add_job(
            run_weekly_badge_job,
            trigger="cron",
            day_of_week="mon",
            hour=0,
            minute=30,
            id="weekly_badge_award",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Monthly badges – 1st of every month at 01:00
    if not _scheduler.get_job("monthly_badge_award"):
        _scheduler.add_job(
            run_monthly_badge_job,
            trigger="cron",
            day=1,
            hour=1,
            minute=0,
            id="monthly_badge_award",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Tenure + Yearly milestones – every day at 02:00
    if not _scheduler.get_job("tenure_yearly_badges"):
        _scheduler.add_job(
            run_tenure_and_yearly_job,
            trigger="cron",
            hour=2,
            minute=0,
            id="tenure_yearly_badges",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if not _scheduler.running:
        _scheduler.start()

    logger.info(
        "[scheduler] Started — Encord sync every %s min; hiring sync %s",
        ENCORD_SYNC_MINUTE,
        "ENABLED (every 12h)" if os.getenv("ENABLE_HIRING_SYNC") else "disabled",
    )


def shutdown_scheduler() -> None:
    # guard against double-shutdown if called more than once
    if _scheduler.running:
        _scheduler.shutdown()
        logger.info("[scheduler] Shut down cleanly")
