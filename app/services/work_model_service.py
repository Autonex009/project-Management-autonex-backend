"""Service to evaluate and automatically update employee work models (WFO <-> WFH)
based on monthly check-in patterns.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional, Dict, Any, List, Set

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.daily_checkin import DailyCheckIn
from app.models.leave import Leave
from app.constants.leave_types import is_weekend, is_fixed_holiday
from app.utils.business_time import today_ist
from app.services import audit_service

logger = logging.getLogger(__name__)


def sync_monthly_work_models(
    db: Session,
    target_date: Optional[date] = None,
    min_threshold_days: int = 15,
) -> Dict[str, Any]:
    """
    Evaluates check-in records for the previous calendar month.
    If an employee worked 100% of their checked-in working days as WFH (min threshold met),
    their baseline `work_model` is updated to 'WFH'.
    Conversely, if an employee set to WFH worked 100% of their checked-in working days as WFO,
    their baseline `work_model` is updated to 'WFO'.
    """
    if target_date is None:
        target_date = today_ist()

    # Determine previous calendar month
    if target_date.month == 1:
        prev_year = target_date.year - 1
        prev_month = 12
    else:
        prev_year = target_date.year
        prev_month = target_date.month - 1

    month_start = date(prev_year, prev_month, 1)
    if prev_month == 12:
        month_end = date(prev_year, 12, 31)
    else:
        month_end = date(prev_year, prev_month + 1, 1) - timedelta(days=1)

    month_label = month_start.strftime("%B %Y")
    logger.info("Evaluating monthly work models for period: %s (%s to %s)", month_label, month_start, month_end)

    # Calculate all company working days in the month (excluding weekends and fixed holidays)
    working_days: List[date] = []
    curr = month_start
    while curr <= month_end:
        if not is_weekend(curr) and not is_fixed_holiday(curr):
            working_days.append(curr)
        curr += timedelta(days=1)

    active_employees = db.query(Employee).filter(Employee.status == "active").all()
    if not active_employees:
        return {
            "month": month_label,
            "total_working_days": len(working_days),
            "total_active_evaluated": 0,
            "switched_to_wfh_count": 0,
            "switched_to_wfh": [],
            "switched_to_wfo_count": 0,
            "switched_to_wfo": [],
        }

    active_emp_ids = [emp.id for emp in active_employees]

    # Bulk query 1: All approved leaves for all active employees for the month
    all_leaves = db.query(
        Leave.employee_id,
        Leave.start_date,
        Leave.end_date,
    ).filter(
        Leave.employee_id.in_(active_emp_ids),
        Leave.status == "approved",
        Leave.start_date <= month_end,
        Leave.end_date >= month_start,
    ).all()

    leaves_by_emp: Dict[int, Set[date]] = defaultdict(set)
    for emp_id, l_start, l_end in all_leaves:
        l_curr = max(l_start, month_start)
        l_limit = min(l_end, month_end)
        while l_curr <= l_limit:
            leaves_by_emp[emp_id].add(l_curr)
            l_curr += timedelta(days=1)

    # Bulk query 2: All check-ins for all active employees for the month
    all_checkins = db.query(
        DailyCheckIn.employee_id,
        DailyCheckIn.checkin_date,
        DailyCheckIn.work_mode,
    ).filter(
        DailyCheckIn.employee_id.in_(active_emp_ids),
        DailyCheckIn.checkin_date >= month_start,
        DailyCheckIn.checkin_date <= month_end,
    ).all()

    checkins_by_emp: Dict[int, Dict[date, str]] = defaultdict(dict)
    for emp_id, c_date, w_mode in all_checkins:
        checkins_by_emp[emp_id][c_date] = (w_mode or "WFO").upper()

    switched_to_wfh: List[str] = []
    switched_to_wfo: List[str] = []
    updated_count = 0

    for emp in active_employees:
        # Skip new joiners who joined late in the month
        if emp.created_at and hasattr(emp.created_at, "date"):
            emp_created_date = emp.created_at.date()
            if emp_created_date > (month_start + timedelta(days=15)):
                continue

        # Get approved leave days for this employee in that month (from in-memory cache)
        leave_dates = leaves_by_emp.get(emp.id, set())

        # Working days the employee was expected to work
        effective_working_days = [d for d in working_days if d not in leave_dates]
        if not effective_working_days:
            continue

        # Daily check-ins for the month (from in-memory cache)
        checkin_map = checkins_by_emp.get(emp.id, {})

        # Check-in days on effective working days
        checked_in_working_days = [d for d in effective_working_days if d in checkin_map]

        # Minimum required days to establish a valid monthly pattern
        if len(checked_in_working_days) < min_threshold_days:
            continue

        modes = [checkin_map[d] for d in checked_in_working_days]
        is_all_wfh = all(m == "WFH" for m in modes)
        is_all_wfo = all(m == "WFO" for m in modes)

        current_model = (emp.work_model or "WFO").upper()

        if is_all_wfh and current_model != "WFH":
            old_model = current_model
            emp.work_model = "WFH"
            updated_count += 1
            switched_to_wfh.append(emp.name)

            audit_service.record(
                db,
                actor=None,
                action="employee.work_model_auto_switched",
                category="Employees",
                action_type="Updated",
                entity_type="employee",
                entity_id=emp.id,
                entity_name=emp.name,
                subject_employee_id=emp.id,
                subject_name=emp.name,
                details=audit_service.changes(
                    audit_service.field_diff("Work model", old_model, "WFH"),
                    audit_service.field_diff(
                        "Monthly consistency",
                        None,
                        f"{len(checked_in_working_days)}/{len(effective_working_days)} working days (100% WFH)"
                    ),
                ),
                summary=f"Work model for {emp.name} was automatically updated from {old_model} to WFH based on 100% WFH check-ins for {month_label}.",
            )

        elif is_all_wfo and current_model != "WFO":
            old_model = current_model
            emp.work_model = "WFO"
            updated_count += 1
            switched_to_wfo.append(emp.name)

            audit_service.record(
                db,
                actor=None,
                action="employee.work_model_auto_switched",
                category="Employees",
                action_type="Updated",
                entity_type="employee",
                entity_id=emp.id,
                entity_name=emp.name,
                subject_employee_id=emp.id,
                subject_name=emp.name,
                details=audit_service.changes(
                    audit_service.field_diff("Work model", old_model, "WFO"),
                    audit_service.field_diff(
                        "Monthly consistency",
                        None,
                        f"{len(checked_in_working_days)}/{len(effective_working_days)} working days (100% WFO)"
                    ),
                ),
                summary=f"Work model for {emp.name} was automatically updated from {old_model} to WFO based on 100% WFO check-ins for {month_label}.",
            )

    if updated_count > 0:
        db.commit()

    result = {
        "month": month_label,
        "total_working_days": len(working_days),
        "total_active_evaluated": len(active_employees),
        "switched_to_wfh_count": len(switched_to_wfh),
        "switched_to_wfh": switched_to_wfh,
        "switched_to_wfo_count": len(switched_to_wfo),
        "switched_to_wfo": switched_to_wfo,
    }
    logger.info("Monthly work model sync complete: %s", result)
    return result
