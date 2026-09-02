from datetime import date, datetime, time, timedelta
from typing import Optional, Any, Dict, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from dateutil.relativedelta import relativedelta

from app.constants.badges_catalog import BADGE_CATALOG, VALID_BADGE_CODES
from app.models.employee_badge import EmployeeBadge, EmployeeBadgeLog


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59))


def default_expires_at(
    badge_code: str,
    period_end: Optional[date],
) -> Optional[datetime]:
    """Compute expires_at from catalog rules when caller does not pass one."""
    info = BADGE_CATALOG.get(badge_code)
    if not info or not info["expires"]:
        return None
    if period_end is None:
        return None
    return _end_of_day(period_end)


def _write_log(
    db: Session,
    *,
    employee_badge_id: Optional[int],
    employee_id: int,
    badge_code: str,
    action: str,
    period_start: Optional[date],
    period_end: Optional[date],
    details: Optional[dict[str, Any]],
    actor_id: Optional[int],
) -> EmployeeBadgeLog:
    log = EmployeeBadgeLog(
        employee_badge_id=employee_badge_id,
        employee_id=employee_id,
        badge_code=badge_code,
        action=action,
        period_start=period_start,
        period_end=period_end,
        details=details,
        actor_id=actor_id,
    )
    db.add(log)
    return log


def award_badge(
    db: Session,
    *,
    employee_id: int,
    badge_code: str,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    expires_at: Optional[datetime] = None,
    meta: Optional[dict[str, Any]] = None,
    actor_id: Optional[int] = None,
) -> Optional[EmployeeBadge]:
    """
    Award a badge if not already awarded for the same period.
    Returns the new row, or None if duplicate / invalid code.
    """
    if badge_code not in VALID_BADGE_CODES:
        return None

    if expires_at is None:
        expires_at = default_expires_at(badge_code, period_end)

    existing = (
        db.query(EmployeeBadge)
        .filter(
            EmployeeBadge.employee_id == employee_id,
            EmployeeBadge.badge_code == badge_code,
            EmployeeBadge.period_start == period_start,
            EmployeeBadge.period_end == period_end,
        )
        .first()
    )
    if existing:
        return None

    badge = EmployeeBadge(
        employee_id=employee_id,
        badge_code=badge_code,
        period_start=period_start,
        period_end=period_end,
        expires_at=expires_at,
        status="active",
        awarded_by=actor_id,
        meta=meta,
    )
    db.add(badge)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None

    _write_log(
        db,
        employee_badge_id=badge.id,
        employee_id=employee_id,
        badge_code=badge_code,
        action="awarded",
        period_start=period_start,
        period_end=period_end,
        details=meta,
        actor_id=actor_id,
    )
    return badge


def expire_due_badges(db: Session, now: Optional[datetime] = None) -> int:
    """Expire active badges past expires_at. Returns number expired."""
    now = now or datetime.utcnow()
    due = (
        db.query(EmployeeBadge)
        .filter(
            EmployeeBadge.status == "active",
            EmployeeBadge.expires_at.isnot(None),
            EmployeeBadge.expires_at <= now,
        )
        .all()
    )
    count = 0
    for badge in due:
        badge.status = "expired"
        _write_log(
            db,
            employee_badge_id=badge.id,
            employee_id=badge.employee_id,
            badge_code=badge.badge_code,
            action="expired",
            period_start=badge.period_start,
            period_end=badge.period_end,
            details={"expires_at": badge.expires_at.isoformat() if badge.expires_at else None},
            actor_id=None,
        )
        count += 1
    return count


def revoke_badge(
    db: Session,
    badge: EmployeeBadge,
    *,
    actor_id: Optional[int] = None,
) -> EmployeeBadge:
    if badge.status == "revoked":
        return badge
    badge.status = "revoked"
    _write_log(
        db,
        employee_badge_id=badge.id,
        employee_id=badge.employee_id,
        badge_code=badge.badge_code,
        action="revoked",
        period_start=badge.period_start,
        period_end=badge.period_end,
        details=None,
        actor_id=actor_id,
    )
    return badge


# =====================================================================
# Week / Month helpers
# =====================================================================

def get_week_range(reference: date) -> Tuple[date, date]:
    """Return (monday, sunday) of the week containing reference."""
    start = reference - timedelta(days=reference.weekday())
    end = start + timedelta(days=6)
    return start, end


def get_previous_week_range(today: Optional[date] = None) -> Tuple[date, date]:
    today = today or date.today()
    this_monday, _ = get_week_range(today)
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = prev_monday + timedelta(days=6)
    return prev_monday, prev_sunday


def get_previous_month_range(today: Optional[date] = None) -> Tuple[date, date]:
    today = today or date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)
    return first_of_prev_month, last_of_prev_month


# =====================================================================
# Weekly badges (50 hrs + top 1/2/3)
# =====================================================================

