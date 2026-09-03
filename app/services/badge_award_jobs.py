"""Scheduled jobs that calculate hours and award all types of badges."""

import logging
from collections import defaultdict
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.database import SessionLocal
from app.models.employee import Employee
from app.models.encord_analytics import EncordDailyTimeSpent
from app.services.badge_service import (
    award_weekly_badges,
    award_monthly_badges,
    award_tenure_badges,
    award_yearly_milestones,
    expire_due_badges,
    get_previous_week_range,
    get_previous_month_range,
)

# Re-use the same normalization + Autonex filter used by the leaderboard
from app.api.analytics import is_autonex_email, _norm_encord

logger = logging.getLogger(__name__)


def _get_ranked_hours(db: Session, start: date, end: date) -> list[dict]:
    """
    Full ranking of Autonex accounts by platform hours for [start, end].

    Returns a list sorted by hours descending:
    [
      {
        "user_email": str,
        "hours": float,
        "employee_id": int | None,   # None when not mapped to an employee
      },
      ...
    ]
    """
    # Build normalized lookup: normalized_encord_id / email → employee_id
    employees = db.query(Employee).all()
    emp_map: dict[str, int] = {}
    for emp in employees:
        if emp.encord_id:
            emp_map[_norm_encord(emp.encord_id)] = emp.id
        if emp.email:
            # fallback for accounts that still use the company email as encord_id
            emp_map.setdefault(_norm_encord(emp.email), emp.id)

    results = (
        db.query(
            EncordDailyTimeSpent.user_email,
            func.sum(EncordDailyTimeSpent.time_spent_seconds).label("total_seconds"),
        )
        .filter(
            EncordDailyTimeSpent.metric_date >= start,
            EncordDailyTimeSpent.metric_date <= end,
        )
        .group_by(EncordDailyTimeSpent.user_email)
        .all()
    )

    # Aggregate (keep only Autonex accounts – same rule as leaderboard)
    hours_by_email: dict[str, float] = defaultdict(float)
    for user_email, total_seconds in results:
        if not is_autonex_email(user_email):
            continue
        hours_by_email[user_email] += (total_seconds or 0) / 3600.0

    # Build ranked list (true global order)
    ranked = []
    for user_email, hours in sorted(hours_by_email.items(), key=lambda kv: kv[1], reverse=True):
        emp_id = emp_map.get(_norm_encord(user_email))
        ranked.append({
            "user_email": user_email,
            "hours": round(hours, 2),
            "employee_id": emp_id,
        })
    return ranked


def run_weekly_badge_job() -> dict:
    """Run every Monday – process previous week."""
    db = SessionLocal()
    try:
        expire_due_badges(db)

        week_start, week_end = get_previous_week_range()
        ranked = _get_ranked_hours(db, week_start, week_end)

        count = award_weekly_badges(db, week_start, week_end, ranked)
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
        ranked = _get_ranked_hours(db, month_start, month_end)

        count = award_monthly_badges(db, month_start, month_end, ranked)
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