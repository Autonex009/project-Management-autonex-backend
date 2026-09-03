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


def _onboarding_day5_check() -> None:
    """Daily job: flip any pipeline record to 'day_5_pending' when 5+ days have
    elapsed since the candidate clicked 'Accept & Start'."""
    from app.models.onboarding_pipeline import OnboardingPipeline
    db = SessionLocal()
    try:
        cutoff = datetime.now() - timedelta(days=5)
        records = db.query(OnboardingPipeline).filter(
            OnboardingPipeline.status == "in_progress",
            OnboardingPipeline.started_at <= cutoff,
        ).all()
        for record in records:
            record.status = "day_5_pending"
        if records:
            db.commit()
            logger.info("[scheduler] Onboarding Day-5 check: escalated %d candidate(s)", len(records))
        else:
            logger.info("[scheduler] Onboarding Day-5 check: no candidates to escalate")
    except Exception as exc:
        logger.error("[scheduler] Onboarding Day-5 check failed: %s", exc)
    finally:
        db.close()

_scheduler = BackgroundScheduler()

# Encord analytics are pulled once a day, at end of day. Hour is 24h local time
# (default 23:30). Upsert makes the pull idempotent if re-run.
ENCORD_SYNC_HOUR = int(os.getenv("ENCORD_SYNC_HOUR", "23"))
ENCORD_SYNC_MINUTE = int(os.getenv("ENCORD_SYNC_MINUTE", "30"))

# Daily check-in reminders — mid-morning nudge to whoever hasn't checked in yet,
# then a later nudge to PMs/leads who still have unconfirmed check-ins. Weekdays
# only. Hours are 24h local time.
CHECKIN_REMINDER_HOUR = int(os.getenv("CHECKIN_REMINDER_HOUR", "10"))
CHECKIN_REMINDER_MINUTE = int(os.getenv("CHECKIN_REMINDER_MINUTE", "15"))
PM_CONFIRM_REMINDER_HOUR = int(os.getenv("PM_CONFIRM_REMINDER_HOUR", "12"))
PM_CONFIRM_REMINDER_MINUTE = int(os.getenv("PM_CONFIRM_REMINDER_MINUTE", "0"))


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


def _scheduled_checkin_reminders() -> None:
    """Nudge every active employee who hasn't checked in yet today."""
    db = SessionLocal()
    try:
        from datetime import date
        from app.models.employee import Employee
        from app.models.leave import Leave
        from app.models.daily_checkin import DailyCheckIn
        from app.services.slack_service import (
            try_get_or_cache_employee_slack_user_id,
            try_send_checkin_reminder_message,
        )

        today = date.today()

        checked_in_ids = {
            row[0] for row in db.query(DailyCheckIn.employee_id).filter(
                DailyCheckIn.checkin_date == today
            ).all()
        }
        on_leave_ids = {
            row[0] for row in db.query(Leave.employee_id).filter(
                Leave.status == "approved",
                Leave.start_date <= today,
                Leave.end_date >= today,
            ).all()
        }

        employees = db.query(Employee).filter(Employee.status == "active").all()
        sent = 0
        for employee in employees:
            if employee.id in checked_in_ids or employee.id in on_leave_ids:
                continue
            slack_id = try_get_or_cache_employee_slack_user_id(db, employee)
            if not slack_id:
                continue
            if try_send_checkin_reminder_message(
                employee_slack_user_id=slack_id, employee_name=employee.name
            ):
                sent += 1
        logger.info("[scheduler] Check-in reminders sent to %s employee(s)", sent)
    except Exception as exc:
        logger.error("[scheduler] Check-in reminder job failed: %s", exc)
    finally:
        db.close()


