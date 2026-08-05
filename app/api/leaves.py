import json
import os
from datetime import timedelta, date as date_type, timezone, datetime, time as time_type
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_current_ist_datetime() -> datetime:
    utc_now = datetime.now(timezone.utc)
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    return utc_now.astimezone(ist_tz)


def validate_half_day_timing(start_date: date_type, half_day_slot: str) -> None:
    current_dt = get_current_ist_datetime()
    current_date = current_dt.date()
    current_time = current_dt.time()

    if half_day_slot == "first_half":
        if current_date >= start_date:
            raise HTTPException(
                status_code=400,
                detail="First-half leaves must be applied at least one day in advance.",
            )
    elif half_day_slot == "second_half":
        if current_date > start_date:
            raise HTTPException(
                status_code=400,
                detail="Cannot apply for a second-half leave after the request date has passed.",
            )
        elif current_date == start_date:
            if current_time > time_type(14, 0):
                raise HTTPException(
                    status_code=400,
                    detail="Second-half leaves must be applied before 2:00 PM on the same day.",
                )
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid half-day slot. Must be 'first_half' or 'second_half'.",
        )

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from app.db.database import get_db
from app.constants.leave_types import (
    RAZORPAY_LEAVE_TYPE_IDS, get_leave_type_label, normalize_leave_type,
    is_valid_floater_date, get_floater_dates_for_year,
    is_weekend, is_fixed_holiday, get_fixed_holidays_for_year,
    is_intern_or_contractor,
)
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.parent_project import MainProject
from app.models.project import DailySheet
from app.models.sub_project import SubProject
from app.models.user import User
from app.models.notification import Notification
from app.schemas.leave import Leave as LeaveSchema, LeaveCreate
from app.services.slack_service import (
    get_or_cache_employee_slack_user_id,
    try_get_or_cache_employee_slack_user_id,
    try_send_leave_applied_message,
    try_send_pm_leave_request_message,
    try_send_leave_status_message,
)

from app.services.auth_service import get_current_user, require_role
from app.services import audit_service

# NOTE: ``Request`` at the top of this module is urllib's, used for the Razorpay
# calls. FastAPI's request object is aliased so the two never get confused — a bare
# ``Request`` annotation here would silently break request parsing.
from fastapi import Request as HTTPRequest

router = APIRouter(prefix="/api/leaves", tags=["Leaves"], dependencies=[Depends(get_current_user)])


def check_leave_access(leave_employee_id: int, current_user: User, db: Session):
    if current_user.role not in ["admin", "pm", "hr"]:
        is_self = current_user.employee_id == leave_employee_id
        if not is_self:
            emp = db.query(Employee).filter(Employee.id == leave_employee_id).first()
            if not emp or emp.email != current_user.email:
                raise HTTPException(status_code=403, detail="Access denied")


def _push_notification(db: Session, user_id: int, title: str, message: str, notif_type: str) -> None:
    """Persist an in-app notification for the given user."""
    n = Notification(user_id=user_id, title=title, message=message, type=notif_type)
    db.add(n)
    # Caller is responsible for committing


def get_razorpay_leave_type(local_leave_type: str) -> int:
    normalized = normalize_leave_type(local_leave_type)
    return RAZORPAY_LEAVE_TYPE_IDS.get(normalized, 0)


def post_razorpay_attendance(request_body: dict) -> str:
    razorpay_api_id = (os.getenv("RAZORPAY_API_ID") or "").strip()
    razorpay_api_key = (os.getenv("RAZORPAY_API_KEY") or "").strip()

    if not razorpay_api_id or not razorpay_api_key:
        raise HTTPException(
            status_code=500,
            detail="Razorpay payroll credentials are not configured on the backend",
        )

    request_body["auth"] = {
        "id": int(razorpay_api_id),
        "key": razorpay_api_key,
    }

    request = Request(
        "https://payroll.razorpay.com/api/att",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="ignore")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") or exc.reason
        raise HTTPException(status_code=502, detail=f"Razorpay leave sync failed: {detail}")
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"Razorpay leave sync failed: {exc.reason}")

    # Razorpay returns HTTP 200 even for business-logic errors — check the body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "error" in parsed:
            raise HTTPException(
                status_code=502,
                detail=f"Razorpay leave sync failed: {parsed['error']}",
            )
    except (ValueError, KeyError):
        pass  # non-JSON response is fine

    return body


def build_razorpay_attendance_request(employee: Employee, date_value, leave_type: str, remarks: str) -> dict:
    if not employee.razorpay_email:
        raise HTTPException(
            status_code=400,
            detail=f"Razorpay Email is missing for {employee.name}",
        )

    return {
        "request": {
            "type": "attendance",
            "sub-type": "modify",
        },
        "data": {
            "email": employee.razorpay_email,
            "employee-type": "employee",
            "date": date_value.isoformat(),
            "status": "leave",
            "leave-type": get_razorpay_leave_type(leave_type),
            "remarks": remarks,
        },
    }


def sync_leave_to_razorpay(employee: Employee, leave: Leave) -> None:
    remarks = leave.reason or f"{get_leave_type_label(leave.leave_type)} request from Autonex"
    current_date = leave.start_date

    while current_date <= leave.end_date:
        if not is_weekend(current_date) and not is_fixed_holiday(current_date):
            request_body = build_razorpay_attendance_request(
                employee=employee,
                date_value=current_date,
                leave_type=leave.leave_type,
                remarks=remarks,
            )
            post_razorpay_attendance(request_body)
        current_date += timedelta(days=1)


