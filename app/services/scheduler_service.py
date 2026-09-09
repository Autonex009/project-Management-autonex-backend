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
        today = datetime.now().date()
        records = db.query(OnboardingPipeline).filter(
            OnboardingPipeline.status == "in_progress",
            OnboardingPipeline.expected_eval_date <= today,
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

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from app.db.database import engine
from zoneinfo import ZoneInfo

jobstores = {
    'default': SQLAlchemyJobStore(engine=engine)
}
job_defaults = {
    'misfire_grace_time': 24 * 60 * 60  # Catch up on jobs missed by up to 24 hours
}

_scheduler = BackgroundScheduler(
    jobstores=jobstores,
    job_defaults=job_defaults,
    timezone="Asia/Kolkata"
)

# Encord analytics are pulled once a day, at end of day. Hour is 24h local time
# (default 23:30). Upsert makes the pull idempotent if re-run.
ENCORD_SYNC_HOUR = int(os.getenv("ENCORD_SYNC_HOUR", "23"))
ENCORD_SYNC_MINUTE = int(os.getenv("ENCORD_SYNC_MINUTE", "30"))


# Daily check-in reminders — mid-morning nudge to whoever hasn't checked in yet,
# then a later nudge to PMs/leads who still have unconfirmed check-ins. Weekdays
# only. Hours are 24h local time.
CHECKIN_REMINDER_HOUR = int(os.getenv("CHECKIN_REMINDER_HOUR", "10"))
CHECKIN_REMINDER_MINUTE = int(os.getenv("CHECKIN_REMINDER_MINUTE", "0"))
PM_CONFIRM_REMINDER_HOUR = int(os.getenv("PM_CONFIRM_REMINDER_HOUR", "12"))
PM_CONFIRM_REMINDER_MINUTE = int(os.getenv("PM_CONFIRM_REMINDER_MINUTE", "0"))
LATE_WARNING_HOUR = int(os.getenv("LATE_WARNING_HOUR", "10"))
LATE_WARNING_MINUTE = int(os.getenv("LATE_WARNING_MINUTE", "45"))
ADMIN_REPORT_HOUR = int(os.getenv("ADMIN_REPORT_HOUR", "11"))
ADMIN_REPORT_MINUTE = int(os.getenv("ADMIN_REPORT_MINUTE", "10"))
LATE_CHECKIN_CHANNEL_ID = os.getenv("LATE_CHECKIN_CHANNEL_ID", "C0BV91K4PD5")

# Onboarding checks
ONBOARDING_DAY5_CHECK_HOUR = int(os.getenv("ONBOARDING_DAY5_CHECK_HOUR", "10"))
ONBOARDING_DAY5_CHECK_MINUTE = int(os.getenv("ONBOARDING_DAY5_CHECK_MINUTE", "0"))

# Database maintenance jobs
REFRESH_MATRIX_HOUR = int(os.getenv("REFRESH_MATRIX_HOUR", "0"))
REFRESH_MATRIX_MINUTE = int(os.getenv("REFRESH_MATRIX_MINUTE", "5"))

CREATE_PARTITION_HOUR = int(os.getenv("CREATE_PARTITION_HOUR", "2"))
CREATE_PARTITION_MINUTE = int(os.getenv("CREATE_PARTITION_MINUTE", "0"))

# Hiring sync interval
HIRING_SYNC_INTERVAL_HOURS = int(os.getenv("HIRING_SYNC_INTERVAL_HOURS", "12"))

# Checkin Lunch
LUNCH_REPORT_HOUR = int(os.getenv("LUNCH_REPORT_HOUR", "11"))
LUNCH_REPORT_MINUTE = int(os.getenv("LUNCH_REPORT_MINUTE", "0"))
LUNCH_REPORT_PRIMARY_EMAIL = os.getenv("LUNCH_REPORT_PRIMARY_EMAIL", "jadhavashish061@gmail.com")
LUNCH_REPORT_FALLBACK_EMAIL = os.getenv("LUNCH_REPORT_FALLBACK_EMAIL", "kisanjena40@gmail.com")


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
        from app.utils.business_time import today_ist
        from app.constants.leave_types import is_fixed_holiday, is_weekend
        from app.models.employee import Employee
        from app.models.leave import Leave
        from app.models.daily_checkin import DailyCheckIn
        from app.services.slack_service import (
            try_get_or_cache_employee_slack_user_id,
            try_send_checkin_reminder_message,
        )
        from sqlalchemy import not_
        import time

        today = today_ist()

        if is_weekend(today) or is_fixed_holiday(today):
            logger.info("[scheduler] Skipping check-in reminders because today is a weekend or holiday.")
            return

        checked_in_query = db.query(DailyCheckIn.employee_id).filter(
            DailyCheckIn.checkin_date == today
        )
        on_leave_query = db.query(Leave.employee_id).filter(
            Leave.status == "approved",
            Leave.start_date <= today,
            Leave.end_date >= today,
        )

        employees = db.query(Employee).filter(
            Employee.status == "active",
            not_(Employee.id.in_(checked_in_query)),
            not_(Employee.id.in_(on_leave_query))
        ).all()

        sent = 0
        for employee in employees:
            slack_id = try_get_or_cache_employee_slack_user_id(db, employee)
            if not slack_id:
                continue
            
            try:
                if try_send_checkin_reminder_message(
                    employee_slack_user_id=slack_id, employee_name=employee.name
                ):
                    sent += 1
                    time.sleep(1.5)  # Base sleep to avoid rate limits
            except Exception as exc:
                if "429" in str(exc) or "rate" in str(exc).lower():
                    logger.warning("[scheduler] Rate limit hit sending reminder to %s, sleeping...", employee.email)
                    time.sleep(10)
                else:
                    logger.error("[scheduler] Failed sending checkin reminder to %s: %s", employee.email, exc)

        logger.info("[scheduler] Check-in reminders sent to %s employee(s)", sent)
    except Exception as exc:
        logger.error("[scheduler] Check-in reminder job failed: %s", exc)
    finally:
        db.close()


def _refresh_materialized_view() -> None:
    """Refresh the historical checkins materialized view on the 1st of the month."""
    db = SessionLocal()
    try:
        from sqlalchemy import text
        # CONCURRENTLY requires a unique index, which we added in the migration
        db.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY historical_checkins_matrix"))
        db.commit()
        logger.info("[scheduler] Refreshed historical_checkins_matrix materialized view")
    except Exception as exc:
        logger.error("[scheduler] Failed to refresh materialized view: %s", exc)
    finally:
        db.close()


def _create_next_month_partition() -> None:
    """Create the partition for the upcoming month for the daily_checkins table."""
    db = SessionLocal()
    try:
        from sqlalchemy import text
        
        now = datetime.now()
        # Calculate next month and year
        next_month_val = now.month + 1 if now.month < 12 else 1
        next_year_val = now.year if now.month < 12 else now.year + 1
        
        table_name = f"daily_checkins_{next_year_val}_{next_month_val:02d}"
        start_date = f"{next_year_val}-{next_month_val:02d}-01"
        
        # Calculate end_date (1st of the month after next)
        after_next_month_val = next_month_val + 1 if next_month_val < 12 else 1
        after_next_year_val = next_year_val if next_month_val < 12 else next_year_val + 1
        end_date = f"{after_next_year_val}-{after_next_month_val:02d}-01"
        
        sql = f"""
            CREATE TABLE IF NOT EXISTS {table_name} 
            PARTITION OF daily_checkins 
            FOR VALUES FROM ('{start_date}') TO ('{end_date}')
        """
        db.execute(text(sql))
        db.commit()
        logger.info("[scheduler] Created next month partition: %s", table_name)
    except Exception as exc:
        logger.error("[scheduler] Failed to create partition: %s", exc)
    finally:
        db.close()


def _scheduled_pm_confirm_reminders() -> None:
    """Nudge every PM/lead who still has unconfirmed check-ins on their roster."""
    db = SessionLocal()
    try:
        from app.utils.business_time import today_ist
        from app.constants.leave_types import is_fixed_holiday, is_weekend
        from app.models.user import User
        from app.models.employee import Employee
        from app.models.daily_checkin import DailyCheckIn
        from app.models.allocation import Allocation
        from app.api.checkins import _get_scoped_project_ids
        from app.services.slack_service import (
            try_get_or_cache_employee_slack_user_id,
            try_send_pm_confirm_reminder_message,
        )
        import time

        today = today_ist()

        if is_weekend(today) or is_fixed_holiday(today):
            logger.info("[scheduler] Skipping PM confirm reminders because today is a weekend or holiday.")
            return

        pm_users = (
            db.query(User)
            .filter(User.role.in_(["pm", "team_lead"]), User.employee_id.isnot(None))
            .all()
        )

        # Pre-fetch all check-ins for today to avoid querying inside the loop
        all_today_checkins = db.query(
            DailyCheckIn.employee_id, 
            DailyCheckIn.project_ids, 
            DailyCheckIn.pm_confirmed_at
        ).filter(
            DailyCheckIn.checkin_date == today
        ).all()
        
        # Pre-fetch all active allocations
        all_active_allocations = db.query(
            Allocation.employee_id, 
            Allocation.sub_project_id
        ).filter(
            Allocation.is_active == True
        ).all()

        sent = 0
        for pm_user in pm_users:
            scoped_project_ids = _get_scoped_project_ids(db, pm_user)
            if not scoped_project_ids:
                continue
                
            allocated_emp_ids = {
                a.employee_id for a in all_active_allocations 
                if a.employee_id and a.sub_project_id in scoped_project_ids
            }
            
            checked_in_emp_ids = set()
            for c in all_today_checkins:
                if set(c.project_ids or []).intersection(scoped_project_ids):
                    checked_in_emp_ids.add(c.employee_id)
                    
            visible_emp_ids = allocated_emp_ids.union(checked_in_emp_ids)
            if not visible_emp_ids:
                continue
                
            pending = sum(
                1 for c in all_today_checkins 
                if c.employee_id in visible_emp_ids and c.pm_confirmed_at is None
            )
            
            if pending == 0:
                continue

            pm_employee = db.query(Employee).filter(Employee.id == pm_user.employee_id).first()
            if not pm_employee:
                continue
            slack_id = try_get_or_cache_employee_slack_user_id(db, pm_employee)
            if not slack_id:
                continue
            
            try:
                if try_send_pm_confirm_reminder_message(
                    pm_slack_user_id=slack_id, pm_name=pm_employee.name, pending_count=pending
                ):
                    sent += 1
                    time.sleep(1.5)  # Avoid Slack API rate limit
            except Exception as exc:
                if "429" in str(exc) or "rate" in str(exc).lower():
                    logger.warning("[scheduler] Rate limit hit sending PM reminder, sleeping...")
                    time.sleep(10)
                else:
                    logger.error("[scheduler] Failed sending PM reminder: %s", exc)
                    
        logger.info("[scheduler] PM confirm reminders sent to %s manager(s)", sent)
    except Exception as exc:
        logger.error("[scheduler] PM confirm reminder job failed: %s", exc)
    finally:
        db.close()


def _scheduled_late_warning() -> None:
    """Nudge active employees who still haven't checked in by the late warning time."""
    db = SessionLocal()
    try:
        from app.utils.business_time import today_ist
        from app.constants.leave_types import is_fixed_holiday, is_weekend
        from app.models.employee import Employee
        from app.models.leave import Leave
        from app.models.daily_checkin import DailyCheckIn
        from app.services.slack_service import (
            try_get_or_cache_employee_slack_user_id,
            try_send_late_warning_message,
        )
        from sqlalchemy import not_
        import time

        today = today_ist()

        if is_weekend(today) or is_fixed_holiday(today):
            logger.info("[scheduler] Skipping late warning because today is a weekend or holiday.")
            return

        checked_in_query = db.query(DailyCheckIn.employee_id).filter(
            DailyCheckIn.checkin_date == today
        )
        on_leave_query = db.query(Leave.employee_id).filter(
            Leave.status == "approved",
            Leave.start_date <= today,
            Leave.end_date >= today,
        )

        employees = db.query(Employee).filter(
            Employee.status == "active",
            not_(Employee.id.in_(checked_in_query)),
            not_(Employee.id.in_(on_leave_query))
        ).all()

        sent = 0
        for employee in employees:
            slack_id = try_get_or_cache_employee_slack_user_id(db, employee)
            if not slack_id:
                continue
            
            try:
                if try_send_late_warning_message(
                    employee_slack_user_id=slack_id, employee_name=employee.name
                ):
                    sent += 1
                    time.sleep(1.5)
            except Exception as exc:
                if "429" in str(exc) or "rate" in str(exc).lower():
                    time.sleep(10)
                else:
                    logger.error("[scheduler] Failed sending late warning to %s: %s", employee.email, exc)

        logger.info("[scheduler] Late check-in warnings sent to %s employee(s)", sent)
    except Exception as exc:
        logger.error("[scheduler] Late warning job failed: %s", exc)
    finally:
        db.close()


def _scheduled_admin_report() -> None:
    """Compile and send a list of all active employees who missed the final check-in deadline."""
    db = SessionLocal()
    try:
        from app.utils.business_time import today_ist
        from app.constants.leave_types import is_fixed_holiday, is_weekend
        from app.models.employee import Employee
        from app.models.leave import Leave
        from app.models.daily_checkin import DailyCheckIn
        from app.services.slack_service import try_send_admin_late_list
        from sqlalchemy import not_
        from datetime import datetime, time as dtime, timezone
        from zoneinfo import ZoneInfo

        today = today_ist()

        if is_weekend(today) or is_fixed_holiday(today):
            logger.info("[scheduler] Skipping admin report because today is a weekend or holiday.")
            return

        checked_in_query = db.query(DailyCheckIn.employee_id).filter(
            DailyCheckIn.checkin_date == today
        )
        on_leave_query = db.query(Leave.employee_id).filter(
            Leave.status == "approved",
            Leave.start_date <= today,
            Leave.end_date >= today,
        )

        # 1. Pending: Did not check in and not on leave
        pending_employees = db.query(Employee).filter(
            Employee.status == "active",
            not_(Employee.id.in_(checked_in_query)),
            not_(Employee.id.in_(on_leave_query))
        ).all()
        pending_list = [(emp.name, emp.email) for emp in pending_employees]

        # 2. Late Check-in: Checked in today, but after 11:00 AM IST
        IST = ZoneInfo("Asia/Kolkata")
        late_threshold_ist = datetime.combine(today, dtime(11, 0), tzinfo=IST)
        late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)

        late_checkins = db.query(Employee, DailyCheckIn.checked_in_at).join(
            DailyCheckIn, Employee.id == DailyCheckIn.employee_id
        ).filter(
            Employee.status == "active",
            DailyCheckIn.checkin_date == today,
            DailyCheckIn.checked_in_at > late_threshold_utc
        ).all()
        late_checkin_list = [
            (emp.name, emp.email, checkin_time.astimezone(IST).strftime("%I:%M %p") if checkin_time else "—")
            for emp, checkin_time in late_checkins
        ]

        if late_checkin_list or pending_list:
            try_send_admin_late_list(
                channel_id=LATE_CHECKIN_CHANNEL_ID,
                late_checkins=late_checkin_list,
                pending_checkins=pending_list
            )
            logger.info("[scheduler] Sent admin report (Late: %s, Pending: %s)", len(late_checkin_list), len(pending_list))
        else:
            logger.info("[scheduler] No late or pending check-ins today! Admin report skipped.")

    except Exception as exc:
        logger.error("[scheduler] Admin report job failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    # Encord analytics pull once a day at end of day (ENCORD_SYNC_HOUR:MINUTE).
    # max_instances=1 + coalesce so a slow run never overlaps the next.
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
    if os.getenv("ENABLE_HIRING_SYNC"):
        _scheduler.add_job(
            _scheduled_hiring_sync,
            trigger="interval",
            hours=HIRING_SYNC_INTERVAL_HOURS,
            id="hiring_sync",
            replace_existing=True,
            next_run_time=datetime.now(),
        )

    # Weekly badges – every Monday at ENCORD_SYNC_HOUR:MINUTE (same as Encord)
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

    _scheduler.add_job(
        _scheduled_late_warning,
        trigger="cron",
        day_of_week="mon-fri",
        hour=LATE_WARNING_HOUR,
        minute=LATE_WARNING_MINUTE,
        id="late_warning_reminder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.add_job(
        _scheduled_admin_report,
        trigger="cron",
        day_of_week="mon-fri",
        hour=ADMIN_REPORT_HOUR,
        minute=ADMIN_REPORT_MINUTE,
        id="late_admin_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    if not _scheduler.running:
        _scheduler.start()

    # Onboarding pipeline Day-5 escalation — every day at 10:00 AM
    _scheduler.add_job(
        _onboarding_day5_check,
        trigger="cron",
        hour=ONBOARDING_DAY5_CHECK_HOUR,
        minute=ONBOARDING_DAY5_CHECK_MINUTE,
        id="onboarding_day5_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Refresh historical checkins materialized view - 1st of every month at 00:05 AM IST
    _scheduler.add_job(
        _refresh_materialized_view,
        trigger="cron",
        day=1,
        hour=REFRESH_MATRIX_HOUR,
        minute=REFRESH_MATRIX_MINUTE,
        id="refresh_checkins_matrix",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Create next month's partition for daily_checkins - 25th of every month
    _scheduler.add_job(
        _create_next_month_partition,
        trigger="cron",
        day=25,
        hour=CREATE_PARTITION_HOUR,
        minute=CREATE_PARTITION_MINUTE,
        id="create_checkins_partition",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

        # Daily Lunch Order PDF report – weekdays at 11:00 AM IST
    _scheduler.add_job(
        _scheduled_lunch_report,
        trigger="cron",
        day_of_week="mon-fri",
        hour=LUNCH_REPORT_HOUR,
        minute=LUNCH_REPORT_MINUTE,
        id="daily_lunch_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "[scheduler] Started — Encord sync every %s min; hiring sync %s",
        ENCORD_SYNC_MINUTE,
        "ENABLED (every 12h)" if os.getenv("ENABLE_HIRING_SYNC") else "disabled",
    )

def _scheduled_lunch_report() -> None:
    """Generate and email the daily lunch order PDF (WFO + tiffin/canteen)."""
    db = SessionLocal()
    try:
        from app.utils.business_time import today_ist
        from app.constants.leave_types import is_fixed_holiday, is_weekend
        from app.services.lunch_report_service import generate_and_send_lunch_report

        today = today_ist()

        if is_weekend(today) or is_fixed_holiday(today):
            logger.info("[scheduler] Skipping lunch report because today is a weekend or holiday.")
            return

        # Send to both primary + fallback
        emails = [LUNCH_REPORT_PRIMARY_EMAIL, LUNCH_REPORT_FALLBACK_EMAIL]
        # Remove duplicates if both are the same
        emails = list(dict.fromkeys(emails))

        success = generate_and_send_lunch_report(db, to_emails=emails)

        if success:
            logger.info("[scheduler] Lunch report emailed successfully to %s", emails)
        else:
            logger.error("[scheduler] Lunch report email failed for one or more recipients")
    except Exception as exc:
        logger.exception("[scheduler] Lunch report job crashed: %s", exc)
    finally:
        db.close()


def shutdown_scheduler() -> None:
    # guard against double-shutdown if called more than once
    if _scheduler.running:
        _scheduler.shutdown()
        logger.info("[scheduler] Shut down cleanly")
