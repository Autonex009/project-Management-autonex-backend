"""
Chat tools — the functions the Gemini agent can invoke.

Each tool is a thin wrapper that queries the database using the authenticated
user's employee_id and returns structured data for the LLM to format.
Write operations return confirmation payloads instead of executing directly.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import extract
from sqlalchemy.orm import Session

from app.models.leave import Leave
from app.models.wfh import WFHRequest
from app.models.allocation import Allocation
from app.models.project import DailySheet
from app.models.parent_project import MainProject
from app.models.employee import Employee
from app.constants.leave_types import (
    ANNUAL_LEAVE_QUOTA,
    INTERN_MONTHLY_PAID_QUOTA,
    FIXED_HOLIDAYS_2026,
    FLOATER_DATES_2026,
    FIXED_HOLIDAYS_BY_YEAR,
    FLOATER_DATES_BY_YEAR,
    LEAVE_TYPE_LABELS,
    is_intern,
    is_non_working_day,
    is_weekend,
    normalize_leave_type,
    get_leave_type_label,
)
from app.services.knowledge_service import search_policy as _search_policy

logger = logging.getLogger(__name__)


# ── Helper: count working days ──────────────────────────────────────
def _count_working_days(start: date, end: date) -> int:
    """Count working days between start and end (inclusive), excluding weekends and fixed holidays."""
    count = 0
    current = start
    while current <= end:
        if not is_non_working_day(current):
            count += 1
        current += timedelta(days=1)
    return count


# ── Tool: get_leave_balance ─────────────────────────────────────────
def get_leave_balance(employee_id: int, db: Session) -> dict:
    """
    Compute the leave balance for an employee for the current year.
    Mirrors the logic in MyLeavesPanel.jsx but server-side.
    """
    current_year = date.today().year

    # Get the employee to check type (intern vs full-time)
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return {"error": f"Employee {employee_id} not found"}

    employee_is_intern = is_intern(employee.employee_type)

    # Get all approved leaves for this year
    leaves = (
        db.query(Leave)
        .filter(
            Leave.employee_id == employee_id,
            Leave.status == "approved",
            extract("year", Leave.start_date) == current_year,
        )
        .all()
    )

    # Count used days per type
    used = {"paid": 0, "casual_sick": 0, "floater": 0}
    for leave in leaves:
        lt = normalize_leave_type(leave.leave_type)
        if lt in used:
            days = _count_working_days(leave.start_date, leave.end_date)
            if leave.is_half_day:
                days = 0.5
            used[lt] += days

    # Build balance
    balance = {}
    for leave_type, quota in ANNUAL_LEAVE_QUOTA.items():
        effective_quota = quota
        if employee_is_intern and leave_type == "paid":
            # Interns get 1 paid leave/month
            effective_quota = INTERN_MONTHLY_PAID_QUOTA
            # For monthly tracking, only count used this month
            current_month = date.today().month
            monthly_leaves = [
                lv for lv in leaves
                if normalize_leave_type(lv.leave_type) == "paid"
                and lv.start_date.month == current_month
            ]
            monthly_used = sum(
                0.5 if lv.is_half_day else _count_working_days(lv.start_date, lv.end_date)
                for lv in monthly_leaves
            )
            balance[leave_type] = {
                "label": get_leave_type_label(leave_type),
                "quota": effective_quota,
                "used": monthly_used,
                "remaining": max(0, effective_quota - monthly_used),
                "note": "Monthly quota (resets each month)",
            }
        else:
            balance[leave_type] = {
                "label": get_leave_type_label(leave_type),
                "quota": effective_quota,
                "used": used.get(leave_type, 0),
                "remaining": max(0, effective_quota - used.get(leave_type, 0)),
            }

    # Also count pending leaves
    pending = (
        db.query(Leave)
        .filter(
            Leave.employee_id == employee_id,
            Leave.status == "pending",
            extract("year", Leave.start_date) == current_year,
        )
        .count()
    )

    return {
        "employee_name": employee.name,
        "employee_type": employee.employee_type,
        "year": current_year,
        "balance": balance,
        "pending_requests": pending,
    }


# ── Tool: get_my_leaves ────────────────────────────────────────────
def get_my_leaves(employee_id: int, db: Session) -> dict:
    """Get all leave requests for the employee, sorted by date."""
    leaves = (
        db.query(Leave)
        .filter(Leave.employee_id == employee_id)
        .order_by(Leave.start_date.desc())
        .limit(20)
        .all()
    )

    return {
        "leaves": [
            {
                "id": lv.id,
                "type": get_leave_type_label(lv.leave_type),
                "type_raw": normalize_leave_type(lv.leave_type),
                "start_date": lv.start_date.isoformat(),
                "end_date": lv.end_date.isoformat(),
                "working_days": _count_working_days(lv.start_date, lv.end_date),
                "is_half_day": bool(lv.is_half_day),
                "reason": lv.reason or "",
                "status": lv.status,
            }
            for lv in leaves
        ],
        "total": len(leaves),
    }


# ── Tool: get_wfh_usage ────────────────────────────────────────────
def get_wfh_usage(employee_id: int, db: Session) -> dict:
    """Get WFH usage for the employee — this week, this month, and overall."""
    today = date.today()

    # This week (Monday to Sunday)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    # This month
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)

    # All approved or pending WFH requests
    wfh_requests = (
        db.query(WFHRequest)
        .filter(WFHRequest.employee_id == employee_id)
        .all()
    )

    active = [w for w in wfh_requests if w.status in ("approved", "pending")]

    this_week = [w for w in active if w.wfh_date and week_start <= w.wfh_date <= week_end]
    this_month = [w for w in active if w.wfh_date and month_start <= w.wfh_date <= month_end]

    # Upcoming
    upcoming = [
        {
            "id": w.id,
            "date": w.wfh_date.isoformat() if w.wfh_date else None,
            "end_date": w.end_date.isoformat() if w.end_date else None,
            "status": w.status,
            "reason": w.reason or "",
        }
        for w in wfh_requests
        if w.wfh_date and w.wfh_date >= today
    ]

    return {
        "this_week": len(this_week),
        "this_month": len(this_month),
        "total_this_year": len([w for w in active if w.wfh_date and w.wfh_date.year == today.year]),
        "upcoming": sorted(upcoming, key=lambda x: x["date"] or "")[:5],
    }


# ── Tool: get_my_projects ──────────────────────────────────────────
def get_my_projects(employee_id: int, db: Session) -> dict:
    """Get the employee's current project allocations."""
    allocations = (
        db.query(Allocation)
        .filter(Allocation.employee_id == employee_id)
        .all()
    )

    projects = []
    for alloc in allocations:
        # Get the daily sheet / sub-project
        daily_sheet = db.query(DailySheet).filter(DailySheet.id == alloc.sub_project_id).first()
        if not daily_sheet:
            continue

        # Get the main project
        main_project = None
        if daily_sheet.main_project_id:
            main_project = db.query(MainProject).filter(MainProject.id == daily_sheet.main_project_id).first()

        # Get PM name
        pm_name = None
        if main_project and main_project.program_manager_id:
            pm = db.query(Employee).filter(Employee.id == main_project.program_manager_id).first()
            pm_name = pm.name if pm else None

        projects.append({
            "project_name": main_project.name if main_project else daily_sheet.name,
            "sub_project": daily_sheet.name,
            "client": main_project.client if main_project else daily_sheet.client,
            "role_tags": alloc.role_tags or [],
            "daily_hours": alloc.total_daily_hours or 8,
            "start_date": alloc.active_start_date.isoformat() if alloc.active_start_date else None,
            "end_date": alloc.active_end_date.isoformat() if alloc.active_end_date else None,
            "pm_name": pm_name,
            "status": daily_sheet.project_status,
        })

    total_hours = sum(p["daily_hours"] for p in projects)

    return {
        "allocations": projects,
        "total_daily_hours": total_hours,
        "total_projects": len(projects),
    }