def _scheduled_pm_confirm_reminders() -> None:
    """Nudge every PM/lead who still has unconfirmed check-ins on their roster."""
    db = SessionLocal()
    try:
        from datetime import date
        from app.models.user import User
        from app.models.employee import Employee
        from app.models.daily_checkin import DailyCheckIn
        from app.api.checkins import _scoped_roster
        from app.services.slack_service import (
            try_get_or_cache_employee_slack_user_id,
            try_send_pm_confirm_reminder_message,
        )

        today = date.today()
        pm_users = (
            db.query(User)
            .filter(User.role.in_(["pm", "team_lead"]), User.employee_id.isnot(None))
            .all()
        )

        sent = 0
        for pm_user in pm_users:
            roster = _scoped_roster(db, pm_user)
            if not roster:
                continue
            employee_ids = list(roster.keys())
            pending = (
                db.query(DailyCheckIn)
                .filter(
                    DailyCheckIn.employee_id.in_(employee_ids),
                    DailyCheckIn.checkin_date == today,
                    DailyCheckIn.pm_confirmed_at.is_(None),
                )
                .count()
            )
            if pending == 0:
                continue
            pm_employee = db.query(Employee).filter(Employee.id == pm_user.employee_id).first()
            if not pm_employee:
                continue
            slack_id = try_get_or_cache_employee_slack_user_id(db, pm_employee)
            if not slack_id:
                continue
            if try_send_pm_confirm_reminder_message(
                pm_slack_user_id=slack_id, pm_name=pm_employee.name, pending_count=pending
            ):
                sent += 1
        logger.info("[scheduler] PM confirm reminders sent to %s manager(s)", sent)
    except Exception as exc:
        logger.error("[scheduler] PM confirm reminder job failed: %s", exc)
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

    # Weekly badges – every Monday at ENCORD_SYNC_HOUR:MINUTE (same as Encord)
    if not _scheduler.get_job("weekly_badge_award"):
        _scheduler.add_job(
            run_weekly_badge_job,
            trigger="cron",
            day_of_week="mon",
            hour=ENCORD_SYNC_HOUR,
            minute=ENCORD_SYNC_MINUTE,
            id="weekly_badge_award",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Monthly badges – 1st of every month at ENCORD_SYNC_HOUR:MINUTE (same as Encord)
    if not _scheduler.get_job("monthly_badge_award"):
        _scheduler.add_job(
            run_monthly_badge_job,
            trigger="cron",
            day=1,
            hour=ENCORD_SYNC_HOUR,
            minute=ENCORD_SYNC_MINUTE,
            id="monthly_badge_award",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Tenure + Yearly milestones – every day at ENCORD_SYNC_HOUR:MINUTE (same as Encord)
    if not _scheduler.get_job("tenure_yearly_badges"):
        _scheduler.add_job(
            run_tenure_and_yearly_job,
            trigger="cron",
            hour=ENCORD_SYNC_HOUR,
            minute=ENCORD_SYNC_MINUTE,
            id="tenure_yearly_badges",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Employee check-in reminder – weekdays at CHECKIN_REMINDER_HOUR:MINUTE.
    if not _scheduler.get_job("checkin_reminder"):
        _scheduler.add_job(
            _scheduled_checkin_reminders,
            trigger="cron",
            day_of_week="mon-fri",
            hour=CHECKIN_REMINDER_HOUR,
            minute=CHECKIN_REMINDER_MINUTE,
            id="checkin_reminder",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # PM/lead confirm-roster reminder – weekdays at PM_CONFIRM_REMINDER_HOUR:MINUTE,
    # after the employee reminder has had time to land.
    if not _scheduler.get_job("pm_confirm_reminder"):
        _scheduler.add_job(
            _scheduled_pm_confirm_reminders,
            trigger="cron",
            day_of_week="mon-fri",
            hour=PM_CONFIRM_REMINDER_HOUR,
            minute=PM_CONFIRM_REMINDER_MINUTE,
            id="pm_confirm_reminder",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if not _scheduler.running:
        _scheduler.start()

    # Onboarding pipeline Day-5 escalation — every day at 10:00 AM
    if not _scheduler.get_job("onboarding_day5_check"):
        _scheduler.add_job(
            _onboarding_day5_check,
            trigger="cron",
            hour=10,
            minute=0,
            id="onboarding_day5_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

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