def award_weekly_badges(
    db: Session,
    week_start: date,
    week_end: date,
    hours_by_user: Dict[str, float],
    emp_map: Dict[str, int],
    target_employee_id: Optional[int] = None,
) -> int:
    """
    Award hrs_50_week + weekly_top_1/2/3.
    Badge stays visible during the following week.
    """
    next_week_end = week_end + timedelta(days=7)
    expires_at = _end_of_day(next_week_end)
    awarded = 0

    # 50 hours threshold
    for user, hours in hours_by_user.items():
        emp_id = emp_map.get(user)
        if not emp_id: continue
        if target_employee_id and emp_id != target_employee_id: continue
        if hours >= 50:
            if award_badge(
                db,
                employee_id=emp_id,
                badge_code="hrs_50_week",
                period_start=week_start,
                period_end=week_end,
                expires_at=expires_at,
                meta={"hours": round(hours, 2)},
            ):
                awarded += 1

    # Top 1 / 2 / 3
    ranked = sorted(hours_by_user.items(), key=lambda x: x[1], reverse=True)
    for rank, (user, hours) in enumerate(ranked[:3], start=1):
        emp_id = emp_map.get(user)
        if not emp_id: continue
        if target_employee_id and emp_id != target_employee_id: continue
        if award_badge(
            db,
            employee_id=emp_id,
            badge_code=f"weekly_top_{rank}",
            period_start=week_start,
            period_end=week_end,
            expires_at=expires_at,
            meta={"hours": round(hours, 2), "rank": rank},
        ):
            awarded += 1

    return awarded


# =====================================================================
# Monthly badges (200 hrs + top 1/2/3)
# =====================================================================

def award_monthly_badges(
    db: Session,
    month_start: date,
    month_end: date,
    hours_by_user: Dict[str, float],
    emp_map: Dict[str, int],
    target_employee_id: Optional[int] = None,
) -> int:
    """
    Award hrs_200_month + monthly_top_1/2/3.
    Badge stays visible during the following month.
    """
    next_month_start = month_end + timedelta(days=1)
    if next_month_start.month == 12:
        next_month_end = date(next_month_start.year + 1, 1, 1) - timedelta(days=1)
    else:
        next_month_end = (
            date(next_month_start.year, next_month_start.month + 1, 1) - timedelta(days=1)
        )

    expires_at = _end_of_day(next_month_end)
    awarded = 0

    # 200 hours threshold
    for user, hours in hours_by_user.items():
        emp_id = emp_map.get(user)
        if not emp_id: continue
        if target_employee_id and emp_id != target_employee_id: continue
        if hours >= 200:
            if award_badge(
                db,
                employee_id=emp_id,
                badge_code="hrs_200_month",
                period_start=month_start,
                period_end=month_end,
                expires_at=expires_at,
                meta={"hours": round(hours, 2)},
            ):
                awarded += 1

    # Top 1 / 2 / 3
    ranked = sorted(hours_by_user.items(), key=lambda x: x[1], reverse=True)
    for rank, (user, hours) in enumerate(ranked[:3], start=1):
        emp_id = emp_map.get(user)
        if not emp_id: continue
        if target_employee_id and emp_id != target_employee_id: continue
        if award_badge(
            db,
            employee_id=emp_id,
            badge_code=f"monthly_top_{rank}",
            period_start=month_start,
            period_end=month_end,
            expires_at=expires_at,
            meta={"hours": round(hours, 2), "rank": rank},
        ):
            awarded += 1

    return awarded


# =====================================================================
# Tenure badges (3 months / 6 months) – based on created_at
# =====================================================================

def award_tenure_badges(db: Session, today: Optional[date] = None) -> int:
    """
    Award tenure_3_months and tenure_6_months based on employee.created_at.
    These are one-time and never expire.
    """
    from app.models.employee import Employee

    today = today or date.today()
    awarded = 0

    employees = db.query(Employee).filter(Employee.created_at.isnot(None)).all()

    for emp in employees:
        join = emp.created_at.date() if isinstance(emp.created_at, datetime) else emp.created_at
        if not join:
            continue

        months = (today.year - join.year) * 12 + (today.month - join.month)
        if today.day < join.day:
            months -= 1

        if months >= 3:
            if award_badge(
                db,
                employee_id=emp.id,
                badge_code="tenure_3_months",
                period_start=None,
                period_end=None,
                expires_at=None,
                meta={
                    "based_on": "created_at",
                    "created_at": join.isoformat(),
                    "months": months,
                },
            ):
                awarded += 1

        if months >= 6:
            if award_badge(
                db,
                employee_id=emp.id,
                badge_code="tenure_6_months",
                period_start=None,
                period_end=None,
                expires_at=None,
                meta={
                    "based_on": "created_at",
                    "created_at": join.isoformat(),
                    "months": months,
                },
            ):
                awarded += 1

    return awarded


# =====================================================================
# Yearly milestone – one badge per completed year (based on created_at)
# =====================================================================

def award_yearly_milestones(db: Session, today: Optional[date] = None) -> int:
    """
    Award one 'yearly_milestone' badge for every completed year
    based on employee.created_at.
    Example: 2 years completed → 2 separate badge rows.
    """
    from app.models.employee import Employee

    today = today or date.today()
    awarded = 0

    employees = db.query(Employee).filter(Employee.created_at.isnot(None)).all()

    for emp in employees:
        join = emp.created_at.date() if isinstance(emp.created_at, datetime) else emp.created_at
        if not join:
            continue

        years = today.year - join.year
        if (today.month, today.day) < (join.month, join.day):
            years -= 1

        if years <= 0:
            continue

        for year_num in range(1, years + 1):
            anniversary = join + relativedelta(years=year_num)

            if award_badge(
                db,
                employee_id=emp.id,
                badge_code="yearly_milestone",
                period_start=anniversary,
                period_end=anniversary,
                expires_at=None,
                meta={
                    "based_on": "created_at",
                    "year_number": year_num,
                    "created_at": join.isoformat(),
                    "anniversary": anniversary.isoformat(),
                },
            ):
                awarded += 1

    return awarded