# ── Tool: get_holidays ─────────────────────────────────────────────
def get_holidays(year: int = None) -> dict:
    """Get fixed holidays and floater dates for the given year."""
    if year is None:
        year = date.today().year

    fixed = FIXED_HOLIDAYS_BY_YEAR.get(year, frozenset())
    floaters = FLOATER_DATES_BY_YEAR.get(year, frozenset())

    today = date.today()

    fixed_list = sorted([
        {"date": d.isoformat(), "name": _get_holiday_name(d), "past": d < today}
        for d in fixed
    ], key=lambda x: x["date"])

    floater_list = sorted([
        {"date": d.isoformat(), "name": _get_floater_name(d), "past": d < today}
        for d in floaters
    ], key=lambda x: x["date"])

    # Next upcoming holiday
    upcoming_fixed = [h for h in fixed_list if not h["past"]]
    next_holiday = upcoming_fixed[0] if upcoming_fixed else None

    return {
        "year": year,
        "fixed_holidays": fixed_list,
        "floater_dates": floater_list,
        "next_holiday": next_holiday,
        "total_fixed": len(fixed_list),
        "total_floaters": len(floater_list),
    }


# Holiday name lookup
_HOLIDAY_NAMES = {
    (1, 1): "New Year's Day",
    (1, 26): "Republic Day",
    (3, 4): "Holi",
    (5, 1): "Maharashtra Day",
    (6, 26): "Muharram",
    (8, 15): "Independence Day",
    (9, 14): "Ganesh Chaturthi",
    (10, 2): "Mahatma Gandhi Jayanti",
    (11, 9): "Govardhan Puja",
    (12, 25): "Christmas",
}