def build_razorpay_clear_request(employee: Employee, date_value, remarks: str) -> dict:
    """Build a request that resets a day's attendance back to 'present' (removes the leave)."""
    if not employee.razorpay_email:
        raise HTTPException(
            status_code=400,
            detail=f"Razorpay Email is missing for {employee.name}",
        )

    return {
        "request": {
            "type": "attendance",
            "sub-type": "modify",
        },
        "data": {
            "email": employee.razorpay_email,
            "employee-type": "employee",
            "date": date_value.isoformat(),
            "status": "present",
            "remarks": remarks,
        },
    }


def unsync_leave_from_razorpay(employee: Employee, leave: Leave) -> None:
    """Reverse a previously-synced leave in Razorpay by resetting each working day to 'present'.

    Mirrors sync_leave_to_razorpay so the same working days that were marked as leave are cleared.
    Raises HTTPException if Razorpay rejects the request — callers should reverse BEFORE mutating
    local state so the website and Razorpay never drift out of sync.
    """
    remarks = f"Leave cancelled in Autonex ({get_leave_type_label(leave.leave_type)})"
    current_date = leave.start_date

    while current_date <= leave.end_date:
        if not is_weekend(current_date) and not is_fixed_holiday(current_date):
            request_body = build_razorpay_clear_request(
                employee=employee,
                date_value=current_date,
                remarks=remarks,
            )
            post_razorpay_attendance(request_body)
        current_date += timedelta(days=1)


def _date_ranges_overlap(start_a, end_a, start_b, end_b) -> bool:
    return start_a <= end_b and start_b <= end_a


def _format_impacted_project_line(project: DailySheet, allocation: Allocation, sub_project: SubProject | None) -> str:
    sub_project_name = sub_project.name if sub_project else "Unmapped sub-project"
    hours = allocation.total_daily_hours or 0
    roles = ", ".join(allocation.role_tags or []) or "No role tags"
    return f"{project.name} ({sub_project_name}) - {hours}h/day - Roles: {roles}"


def _get_pm_notification_targets(db: Session, employee: Employee, leave: Leave) -> list[dict]:
    allocations = db.query(Allocation).filter(Allocation.employee_id == employee.id).all()
    if not allocations:
        return []

    project_ids = list({allocation.sub_project_id for allocation in allocations if allocation.sub_project_id})
    if not project_ids:
        return []

    projects = db.query(DailySheet).filter(DailySheet.id.in_(project_ids)).all()
    project_map = {project.id: project for project in projects}

    sub_project_ids = list({project.sub_project_id for project in projects if project.sub_project_id})
    sub_projects = db.query(SubProject).filter(SubProject.id.in_(sub_project_ids)).all() if sub_project_ids else []
    sub_project_map = {sub_project.id: sub_project for sub_project in sub_projects}

    main_project_ids = list({project.main_project_id for project in projects if project.main_project_id})
    main_projects = db.query(MainProject).filter(MainProject.id.in_(main_project_ids)).all() if main_project_ids else []
    main_project_map = {main_project.id: main_project for main_project in main_projects}

    pm_project_map: dict[int, list[str]] = {}
    for allocation in allocations:
        project = project_map.get(allocation.sub_project_id)
        if not project:
            continue

        allocation_start = allocation.active_start_date or project.start_date
        allocation_end = allocation.active_end_date or project.end_date
        if not allocation_start or not allocation_end:
            continue

        if not _date_ranges_overlap(leave.start_date, leave.end_date, allocation_start, allocation_end):
            continue

        sub_project = sub_project_map.get(project.sub_project_id) if project.sub_project_id else None
        main_project = main_project_map.get(project.main_project_id) if project.main_project_id else None
        pm_ids = {
            pm_id
            for pm_id in (
                getattr(sub_project, "pm_id", None),
                getattr(main_project, "program_manager_id", None),
            )
            if pm_id
        }
        if not pm_ids:
            continue

        project_line = _format_impacted_project_line(project, allocation, sub_project)
        for pm_id in pm_ids:
            pm_project_map.setdefault(pm_id, [])
            if project_line not in pm_project_map[pm_id]:
                pm_project_map[pm_id].append(project_line)

    if not pm_project_map:
        return []

    pm_employees = db.query(Employee).filter(Employee.id.in_(pm_project_map.keys())).all()
    notification_targets = []
    for pm_employee in pm_employees:
        slack_user_id = try_get_or_cache_employee_slack_user_id(db, pm_employee)
        if not slack_user_id:
            continue
        notification_targets.append(
            {
                "pm_employee": pm_employee,
                "pm_slack_user_id": slack_user_id,
                "impacted_projects": pm_project_map.get(pm_employee.id, []),
            }
        )

    return notification_targets


def _get_admin_notification_targets(db: Session) -> list[dict]:
    """Return Slack-reachable admin users to use as fallback when no PM is assigned."""
    from app.services.slack_service import lookup_user_id_by_email

    admin_users = (
        db.query(User)
        .filter(User.role == "admin", User.is_active == True)
        .all()
    )
    targets = []
    for admin_user in admin_users:
        # Prefer linked employee record for Slack lookup; fall back to user email
        slack_user_id = None
        admin_name = admin_user.name or admin_user.email

        if admin_user.employee_id:
            admin_employee = db.query(Employee).filter(Employee.id == admin_user.employee_id).first()
            if admin_employee:
                slack_user_id = try_get_or_cache_employee_slack_user_id(db, admin_employee)
                admin_name = admin_employee.name or admin_name

        if not slack_user_id:
            try:
                slack_user_id = lookup_user_id_by_email(admin_user.email)
            except Exception:
                pass

        if not slack_user_id:
            continue

        targets.append(
            {
                "pm_employee": type("_Admin", (), {"name": admin_name, "id": None})(),
                "pm_slack_user_id": slack_user_id,
                "impacted_projects": ["No PM assigned — routed to Admin"],
            }
        )
    return targets


