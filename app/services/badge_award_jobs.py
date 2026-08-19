"""Scheduled jobs that calculate hours and award all types of badges."""

import logging
from datetime import date
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.services.badge_service import (
    award_weekly_badges,
    award_monthly_badges,
    award_tenure_badges,
    award_yearly_milestones,
    expire_due_badges,
    get_previous_week_range,
    get_previous_month_range,
)

logger = logging.getLogger(__name__)


def _get_employee_hours(db: Session, start: date, end: date) -> dict[int, float]:
    """
    Calculate real hours using EncordDailyTimeSpent.
    Returns {employee_id: total_hours} for the given date range.
    """
    from sqlalchemy import func
    from app.models.employee import Employee
    from app.models.encord_analytics import EncordDailyTimeSpent

    employees = db.query(Employee).all()
    emp_map = {}
    for emp in employees:
        if emp.encord_id:
            emp_map[emp.encord_id] = emp.id
        if emp.email:
            emp_map[emp.email] = emp.id

    results = (
        db.query(
            EncordDailyTimeSpent.user_email,
            func.sum(EncordDailyTimeSpent.time_spent_seconds).label("total_seconds")
        )
        .filter(
            EncordDailyTimeSpent.metric_date >= start,
            EncordDailyTimeSpent.metric_date <= end,
        )
        .group_by(EncordDailyTimeSpent.user_email)
        .all()
    )

    hours_by_emp = {}
    for user_email, total_seconds in results:
        emp_id = emp_map.get(user_email)
        if emp_id:
            hours_by_emp[emp_id] = hours_by_emp.get(emp_id, 0.0) + (total_seconds / 3600.0)

    return hours_by_emp


def run_weekly_badge_job() -> dict:
    """Run every Monday – process previous week."""
    db = SessionLocal()
    try:
        expire_due_badges(db)

        week_start, week_end = get_previous_week_range()
        hours = _get_employee_hours(db, week_start, week_end)

        count = award_weekly_badges(db, week_start, week_end, hours)
        db.commit()

        logger.info("[badges] Weekly job done – awarded=%s (%s → %s)", count, week_start, week_end)
        return {"awarded": count, "period": f"{week_start} → {week_end}"}
    except Exception as exc:
        db.rollback()
        logger.error("[badges] Weekly job failed: %s", exc)
        raise
    finally:
        db.close()


def run_monthly_badge_job() -> dict:
    """Run on the 1st of every month – process previous month."""
    db = SessionLocal()
    try:
        expire_due_badges(db)

        month_start, month_end = get_previous_month_range()
        hours = _get_employee_hours(db, month_start, month_end)

        count = award_monthly_badges(db, month_start, month_end, hours)
        db.commit()

        logger.info("[badges] Monthly job done – awarded=%s (%s → %s)", count, month_start, month_end)
        return {"awarded": count, "period": f"{month_start} → {month_end}"}
    except Exception as exc:
        db.rollback()
        logger.error("[badges] Monthly job failed: %s", exc)
        raise
    finally:
        db.close()


def run_tenure_and_yearly_job() -> dict:
    """Run daily – check tenure + yearly milestones."""
    db = SessionLocal()
    try:
        tenure_count = award_tenure_badges(db)
        yearly_count = award_yearly_milestones(db)
        db.commit()

        logger.info(
            "[badges] Tenure/Yearly job done – tenure=%s yearly=%s",
            tenure_count, yearly_count,
        )
        return {"tenure_awarded": tenure_count, "yearly_awarded": yearly_count}
    except Exception as exc:
        db.rollback()
        logger.error("[badges] Tenure/Yearly job failed: %s", exc)
        raise
    finally:
        db.close()