_FLOATER_NAMES = {
    (1, 14): "Pongal / Makar Sankranti",
    (1, 23): "Vasant Panchami",
    (2, 15): "Maha Shivratri",
    (2, 19): "Shivaji Jayanti",
    (3, 19): "Ugadi / Gudi Padwa",
    (3, 21): "Ramzan Eid",
    (3, 31): "Mahavir Jayanti",
    (4, 3): "Good Friday",
    (4, 14): "Ambedkar Jayanti",
    (5, 27): "Bakrid",
    (8, 15): "Independence Day",
    (8, 26): "Onam",
    (8, 28): "Raksha Bandhan",
    (9, 4): "Janmashtami",
    (10, 20): "Dussehra",
    (11, 8): "Diwali",
    (11, 11): "Bhai Duj",
    (11, 24): "Guru Nanak Jayanti",
    (12, 23): "Hazarat Ali's Birthday",
}


def _get_holiday_name(d: date) -> str:
    return _HOLIDAY_NAMES.get((d.month, d.day), f"Holiday ({d.isoformat()})")


def _get_floater_name(d: date) -> str:
    return _FLOATER_NAMES.get((d.month, d.day), f"Floater ({d.isoformat()})")


# ── Tool: plan_leave ────────────────────────────────────────────────
def plan_leave(
    employee_id: int,
    days_wanted: int,
    preferred_month: Optional[int],
    db: Session,
) -> dict:
    """
    Smart leave planner: suggests optimal dates considering holidays, weekends,
    and leave balance.
    """
    today = date.today()
    year = today.year
    target_month = preferred_month or (today.month + 1 if today.month < 12 else 12)

    # Get balance first
    balance = get_leave_balance(employee_id, db)
    if "error" in balance:
        return balance

    paid_remaining = balance["balance"]["paid"]["remaining"]

    # Get holidays for the year
    fixed = FIXED_HOLIDAYS_BY_YEAR.get(year, frozenset())

    # Find optimal windows in the target month
    month_start = date(year, target_month, 1)
    if target_month == 12:
        month_end = date(year, 12, 31)
    else:
        month_end = date(year, target_month + 1, 1) - timedelta(days=1)

    suggestions = []

    # Scan through the month for potential windows
    current = max(month_start, today + timedelta(days=1))  # Can't take leave today or before
    while current <= month_end - timedelta(days=days_wanted - 1):
        # Try a window starting at 'current'
        window_start = current
        window_days = 0
        leave_days_needed = 0
        scan = window_start
        holidays_in_window = []
        weekends_in_window = []

        while window_days < days_wanted and scan <= month_end + timedelta(days=7):
            if is_weekend(scan):
                weekends_in_window.append(scan)
                window_days += 1
            elif scan in fixed:
                holidays_in_window.append(scan)
                window_days += 1
            else:
                leave_days_needed += 1
                window_days += 1
            scan += timedelta(days=1)

        window_end = scan - timedelta(days=1)
        total_consecutive = (window_end - window_start).days + 1

        if leave_days_needed <= days_wanted and leave_days_needed <= paid_remaining:
            savings = days_wanted - leave_days_needed

            suggestions.append({
                "start_date": window_start.isoformat(),
                "end_date": window_end.isoformat(),
                "leave_days_used": leave_days_needed,
                "total_days_off": total_consecutive,
                "holidays_included": [d.isoformat() for d in holidays_in_window],
                "savings": savings,
                "savings_note": f"Save {savings} leave day(s) by using holidays/weekends" if savings > 0 else "Standard block",
            })

        current += timedelta(days=1)

    # Sort by savings (best first), then by date
    suggestions.sort(key=lambda x: (-x["savings"], x["start_date"]))

    return {
        "days_requested": days_wanted,
        "target_month": target_month,
        "paid_leaves_remaining": paid_remaining,
        "sufficient_balance": paid_remaining >= days_wanted,
        "suggestions": suggestions[:3],  # Top 3
        "tip": f"You have {paid_remaining} paid leaves remaining this year." if paid_remaining >= days_wanted
               else f"You only have {paid_remaining} paid leaves. {days_wanted - paid_remaining} day(s) would be unpaid.",
    }