def _leave_to_schema(leave: Leave) -> LeaveSchema:
    return LeaveSchema(
        leave_id=leave.id,
        employee_id=leave.employee_id,
        start_date=leave.start_date,
        end_date=leave.end_date,
        leave_type=leave.leave_type,
        reason=leave.reason,
        status=leave.status or "pending",
        approved_by=leave.approved_by,
        razorpay_applied=leave.razorpay_applied or False,
        flagged=leave.flagged or False,
        approval_remark=leave.approval_remark,
        is_emergency=leave.is_emergency or False,
        is_half_day=leave.is_half_day or False,
        half_day_slot=leave.half_day_slot,
        created_at=leave.created_at.isoformat() if leave.created_at else None,
        updated_at=leave.updated_at.isoformat() if leave.updated_at else None,
    )


@router.get("", response_model=List[LeaveSchema])
def get_all_leaves(
    employee_id: Optional[int] = None,
    start_date: Optional[date_type] = None,
    end_date: Optional[date_type] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all leaves, optionally filtered by employee_id, start_date, or end_date"""
    if current_user.role not in ["admin", "pm", "hr"]:
        if employee_id is None:
            employee_id = current_user.employee_id
            if employee_id is None:
                emp = db.query(Employee).filter(Employee.email == current_user.email).first()
                if emp:
                    employee_id = emp.id
                else:
                    raise HTTPException(status_code=403, detail="Access denied")
        else:
            is_self = current_user.employee_id == employee_id
            if not is_self:
                emp = db.query(Employee).filter(Employee.id == employee_id).first()
                if not emp or emp.email != current_user.email:
                    raise HTTPException(status_code=403, detail="Access denied")
    query = db.query(Leave)
    if employee_id:
        query = query.filter(Leave.employee_id == employee_id)
    if start_date:
        query = query.filter(Leave.end_date >= start_date)
    if end_date:
        query = query.filter(Leave.start_date <= end_date)

    leaves = query.order_by(Leave.id.desc()).all()
    return [
        LeaveSchema(
            leave_id=leave.id,
            employee_id=leave.employee_id,
            start_date=leave.start_date,
            end_date=leave.end_date,
            leave_type=leave.leave_type,
            reason=leave.reason,
            status=leave.status or "pending",
            approved_by=leave.approved_by,
            razorpay_applied=leave.razorpay_applied or False,
            flagged=leave.flagged or False,
            approval_remark=leave.approval_remark,
            is_emergency=leave.is_emergency or False,
            is_half_day=leave.is_half_day or False,
            half_day_slot=leave.half_day_slot,
            created_at=str(leave.created_at) if leave.created_at else None,
            updated_at=str(leave.updated_at) if leave.updated_at else None,
        )
        for leave in leaves
    ]


@router.get("/calendar", response_model=dict)
def get_calendar(
    month: str = Query(..., description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """
    Returns all leaves and WFH requests for a given month.
    month: YYYY-MM format
    """
    from app.models.wfh import WFHRequest
    try:
        year, mo = int(month[:4]), int(month[5:7])
        month_start = date_type(year, mo, 1)
        end_mo = mo + 1 if mo < 12 else 1
        end_yr = year if mo < 12 else year + 1
        month_end = date_type(end_yr, end_mo, 1)
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM format")

    leaves = db.query(Leave).filter(
        Leave.status != "rejected",
        Leave.start_date < month_end,
        Leave.end_date >= month_start,
    ).all()

    wfh_requests = db.query(WFHRequest).filter(
        WFHRequest.status != "rejected",
        WFHRequest.wfh_date >= month_start,
        WFHRequest.wfh_date < month_end,
    ).all()

    emp_ids = list({l.employee_id for l in leaves} | {w.employee_id for w in wfh_requests})
    employees = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(emp_ids)).all()}

    leave_events = []
    for leave in leaves:
        emp = employees.get(leave.employee_id)
        leave_events.append({
            "id": leave.id,
            "type": "leave",
            "leave_type": leave.leave_type,
            "employee_id": leave.employee_id,
            "employee_name": emp.name if emp else "Unknown",
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
            "status": leave.status,
            "reason": leave.reason,
            "flagged": leave.flagged or False,
            "is_half_day": leave.is_half_day or False,
            "half_day_slot": leave.half_day_slot,
        })

    wfh_events = []
    for wfh in wfh_requests:
        emp = employees.get(wfh.employee_id)
        wfh_events.append({
            "id": wfh.id,
            "type": "wfh",
            "employee_id": wfh.employee_id,
            "employee_name": emp.name if emp else "Unknown",
            "date": wfh.wfh_date.isoformat(),
            "status": wfh.status,
            "reason": wfh.reason,
        })

    return {"month": month, "leaves": leave_events, "wfh": wfh_events}


@router.get("/{leave_id}", response_model=LeaveSchema)
def get_leave(
    leave_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    check_leave_access(leave.employee_id, current_user, db)
    return LeaveSchema(
        leave_id=leave.id,
        employee_id=leave.employee_id,
        start_date=leave.start_date,
        end_date=leave.end_date,
        leave_type=leave.leave_type,
        reason=leave.reason,
        status=leave.status or "pending",
        approved_by=leave.approved_by,
        razorpay_applied=leave.razorpay_applied or False,
        is_emergency=leave.is_emergency or False,
        is_half_day=leave.is_half_day or False,
        half_day_slot=leave.half_day_slot,
        created_at=str(leave.created_at) if leave.created_at else None,
        updated_at=str(leave.updated_at) if leave.updated_at else None,
    )


def validate_consecutive_leaves(
    employee_id: int,
    start_date: date_type,
    end_date: date_type,
    db: Session,
    exclude_leave_id: Optional[int] = None,
    is_half_day: bool = False
) -> None:
    if is_half_day:
        return

    # Define window of 10 days before and after the requested range
    window_start = start_date - timedelta(days=10)
    window_end = end_date + timedelta(days=10)

    # Query existing non-rejected leaves of this employee in the window
    query = db.query(Leave).filter(
        Leave.employee_id == employee_id,
        Leave.status != "rejected",
        Leave.start_date <= window_end,
        Leave.end_date >= window_start
    )
    if exclude_leave_id:
        query = query.filter(Leave.id != exclude_leave_id)
        
    existing_leaves = query.all()

    # Collect all working days of existing leaves + new leave
    leave_working_days = set()
    
    # Add new leave's working days
    cur = start_date
    while cur <= end_date:
        if not is_weekend(cur) and not is_fixed_holiday(cur):
            leave_working_days.add(cur)
        cur += timedelta(days=1)

    # Add existing leaves' working days
    for l in existing_leaves:
        if getattr(l, "is_half_day", False) or l.leave_type in ("first_half", "second_half"):
            continue
        if l.start_date is None or l.end_date is None:
            continue
        cur = max(l.start_date, window_start)
        l_end = min(l.end_date, window_end)
        while cur <= l_end:
            if not is_weekend(cur) and not is_fixed_holiday(cur):
                leave_working_days.add(cur)
            cur += timedelta(days=1)

    # Loop day-by-day in the window and track consecutive working day run
    consecutive_run = 0
    cur = window_start
    while cur <= window_end:
        if not is_weekend(cur) and not is_fixed_holiday(cur):
            if cur in leave_working_days:
                consecutive_run += 1
                if consecutive_run >= 5:
                    raise HTTPException(
                        status_code=400,
                        detail="Safe guard triggered: You cannot apply for 5 or more consecutive leaves."
                    )
            else:
                consecutive_run = 0
        cur += timedelta(days=1)



@router.post("", response_model=LeaveSchema, status_code=201)
def create_leave(
    payload: LeaveCreate,
    http_request: HTTPRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    check_leave_access(payload.employee_id, current_user, db)
    employee = db.query(Employee).filter(Employee.id == payload.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if (
        current_user.role not in ["admin", "hr"]
        and not payload.is_emergency
        and payload.start_date < date_type.today()
    ):
        raise HTTPException(
            status_code=400,
            detail="Employees and PMs cannot apply for leaves on past dates unless marked as Emergency Leave."
        )

    if payload.is_half_day:
        validate_half_day_timing(payload.start_date, payload.half_day_slot)

    # Validate consecutive leaves safeguard
    validate_consecutive_leaves(payload.employee_id, payload.start_date, payload.end_date, db, is_half_day=payload.is_half_day)

    # Reject if any existing leave (pending or approved) overlaps the requested range
    overlap = (
        db.query(Leave)
        .filter(
            Leave.employee_id == payload.employee_id,
            Leave.status != "rejected",
            Leave.start_date <= payload.end_date,
            Leave.end_date >= payload.start_date,
        )
        .first()
    )
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=f"A leave already exists for this period ({overlap.start_date} – {overlap.end_date}). Please check your existing leaves.",
        )

    # Reject if the entire range contains no working days
    working_day_count = sum(
        1 for i in range((payload.end_date - payload.start_date).days + 1)
        if not is_weekend(payload.start_date + timedelta(days=i))
        and not is_fixed_holiday(payload.start_date + timedelta(days=i))
    )
    if payload.is_half_day:
        working_day_count = 0.5 if working_day_count > 0 else 0.0

    if working_day_count == 0:
        raise HTTPException(
            status_code=400,
            detail="The selected date range contains no working days. Please choose dates that include at least one working day.",
        )

    # Floater leave date validation
    normalized_type = normalize_leave_type(payload.leave_type)
    if normalized_type == "floater":
        check_date = payload.start_date
        while check_date <= payload.end_date:
            if not is_valid_floater_date(check_date):
                approved = sorted(get_floater_dates_for_year(payload.start_date.year))
                if not approved:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Floater leave is not available for {payload.start_date.year}. No approved floater dates have been configured.",
                    )
                approved_str = ", ".join(str(d) for d in approved)
                raise HTTPException(
                    status_code=400,
                    detail=f"{check_date} is not an approved floater holiday date. Approved dates for {check_date.year}: {approved_str}",
                )
            check_date += timedelta(days=1)

    # Monthly paid leave limit: max 2 paid leaves per calendar month
    flagged = False
    if payload.leave_type == "paid":
        month_start = payload.start_date.replace(day=1)
        end_mo = payload.start_date.month + 1 if payload.start_date.month < 12 else 1
        end_yr = payload.start_date.year if payload.start_date.month < 12 else payload.start_date.year + 1
        month_end = date_type(end_yr, end_mo, 1)
        paid_this_month = (
            db.query(Leave)
            .filter(
                Leave.employee_id == payload.employee_id,
                Leave.leave_type == "paid",
                Leave.status != "rejected",
                Leave.start_date >= month_start,
                Leave.start_date < month_end,
            )
            .count()
        )
        limit = 1 if is_intern_or_contractor(employee.employee_type) else 2
        if paid_this_month >= limit:
            flagged = True

    leave = Leave(
        employee_id=payload.employee_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        leave_type=payload.leave_type,
        reason=payload.reason,
        is_emergency=payload.is_emergency or False,
        status="pending",
        flagged=flagged,
        is_half_day=payload.is_half_day or False,
        half_day_slot=payload.half_day_slot,
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)

    # Recorded after the refresh because leave.id only exists once the insert lands.
    # Actor and subject genuinely differ here: an admin can file a leave on someone
    # else's behalf, and the log needs to show both people.
    duration = (
        f"{leave.start_date} → {leave.end_date}"
        if leave.start_date != leave.end_date
        else str(leave.start_date)
    )
    audit_service.record(
        db,
        actor=current_user,
        action="leave.applied",
        category="Leaves",
        action_type="Applied",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Leave type", None, get_leave_type_label(leave.leave_type)),
            audit_service.field_diff("Dates", None, duration),
            audit_service.field_diff("Half day", None, leave.half_day_slot if leave.is_half_day else None),
            audit_service.field_diff("Reason", None, leave.reason),
            audit_service.field_diff("Status", None, leave.status),
            audit_service.field_diff("Over monthly limit", None, True if leave.flagged else None),
        ),
        summary=(
            f"Applied for {get_leave_type_label(leave.leave_type)} leave"
            + (
                f" on behalf of {employee.name}"
                if current_user.employee_id != employee.id
                else ""
            )
            + f" ({duration})"
        ),
        request=http_request,
    )

    # In-app notification: employee who applied
    emp_user = db.query(User).filter(User.employee_id == employee.id).first()
    if emp_user:
        _push_notification(
            db, emp_user.id,
            "Leave request submitted",
            f"Your {get_leave_type_label(leave.leave_type)} request ({leave.start_date} – {leave.end_date}) has been submitted and is pending approval.",
            "leave_applied",
        )
        db.commit()

    employee.slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)

    try_send_leave_applied_message(
        employee_name=employee.name,
        employee_email=employee.email,
        leave_type=get_leave_type_label(leave.leave_type),
        start_date=leave.start_date.isoformat(),
        end_date=leave.end_date.isoformat(),
    )

    duration_days = 0.5 if leave.is_half_day else (leave.end_date - leave.start_date).days + 1
    # PMs route their own leave requests straight to Admin for approval. Everyone
    # else routes to the PM(s) of their allocated projects, falling back to Admin.
    is_pm_applicant = emp_user is not None and emp_user.role == "pm"
    pm_targets = [] if is_pm_applicant else _get_pm_notification_targets(db, employee, leave)
    notification_targets = pm_targets if pm_targets else _get_admin_notification_targets(db)
    notified_user_ids: set[int] = set()
    for target in notification_targets:
        try_send_pm_leave_request_message(
            pm_slack_user_id=target["pm_slack_user_id"],
            pm_name=target["pm_employee"].name,
            employee_name=employee.name,
            employee_email=employee.email,
            employee_designation=employee.designation,
            leave_type=get_leave_type_label(leave.leave_type),
            start_date=leave.start_date.isoformat(),
            end_date=leave.end_date.isoformat(),
            duration_days=duration_days,
            reason=leave.reason,
            impacted_projects=target["impacted_projects"],
        )
        # In-app notification for PM (real PM only — admin fallback handled below)
        pm_emp_id = getattr(target["pm_employee"], "id", None)
        if pm_emp_id:
            pm_user = db.query(User).filter(User.employee_id == pm_emp_id).first()
            if pm_user and pm_user.id not in notified_user_ids:
                notified_user_ids.add(pm_user.id)
                _push_notification(
                    db, pm_user.id,
                    f"New leave request from {employee.name}",
                    f"{employee.name} has requested {get_leave_type_label(leave.leave_type)} leave from {leave.start_date} to {leave.end_date}.",
                    "leave_applied",
                )

    # Admin fallback: notify each admin exactly once (regardless of Slack-reachable count)
    if not pm_targets:
        for admin_user in db.query(User).filter(User.role == "admin", User.is_active == True).all():
            if admin_user.id not in notified_user_ids:
                notified_user_ids.add(admin_user.id)
                _push_notification(
                    db, admin_user.id,
                    f"New leave request from {employee.name}",
                    f"{employee.name} has requested {get_leave_type_label(leave.leave_type)} leave from {leave.start_date} to {leave.end_date} (no PM assigned).",
                    "leave_applied",
                )
    db.commit()
    db.refresh(leave)

    return LeaveSchema(
        leave_id=leave.id,
        employee_id=leave.employee_id,
        start_date=leave.start_date,
        end_date=leave.end_date,
        leave_type=leave.leave_type,
        reason=leave.reason,
        status=leave.status or "pending",
        approved_by=leave.approved_by,
        razorpay_applied=leave.razorpay_applied or False,
        flagged=leave.flagged or False,
        approval_remark=leave.approval_remark,
        is_emergency=leave.is_emergency or False,
        is_half_day=leave.is_half_day or False,
        half_day_slot=leave.half_day_slot,
        created_at=str(leave.created_at) if leave.created_at else None,
        updated_at=str(leave.updated_at) if leave.updated_at else None,
    )


class ApproveBody(BaseModel):
    remark: Optional[str] = None


@router.post("/{leave_id}/apply-to-razorpay")
def apply_leave_to_razorpay(
    leave_id: int,
    http_request: HTTPRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")

    if getattr(leave, "is_half_day", False):
        raise HTTPException(status_code=400, detail="Half-day leaves do not sync to Razorpay")

    if (leave.status or "pending") != "approved":
        raise HTTPException(status_code=400, detail="Only approved leaves can be applied to Razorpay")
    if leave.razorpay_applied:
        raise HTTPException(status_code=400, detail="Leave has already been applied to Razorpay")

    employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    sync_leave_to_razorpay(employee, leave)
    leave.razorpay_applied = True

    # This pushes attendance into the external payroll system, so it affects what the
    # person is actually paid — worth recording as its own action, not just a flag flip.
    audit_service.record(
        db,
        actor=current_user,
        action="leave.razorpay_synced",
        category="Leaves",
        action_type="Updated",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Razorpay applied", False, True),
            audit_service.field_diff("Dates", None, f"{leave.start_date} → {leave.end_date}"),
        ),
        summary=(
            f"Pushed {get_leave_type_label(leave.leave_type)} leave for "
            f"{employee.name} ({leave.start_date} → {leave.end_date}) to Razorpay payroll"
        ),
        request=http_request,
    )

    db.commit()
    return {"message": "Leave submitted to Razorpay", "leave_id": leave_id}


@router.put("/{leave_id}", response_model=LeaveSchema)
def update_leave(
    leave_id: int,
    payload: LeaveCreate,
    http_request: HTTPRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    check_leave_access(leave.employee_id, current_user, db)
    check_leave_access(payload.employee_id, current_user, db)
    if current_user.role not in ["admin", "hr"] and leave.start_date <= date_type.today():
        raise HTTPException(status_code=400, detail="Cannot edit a leave that has already started")

    if (
        current_user.role not in ["admin", "hr"]
        and not payload.is_emergency
        and payload.start_date < date_type.today()
    ):
        raise HTTPException(
            status_code=400,
            detail="Employees and PMs cannot update leaves to past dates unless marked as Emergency Leave."
        )

    if payload.is_half_day:
        validate_half_day_timing(payload.start_date, payload.half_day_slot)

    # Validate consecutive leaves safeguard (excluding this leave ID)
    validate_consecutive_leaves(payload.employee_id, payload.start_date, payload.end_date, db, exclude_leave_id=leave_id, is_half_day=payload.is_half_day)

    # Check for overlapping leaves (excluding this one)
    overlap = (
        db.query(Leave)
        .filter(
            Leave.employee_id == leave.employee_id,
            Leave.id != leave_id,
            Leave.status != "rejected",
            Leave.start_date <= payload.end_date,
            Leave.end_date >= payload.start_date,
        )
        .first()
    )
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=f"A leave already exists for this period ({overlap.start_date} – {overlap.end_date}).",
        )

    # Reject if the entire range contains no working days
    working_day_count = sum(
        1 for i in range((payload.end_date - payload.start_date).days + 1)
        if not is_weekend(payload.start_date + timedelta(days=i))
        and not is_fixed_holiday(payload.start_date + timedelta(days=i))
    )
    if payload.is_half_day:
        working_day_count = 0.5 if working_day_count > 0 else 0.0

    if working_day_count == 0:
        raise HTTPException(
            status_code=400,
            detail="The selected date range contains no working days. Please choose dates that include at least one working day.",
        )

    # Floater leave date validation
    normalized_type = normalize_leave_type(payload.leave_type)
    if normalized_type == "floater":
        check_date = payload.start_date
        while check_date <= payload.end_date:
            if not is_valid_floater_date(check_date):
                approved = sorted(get_floater_dates_for_year(payload.start_date.year))
                if not approved:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Floater leave is not available for {payload.start_date.year}. No approved floater dates have been configured.",
                    )
                approved_str = ", ".join(str(d) for d in approved)
                raise HTTPException(
                    status_code=400,
                    detail=f"{check_date} is not an approved floater holiday date. Approved dates for {check_date.year}: {approved_str}",
                )
            check_date += timedelta(days=1)

    # If this leave was already pushed to Razorpay, clear the ORIGINAL dates there before
    # editing. It returns to pending and will be re-synced when re-approved. Reverse first so
    # a Razorpay failure aborts the edit and the two systems never drift apart.
    if leave.razorpay_applied:
        employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")
        unsync_leave_from_razorpay(employee, leave)  # uses the current (pre-edit) dates
        leave.razorpay_applied = False

    before = audit_service.snapshot(
        leave,
        [
            "start_date", "end_date", "leave_type", "reason",
            "is_emergency", "status", "is_half_day", "half_day_slot",
        ],
    )

    leave.start_date = payload.start_date
    leave.end_date = payload.end_date
    leave.leave_type = payload.leave_type
    leave.reason = payload.reason
    leave.is_emergency = payload.is_emergency or False
    leave.status = "pending"  # reset to pending so PM re-reviews the edited request
    leave.is_half_day = payload.is_half_day or False
    leave.half_day_slot = payload.half_day_slot

    edited_employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    audit_service.record(
        db,
        actor=current_user,
        action="leave.updated",
        category="Leaves",
        action_type="Updated",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=edited_employee.name if edited_employee else None,
        subject_employee_id=leave.employee_id,
        subject_name=edited_employee.name if edited_employee else None,
        details=audit_service.diff_all(
            before,
            leave,
            {
                "start_date": "Start date",
                "end_date": "End date",
                "leave_type": "Leave type",
                "reason": "Reason",
                "is_emergency": "Emergency",
                "status": "Status",
                "is_half_day": "Half day",
                "half_day_slot": "Half-day slot",
            },
        ),
        summary=(
            f"Edited {get_leave_type_label(leave.leave_type)} leave for "
            f"{edited_employee.name if edited_employee else 'employee'} — "
            f"reset to pending for re-approval"
        ),
        request=http_request,
    )

    db.commit()
    db.refresh(leave)
    return LeaveSchema(
        leave_id=leave.id,
        employee_id=leave.employee_id,
        start_date=leave.start_date,
        end_date=leave.end_date,
        leave_type=leave.leave_type,
        reason=leave.reason,
        status=leave.status or "pending",
        approved_by=leave.approved_by,
        razorpay_applied=leave.razorpay_applied or False,
        flagged=leave.flagged or False,
        approval_remark=leave.approval_remark,
        is_emergency=leave.is_emergency or False,
        is_half_day=leave.is_half_day or False,
        half_day_slot=leave.half_day_slot,
        created_at=str(leave.created_at) if leave.created_at else None,
        updated_at=str(leave.updated_at) if leave.updated_at else None,
    )


# ── Approve / Reject ───────────────────────────────────────────────

@router.patch("/{leave_id}/approve")
def approve_leave(
    leave_id: int,
    http_request: HTTPRequest,
    approved_by: int = Query(default=0, deprecated=True),
    body: ApproveBody = Body(default=ApproveBody()),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Approve a leave request. The approver is taken from the authenticated session.
    Flagged leaves (exceeding monthly limit) require a remark in the request body.

    ``approved_by`` is still accepted so existing callers keep working, but it is
    ignored — trusting it let any approver attribute a decision to someone else."""
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")

    if leave.flagged and not (body.remark and body.remark.strip()):
        raise HTTPException(
            status_code=422,
            detail="This leave exceeds the monthly paid leave limit (2/month). A justification remark is required to approve it.",
        )

    employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Snapshot the old values before mutating — otherwise the audit diff would
    # compare the new value against itself.
    previous_status = leave.status
    previous_remark = leave.approval_remark

    # Approve the leave first — always succeeds regardless of Razorpay
    leave.status = "approved"
    leave.approved_by = current_user.id
    if body.remark:
        leave.approval_remark = body.remark.strip()

    # Attempt Razorpay sync; if it fails, leave razorpay_applied=False for later retry
    sync_warning = None
    if not leave.razorpay_applied and not getattr(leave, "is_half_day", False):
        try:
            sync_leave_to_razorpay(employee, leave)
            leave.razorpay_applied = True
        except HTTPException as exc:
            sync_warning = exc.detail
        except Exception as exc:
            sync_warning = str(exc)

    leave_label = get_leave_type_label(leave.leave_type)
    audit_service.record(
        db,
        actor=current_user,
        action="leave.approved",
        category="Leaves",
        action_type="Approved",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "approved"),
            audit_service.field_diff("Remark", previous_remark, leave.approval_remark),
        ),
        summary=(
            f"Approved {leave_label} leave for {employee.name} "
            f"({leave.start_date} → {leave.end_date})"
        ),
        request=http_request,
    )

    db.commit()
    employee.slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)

    pm_name = current_user.name or "your PM"

    # In-app notification: employee
    emp_user = db.query(User).filter(User.employee_id == employee.id).first()
    if emp_user:
        _push_notification(
            db, emp_user.id,
            "Leave approved",
            f"Your {get_leave_type_label(leave.leave_type)} leave from {leave.start_date} to {leave.end_date} has been approved by {pm_name}.",
            "leave_approved",
        )
        db.commit()

    try_send_leave_status_message(
        employee_email=employee.email,
        employee_name=employee.name,
        start_date=leave.start_date.isoformat(),
        end_date=leave.end_date.isoformat(),
        pm_name=pm_name,
        approved=True,
    )

    msg = "Leave approved"
    if getattr(leave, "is_half_day", False):
        msg = "Leave approved (half-day leaves do not sync to Razorpay)"
    elif leave.razorpay_applied:
        msg = "Leave approved and synced to Razorpay"
    else:
        msg = "Leave approved (Razorpay sync pending — use 'Apply to Razorpay' to retry)"

    result = {
        "message": msg,
        "leave_id": leave_id,
        "status": "approved",
        "razorpay_applied": leave.razorpay_applied or False,
    }
    if sync_warning:
        result["sync_warning"] = sync_warning
    return result


