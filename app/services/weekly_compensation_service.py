"""Weekly Compensation Service.

Handles:
1. Weekend and holiday shift detection.
2. Automatic creation of WeeklyCompensationRecord upon weekend check-in.
3. Portal duration recording upon weekend check-out.
4. Automatic Encord active telemetry fallback aggregation (SUM across projects)
   for missed check-outs.
5. In-row snapshotting of hourly rate and financial payout calculations.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.constants.leave_types import is_weekend, is_fixed_holiday
from app.models.weekly_compensation import WeeklyCompensationRecord
from app.models.employee import Employee
from app.models.company_settings import CompanySetting
from app.models.encord_analytics import EncordDailyTimeSpent

logger = logging.getLogger(__name__)

DEFAULT_WEEKLY_HOURLY_RATE = Decimal("143.00")


def is_weekend_or_holiday(d: date) -> bool:
    """Return True if date is a Saturday, Sunday, or official company holiday."""
    return is_weekend(d) or is_fixed_holiday(d)


def get_default_hourly_rate(db: Session) -> Decimal:
    """Fetch default hourly rate from CompanySetting, falling back to DEFAULT_WEEKLY_HOURLY_RATE (143.00)."""
    try:
        setting = db.query(CompanySetting).filter(CompanySetting.key == "weekly_comp_hourly_rate").first()
        if setting and setting.value:
            val = Decimal(str(setting.value).strip())
            if val > 0:
                return val
    except Exception as exc:
        logger.warning("[weekly_compensation] Failed to load rate setting, using fallback %s: %s", DEFAULT_WEEKLY_HOURLY_RATE, exc)
    return DEFAULT_WEEKLY_HOURLY_RATE


def record_weekend_checkin(
    db: Session,
    employee_id: int,
    daily_checkin_id: int,
    checkin_date: date,
    project_id: Optional[int] = None,
    pm_id: Optional[int] = None,
) -> Optional[WeeklyCompensationRecord]:
    """Create or get a WeeklyCompensationRecord for a weekend or holiday check-in.

    Idempotent: uses (employee_id, work_date) unique constraint guard.
    """
    if not is_weekend_or_holiday(checkin_date):
        return None

    # Check for existing record
    record = (
        db.query(WeeklyCompensationRecord)
        .filter(
            WeeklyCompensationRecord.employee_id == employee_id,
            WeeklyCompensationRecord.work_date == checkin_date,
        )
        .first()
    )

    if record:
        # Update daily_checkin_id or project if not set
        if not record.daily_checkin_id and daily_checkin_id:
            record.daily_checkin_id = daily_checkin_id
        if not record.project_id and project_id:
            record.project_id = project_id
        if not record.pm_id and pm_id:
            record.pm_id = pm_id
        return record

    hourly_rate = get_default_hourly_rate(db)
    payroll_month = checkin_date.strftime("%Y-%m")
    week_number = checkin_date.isocalendar()[1]

    new_record = WeeklyCompensationRecord(
        employee_id=employee_id,
        project_id=project_id,
        pm_id=pm_id,
        daily_checkin_id=daily_checkin_id,
        work_date=checkin_date,
        payroll_month=payroll_month,
        week_number=week_number,
        is_weekend=True,
        portal_duration_seconds=None,
        encord_duration_seconds=0,
        final_duration_seconds=0,
        is_final_time_overridden=False,
        hourly_rate=hourly_rate,
        calculated_payout=Decimal("0.00"),
        override_payout=None,
        status="pending",
        is_locked=False,
    )
    db.add(new_record)
    db.flush()
    logger.info(
        "[weekly_compensation] Created shift record #%s for employee #%s on %s",
        new_record.id,
        employee_id,
        checkin_date,
    )
    return new_record


def record_weekend_checkout(
    db: Session,
    employee_id: int,
    checkin_date: date,
    checked_in_at: datetime,
    checked_out_at: datetime,
) -> Optional[WeeklyCompensationRecord]:
    """Calculate and stamp portal duration on weekend check-out.

    Sets final_duration_seconds to portal duration unless manually overridden.
    """
    if not is_weekend_or_holiday(checkin_date):
        return None

    if checked_out_at < checked_in_at:
        logger.warning("[weekly_compensation] Invalid timestamps: out %s < in %s", checked_out_at, checked_in_at)
        return None

    portal_seconds = max(0, int((checked_out_at - checked_in_at).total_seconds()))

    record = (
        db.query(WeeklyCompensationRecord)
        .filter(
            WeeklyCompensationRecord.employee_id == employee_id,
            WeeklyCompensationRecord.work_date == checkin_date,
        )
        .first()
    )

    if not record:
        # Create record if check-in hook was bypassed
        hourly_rate = get_default_hourly_rate(db)
        payroll_month = checkin_date.strftime("%Y-%m")
        week_number = checkin_date.isocalendar()[1]
        calculated_payout = round((Decimal(portal_seconds) / Decimal(3600.0)) * hourly_rate, 2)
        record = WeeklyCompensationRecord(
            employee_id=employee_id,
            work_date=checkin_date,
            payroll_month=payroll_month,
            week_number=week_number,
            is_weekend=True,
            portal_duration_seconds=portal_seconds,
            encord_duration_seconds=0,
            final_duration_seconds=portal_seconds,
            is_final_time_overridden=False,
            hourly_rate=hourly_rate,
            calculated_payout=calculated_payout,
            status="pending",
            is_locked=False,
        )
        db.add(record)
        db.flush()
        return record

    if record.is_locked:
        logger.warning("[weekly_compensation] Record #%s is locked in payroll, skipping checkout update", record.id)
        return record

    record.portal_duration_seconds = portal_seconds

    # Only update final time if not manually overridden by a manager
    if not record.is_final_time_overridden:
        record.final_duration_seconds = portal_seconds
        record.calculated_payout = round(
            (Decimal(portal_seconds) / Decimal(3600.0)) * record.hourly_rate, 2
        )

    logger.info(
        "[weekly_compensation] Recorded checkout for record #%s: %s seconds (₹%s)",
        record.id,
        portal_seconds,
        record.calculated_payout,
    )
    return record


def sync_encord_weekly_compensation_fallback(db: Session, target_date: date) -> int:
    """Post-telemetry sync hook: aggregates Encord active time and applies fallback.

    1. Enforces SUM(time_spent_seconds) across ALL Encord projects for each user.
    2. Applies fallback to final_duration_seconds ONLY if:
       - Portal check-out was missed (portal_duration_seconds is None)
       - Manager has NOT manually overridden final time (is_final_time_overridden is False)
       - Record is not locked (is_locked is False)
    Returns number of records updated.
    """
    records = (
        db.query(WeeklyCompensationRecord)
        .filter(
            WeeklyCompensationRecord.work_date == target_date,
            WeeklyCompensationRecord.is_locked == False,
        )
        .all()
    )

    if not records:
        return 0

    # Build map of employee_id -> encord_email
    emp_ids = [r.employee_id for r in records]
    employees = db.query(Employee.id, Employee.encord_id, Employee.email).filter(Employee.id.in_(emp_ids)).all()
    emp_email_map = {}
    for emp in employees:
        email = emp.encord_id or emp.email
        if email:
            emp_email_map[emp.id] = email.strip().lower()

    # Aggregate telemetry per email for this date
    emails = list(set(emp_email_map.values()))
    if not emails:
        return 0

    encord_totals = (
        db.query(
            func.lower(EncordDailyTimeSpent.user_email).label("email"),
            func.coalesce(func.sum(EncordDailyTimeSpent.time_spent_seconds), 0).label("total_seconds"),
        )
        .filter(
            EncordDailyTimeSpent.metric_date == target_date,
            func.lower(EncordDailyTimeSpent.user_email).in_(emails),
        )
        .group_by(func.lower(EncordDailyTimeSpent.user_email))
        .all()
    )
    email_seconds_map = {row.email: int(row.total_seconds) for row in encord_totals}

    updated_count = 0
    for record in records:
        email = emp_email_map.get(record.employee_id)
        encord_seconds = email_seconds_map.get(email, 0) if email else 0
        record.encord_duration_seconds = encord_seconds

        # Fallback condition: missed portal check-out AND not manually overridden
        if record.portal_duration_seconds is None and not record.is_final_time_overridden:
            record.final_duration_seconds = encord_seconds
            record.calculated_payout = round(
                (Decimal(encord_seconds) / Decimal(3600.0)) * record.hourly_rate, 2
            )
            updated_count += 1

    db.flush()
    logger.info(
        "[weekly_compensation] Telemetry fallback sync for %s complete: %s records evaluated, %s updated",
        target_date,
        len(records),
        updated_count,
    )
    return updated_count