# ── Tool: search_policy_docs ───────────────────────────────────────
def search_policy_docs(query: str) -> dict:
    """Search the knowledge base for policy information."""
    results = _search_policy(query, top_k=3)

    if not results:
        return {
            "found": False,
            "message": "No relevant policy information found for this query.",
            "results": [],
        }

    return {
        "found": True,
        "results": [
            {
                "text": r["text"],
                "source": r["source"],
                "section": r["section"],
                "relevance": round(r["score"], 3),
            }
            for r in results
        ],
    }


# ── Tool: apply_leave (returns confirmation payload) ────────────────
def prepare_apply_leave(
    employee_id: int,
    leave_type: str,
    start_date: str,
    end_date: str,
    reason: str,
    db: Session,
) -> dict:
    """
    Validate and prepare a leave application. Does NOT create the leave —
    returns a confirmation payload for the user to approve.
    """
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}

    if start < date.today():
        return {"error": "Cannot apply leave for past dates."}

    if end < start:
        return {"error": "End date must be on or after start date."}

    normalized_type = normalize_leave_type(leave_type)
    working_days = _count_working_days(start, end)

    # Check balance
    balance = get_leave_balance(employee_id, db)
    if "error" in balance:
        return balance

    type_balance = balance["balance"].get(normalized_type)
    if not type_balance:
        return {"error": f"Unknown leave type: {leave_type}"}

    remaining_after = type_balance["remaining"] - working_days

    return {
        "action": "apply_leave",
        "requires_confirmation": True,
        "details": {
            "employee_id": employee_id,
            "leave_type": normalized_type,
            "leave_type_label": get_leave_type_label(normalized_type),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "working_days": working_days,
            "reason": reason,
            "current_balance": type_balance["remaining"],
            "balance_after": max(0, remaining_after),
            "will_be_unpaid": remaining_after < 0,
            "unpaid_days": abs(remaining_after) if remaining_after < 0 else 0,
        },
    }


# ── Tool: apply_wfh (returns confirmation payload) ──────────────────
def prepare_apply_wfh(
    employee_id: int,
    wfh_date: str,
    end_date: Optional[str],
    reason: str,
    db: Session,
) -> dict:
    """
    Validate and prepare a WFH application. Does NOT create the request —
    returns a confirmation payload for the user to approve.
    """
    try:
        start = date.fromisoformat(wfh_date)
        end = date.fromisoformat(end_date) if end_date else start
    except ValueError:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}

    if start < date.today():
        return {"error": "Cannot apply WFH for past dates."}

    # Check for overlapping WFH
    existing = (
        db.query(WFHRequest)
        .filter(
            WFHRequest.employee_id == employee_id,
            WFHRequest.status.in_(["pending", "approved"]),
            WFHRequest.wfh_date <= end,
            WFHRequest.end_date >= start,
        )
        .first()
    )

    if existing:
        return {"error": f"You already have a WFH request overlapping with these dates (#{existing.id})."}

    working_days = _count_working_days(start, end)

    return {
        "action": "apply_wfh",
        "requires_confirmation": True,
        "details": {
            "employee_id": employee_id,
            "wfh_date": start.isoformat(),
            "end_date": end.isoformat(),
            "working_days": working_days,
            "reason": reason,
        },
    }


# ── Tool: cancel_leave (returns confirmation payload) ───────────────
def prepare_cancel_leave(
    employee_id: int,
    leave_id: int,
    db: Session,
) -> dict:
    """
    Validate and prepare leave cancellation. Returns confirmation payload.
    """
    leave = db.query(Leave).filter(
        Leave.id == leave_id,
        Leave.employee_id == employee_id,
    ).first()

    if not leave:
        return {"error": f"Leave request #{leave_id} not found or does not belong to you."}

    if leave.start_date < date.today():
        return {"error": "Cannot cancel a leave that has already started or passed."}

    return {
        "action": "cancel_leave",
        "requires_confirmation": True,
        "details": {
            "leave_id": leave.id,
            "employee_id": employee_id,
            "leave_type_label": get_leave_type_label(leave.leave_type),
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
            "status": leave.status,
            "reason": leave.reason or "",
        },
    }