@router.patch("/{leave_id}/reject")
def reject_leave(
    leave_id: int,
    http_request: HTTPRequest,
    approved_by: int = Query(default=0, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Reject a leave request. Rejecter comes from the session; ``approved_by`` is ignored."""
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")

    employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # If a previously-approved leave was already pushed to Razorpay, reverse it there
    # before rejecting so the two systems stay in sync. A Razorpay failure aborts the reject.
    if leave.razorpay_applied:
        unsync_leave_from_razorpay(employee, leave)
        leave.razorpay_applied = False

    previous_status = leave.status
    leave.status = "rejected"
    leave.approved_by = current_user.id

    audit_service.record(
        db,
        actor=current_user,
        action="leave.rejected",
        category="Leaves",
        action_type="Rejected",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "rejected"),
        ),
        summary=(
            f"Rejected {get_leave_type_label(leave.leave_type)} leave for "
            f"{employee.name} ({leave.start_date} → {leave.end_date})"
        ),
        request=http_request,
    )

    db.commit()
    employee.slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)

    pm_name = current_user.name or "your PM"

    # In-app notification: employee
    emp_user = db.query(User).filter(User.employee_id == employee.id).first()
    if emp_user:
        _push_notification(
            db, emp_user.id,
            "Leave declined",
            f"Your {get_leave_type_label(leave.leave_type)} leave from {leave.start_date} to {leave.end_date} was declined by {pm_name}.",
            "leave_rejected",
        )
        db.commit()

    try_send_leave_status_message(
        employee_email=employee.email,
        employee_name=employee.name,
        start_date=leave.start_date.isoformat(),
        end_date=leave.end_date.isoformat(),
        pm_name=pm_name,
        approved=False,
    )

    return {"message": "Leave rejected", "leave_id": leave_id, "status": "rejected"}


@router.patch("/{leave_id}/undo-reject")
def undo_reject_leave(
    leave_id: int,
    http_request: HTTPRequest,
    approved_by: int = Query(default=0, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Reopen a rejected leave back to pending. ``approved_by`` is ignored."""
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    if leave.status != "rejected":
        raise HTTPException(status_code=400, detail=f"Leave is not rejected (status: {leave.status})")

    # Re-run the same overlap / consecutive-day guards create_leave applies, since other
    # non-rejected leaves may have been legitimately created in this window while this one
    # was rejected (rejected leaves don't block). Reopening it must not silently double-book.
    overlap = (
        db.query(Leave)
        .filter(
            Leave.employee_id == leave.employee_id,
            Leave.id != leave_id,
            Leave.status != "rejected",
            Leave.start_date <= leave.end_date,
            Leave.end_date >= leave.start_date,
        )
        .first()
    )
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reopen — it now overlaps another active leave ({overlap.start_date} – {overlap.end_date}).",
        )
    validate_consecutive_leaves(
        leave.employee_id, leave.start_date, leave.end_date, db,
        exclude_leave_id=leave_id, is_half_day=getattr(leave, "is_half_day", False),
    )

    previous_status = leave.status
    leave.status = "pending"
    leave.approved_by = current_user.id

    reopened_employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    audit_service.record(
        db,
        actor=current_user,
        action="leave.reject_undone",
        category="Leaves",
        action_type="Restored",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=reopened_employee.name if reopened_employee else None,
        subject_employee_id=leave.employee_id,
        subject_name=reopened_employee.name if reopened_employee else None,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "pending"),
        ),
        summary=(
            f"Reopened a rejected {get_leave_type_label(leave.leave_type)} leave for "
            f"{reopened_employee.name if reopened_employee else 'employee'} "
            f"({leave.start_date} → {leave.end_date}) — back to pending"
        ),
        request=http_request,
    )

    db.commit()

    emp_user = db.query(User).filter(User.employee_id == leave.employee_id).first()
    if emp_user:
        _push_notification(
            db, emp_user.id,
            "Leave request reopened",
            f"Your {get_leave_type_label(leave.leave_type)} leave from {leave.start_date} to {leave.end_date} has been reopened and is pending approval.",
            "leave_applied",
        )
        db.commit()

    return {"message": "Leave reopened", "leave_id": leave_id, "status": "pending"}


@router.patch("/{leave_id}/undo-approve")
def undo_approve_leave(
    leave_id: int,
    http_request: HTTPRequest,
    approved_by: int = Query(default=0, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Revoke an approval, reverting the leave back to pending. Reverses the Razorpay
    sync first (if applied) so the two systems never drift out of sync.
    ``approved_by`` is ignored — the revoker comes from the session."""
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    if leave.status != "approved":
        raise HTTPException(status_code=400, detail=f"Leave is not approved (status: {leave.status})")

    if leave.razorpay_applied:
        employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")
        unsync_leave_from_razorpay(employee, leave)
        leave.razorpay_applied = False

    previous_status = leave.status
    previous_remark = leave.approval_remark
    leave.status = "pending"
    leave.approved_by = current_user.id
    leave.approval_remark = None

    revoked_employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    audit_service.record(
        db,
        actor=current_user,
        action="leave.approval_revoked",
        category="Leaves",
        action_type="Restored",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=revoked_employee.name if revoked_employee else None,
        subject_employee_id=leave.employee_id,
        subject_name=revoked_employee.name if revoked_employee else None,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "pending"),
            audit_service.field_diff("Remark", previous_remark, None),
        ),
        summary=(
            f"Revoked approval of {get_leave_type_label(leave.leave_type)} leave for "
            f"{revoked_employee.name if revoked_employee else 'employee'} "
            f"({leave.start_date} → {leave.end_date}) — back to pending"
        ),
        request=http_request,
    )

    db.commit()

    emp_user = db.query(User).filter(User.employee_id == leave.employee_id).first()
    if emp_user:
        _push_notification(
            db, emp_user.id,
            "Leave approval revoked",
            f"Your {get_leave_type_label(leave.leave_type)} leave from {leave.start_date} to {leave.end_date} is back to pending — approval was revoked.",
            "leave_applied",
        )
        db.commit()

    return {"message": "Leave approval revoked, reverted to pending", "leave_id": leave_id, "status": "pending"}


@router.delete("/{leave_id}")
def delete_leave(
    leave_id: int,
    http_request: HTTPRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    leave = db.query(Leave).filter(Leave.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    check_leave_access(leave.employee_id, current_user, db)
    if leave.start_date <= date_type.today() and current_user.role not in ["admin", "hr"]:
        raise HTTPException(status_code=400, detail="Cannot delete a leave that has already started")

    # If this leave was pushed to Razorpay, reverse it there FIRST. If Razorpay rejects,
    # the raised HTTPException aborts the delete so the website and Razorpay stay in sync.
    if leave.razorpay_applied:
        employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")
        unsync_leave_from_razorpay(employee, leave)

    # Everything worth keeping is copied out before the delete — once the row is gone
    # there is nothing left to describe it, and a deletion with no detail is the least
    # useful entry an audit log can hold.
    deleted_employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    deleted_name = deleted_employee.name if deleted_employee else None
    audit_service.record(
        db,
        actor=current_user,
        action="leave.deleted",
        category="Leaves",
        action_type="Deleted",
        entity_type="leave",
        entity_id=leave.id,
        entity_name=deleted_name,
        subject_employee_id=leave.employee_id,
        subject_name=deleted_name,
        details=audit_service.changes(
            audit_service.field_diff("Leave type", get_leave_type_label(leave.leave_type), None),
            audit_service.field_diff("Dates", f"{leave.start_date} → {leave.end_date}", None),
            audit_service.field_diff("Status at deletion", leave.status, None),
        ),
        summary=(
            f"Deleted {get_leave_type_label(leave.leave_type)} leave for "
            f"{deleted_name or 'employee'} ({leave.start_date} → {leave.end_date}), "
            f"was {leave.status}"
        ),
        request=http_request,
    )

    db.delete(leave)
    db.commit()
    return {"message": "Leave deleted successfully"}
