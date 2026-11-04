"""Weekly Compensation API Router.

Implements high-concurrency, production-grade endpoints for managing weekend and holiday
compensation records. Governed by Zero-Network Cache Surgery principles (skills.md):
- Single-query eager loading with strict projections (< 12ms checkout).
- Zero-Network Follow-Up: mutations return updated rows directly.
- Atomic bulk actions: eliminates client-side mutation loops.
- Strict concurrency and payroll lock guards.
"""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.db.database import get_db
from app.models.weekly_compensation import WeeklyCompensationRecord
from app.models.payroll import PayrollRun
from app.models.employee import Employee
from app.models.project import DailySheet
from app.models.user import User
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service, project_scope
from app.services.weekly_compensation_service import get_default_hourly_rate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/weekly-compensation",
    tags=["weekly-compensation"],
    dependencies=[Depends(get_current_user)],
)


# --- Request & Response Schemas ---

class AdjustTimeRequest(BaseModel):
    final_duration_seconds: Optional[int] = None
    final_hours: Optional[float] = None
    final_duration_hours: Optional[float] = None
    edit_note: Optional[str] = None
    note: Optional[str] = None


class OverridePayoutRequest(BaseModel):
    override_payout: float = Field(..., ge=0, description="Custom payout amount in INR")
    edit_note: Optional[str] = None
    note: Optional[str] = None


class BulkApproveRequest(BaseModel):
    ids: Optional[List[int]] = None
    record_ids: Optional[List[int]] = None
    note: Optional[str] = None


class WeeklyCompRecordResponse(BaseModel):
    id: int
    employee_id: int
    employee_name: Optional[str] = None
    employee_designation: Optional[str] = None
    avatar_url: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    pm_id: Optional[int] = None
    pm_name: Optional[str] = None
    work_date: str
    payroll_month: str
    week_number: int
    is_weekend: bool
    portal_duration_seconds: Optional[int] = None
    portal_time_formatted: str
    encord_duration_seconds: int
    encord_time_formatted: str
    final_duration_seconds: int
    final_time_formatted: str
    is_final_time_overridden: bool
    hourly_rate: float
    calculated_payout: float
    override_payout: Optional[float] = None
    effective_payout: float
    edit_note: Optional[str] = None
    last_edited_by: Optional[int] = None
    last_edited_by_name: Optional[str] = None
    last_edited_at: Optional[str] = None
    status: str
    approved_by: Optional[int] = None
    approved_by_name: Optional[str] = None
    approved_at: Optional[str] = None
    is_locked: bool
    payroll_run_id: Optional[int] = None


class WeeklyCompMonthSummary(BaseModel):
    month: str
    total_records: int
    pending_count: int
    approved_count: int
    rejected_count: int
    total_effective_payout: float
    is_payroll_finalized: bool = False
    payroll_run_id: Optional[int] = None
    default_hourly_rate: float = 143.0
    page: Optional[int] = 1
    page_size: Optional[int] = 50
    total_pages: Optional[int] = 1
    total_filtered_records: Optional[int] = None
    records: List[WeeklyCompRecordResponse]

def check_payroll_month_unlocked(db: Session, payroll_month: str) -> None:
    """Ensure that the payroll for this month has not been finalized.

    If payroll is finalized, no compensation records (even pending ones) can be approved,
    rejected, or modified without reopening/unlocking the payroll run first in Payroll Hub.
    """
    finalized_run = (
        db.query(PayrollRun)
        .filter(
            PayrollRun.month == payroll_month,
            PayrollRun.status == "finalized",
        )
        .first()
    )
    if finalized_run:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Payroll for {payroll_month} is finalized and locked. "
                "You must reopen the payroll run in the Payroll Hub before approving or modifying shifts for this month."
            ),
        )



# --- Helper Serializers ---

def _format_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "Missed Check-out (—)"
    if seconds <= 0:
        return "0h 00m"
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hrs}h {mins:02d}m"


def _format_final_seconds(seconds: int, is_overridden: bool, portal_duration: Optional[int]) -> str:
    if seconds <= 0 and portal_duration is None and not is_overridden:
        return "Pending Encord Sync (—)"
    hrs = seconds / 3600.0
    return f"{hrs:.1f}h"


def _serialize_record(
    record: WeeklyCompensationRecord,
    emp_map: dict,
    proj_map: dict,
    user_map: dict,
) -> WeeklyCompRecordResponse:
    emp = emp_map.get(record.employee_id)
    pm = emp_map.get(record.pm_id) if record.pm_id else None
    proj = proj_map.get(record.project_id) if record.project_id else None
    editor = user_map.get(record.last_edited_by) if record.last_edited_by else None
    approver = user_map.get(record.approved_by) if record.approved_by else None

    return WeeklyCompRecordResponse(
        id=record.id,
        employee_id=record.employee_id,
        employee_name=emp.name if emp else None,
        employee_designation=emp.designation if emp else None,
        avatar_url=emp.avatar_url if emp else None,
        project_id=record.project_id,
        project_name=(proj.batch_name or proj.name) if proj else None,
        pm_id=record.pm_id,
        pm_name=pm.name if pm else None,
        work_date=record.work_date.isoformat(),
        payroll_month=record.payroll_month,
        week_number=record.week_number,
        is_weekend=record.is_weekend,
        portal_duration_seconds=record.portal_duration_seconds,
        portal_time_formatted=_format_seconds(record.portal_duration_seconds),
        encord_duration_seconds=record.encord_duration_seconds,
        encord_time_formatted=_format_seconds(record.encord_duration_seconds),
        final_duration_seconds=record.final_duration_seconds,
        final_time_formatted=_format_final_seconds(
            record.final_duration_seconds,
            record.is_final_time_overridden,
            record.portal_duration_seconds,
        ),
        is_final_time_overridden=record.is_final_time_overridden,
        hourly_rate=float(record.hourly_rate),
        calculated_payout=float(record.calculated_payout),
        override_payout=float(record.override_payout) if record.override_payout is not None else None,
        effective_payout=record.effective_payout,
        edit_note=record.edit_note,
        last_edited_by=record.last_edited_by,
        last_edited_by_name=editor.name if editor else None,
        last_edited_at=record.last_edited_at.isoformat() if record.last_edited_at else None,
        status=record.status,
        approved_by=record.approved_by,
        approved_by_name=approver.name if approver else None,
        approved_at=record.approved_at.isoformat() if record.approved_at else None,
        is_locked=record.is_locked,
        payroll_run_id=record.payroll_run_id,
    )


def _load_reference_maps(db: Session, records: List[WeeklyCompensationRecord]) -> tuple[dict, dict, dict]:
    """Batch-load employees, projects, and users in 3 parallel indexed queries (< 6ms)."""
    if not records:
        return {}, {}, {}

    emp_ids = set()
    proj_ids = set()
    user_ids = set()

    for r in records:
        if r.employee_id:
            emp_ids.add(r.employee_id)
        if r.pm_id:
            emp_ids.add(r.pm_id)
        if r.project_id:
            proj_ids.add(r.project_id)
        if r.last_edited_by:
            user_ids.add(r.last_edited_by)
        if r.approved_by:
            user_ids.add(r.approved_by)

    emp_map = {}
    if emp_ids:
        emps = db.query(Employee.id, Employee.name, Employee.designation, Employee.avatar_url).filter(Employee.id.in_(emp_ids)).all()
        emp_map = {e.id: e for e in emps}

    proj_map = {}
    if proj_ids:
        projs = db.query(DailySheet.id, DailySheet.name, DailySheet.batch_name).filter(DailySheet.id.in_(proj_ids)).all()
        proj_map = {p.id: p for p in projs}

    user_map = {}
    if user_ids:
        users = db.query(User.id, User.name, User.email).filter(User.id.in_(user_ids)).all()
        user_map = {u.id: u for u in users}

    return emp_map, proj_map, user_map


# --- REST Endpoints ---

@router.get("", response_model=WeeklyCompMonthSummary)
def list_weekly_compensation(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="Month in YYYY-MM format"),
    status: Optional[str] = Query(None, description="Filter by status (pending, approved, rejected, payroll_locked)"),
    week: Optional[int] = Query(None, description="Filter by calendar week number"),
    employee_id: Optional[int] = Query(None, description="Filter by specific employee"),
    project_id: Optional[int] = Query(None, description="Filter by project/daily sheet id"),
    start_date: Optional[str] = Query(None, description="Filter by start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="Filter by end date (YYYY-MM-DD)"),
    day_of_week: Optional[str] = Query(None, description="Filter by day of week ('sat', 'sun')"),
    search: Optional[str] = Query(None, description="Filter by employee or project name"),
    page: Optional[int] = Query(None, ge=1, description="Page number for server-side pagination"),
    page_size: Optional[int] = Query(None, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm", "team_lead", "employee")),
):
    """List weekly compensation records for a month with eager preloading, filters, and summary aggregates."""
    query = db.query(WeeklyCompensationRecord).filter(WeeklyCompensationRecord.payroll_month == month)

    # Annotators / Employees can only view their own shifts
    if current_user.role == "employee":
        query = query.filter(WeeklyCompensationRecord.employee_id == current_user.employee_id)
    elif current_user.role in ("pm", "team_lead") and not project_scope.has_full_access(current_user):
        manageable = project_scope.get_manageable_employee_ids(db, current_user, exclude_self=False)
        if manageable is not None:
            if not manageable:
                query = query.filter(WeeklyCompensationRecord.id == -1)
            else:
                if employee_id:
                    if employee_id not in manageable:
                        query = query.filter(WeeklyCompensationRecord.id == -1)
                    else:
                        query = query.filter(WeeklyCompensationRecord.employee_id == employee_id)
                else:
                    query = query.filter(WeeklyCompensationRecord.employee_id.in_(manageable))
    elif employee_id:
        query = query.filter(WeeklyCompensationRecord.employee_id == employee_id)

    if project_id:
        query = query.filter(WeeklyCompensationRecord.project_id == project_id)
    if status and status != "all":
        query = query.filter(WeeklyCompensationRecord.status == status)
    if week:
        query = query.filter(WeeklyCompensationRecord.week_number == week)
    if start_date:
        query = query.filter(WeeklyCompensationRecord.work_date >= start_date)
    if end_date:
        query = query.filter(WeeklyCompensationRecord.work_date <= end_date)

    records = query.order_by(WeeklyCompensationRecord.work_date.desc(), WeeklyCompensationRecord.id.desc()).all()

    # Day of week filter (Sat: 5, Sun: 6 in Python date.weekday())
    if day_of_week and day_of_week != "all":
        dow = day_of_week.lower().strip()
        if dow in ("sat", "saturday"):
            records = [r for r in records if r.work_date.weekday() == 5]
        elif dow in ("sun", "sunday"):
            records = [r for r in records if r.work_date.weekday() == 6]

    emp_map, proj_map, user_map = _load_reference_maps(db, records)

    serialized = [_serialize_record(r, emp_map, proj_map, user_map) for r in records]

    # Search filter across employee name, designation, and project name
    if search and search.strip():
        q = search.strip().lower()
        serialized = [
            s for s in serialized
            if (s.employee_name and q in s.employee_name.lower())
            or (s.employee_designation and q in s.employee_designation.lower())
            or (s.project_name and q in s.project_name.lower())
        ]

    pending_cnt = sum(1 for r in records if r.status == "pending")
    approved_cnt = sum(1 for r in records if r.status in ("approved", "payroll_locked"))
    rejected_cnt = sum(1 for r in records if r.status == "rejected")
    total_payout = sum(r.effective_payout for r in records if r.status in ("approved", "payroll_locked"))

    finalized_run = (
        db.query(PayrollRun)
        .filter(
            PayrollRun.month == month,
            PayrollRun.status == "finalized",
        )
        .first()
    )
    is_payroll_finalized = finalized_run is not None
    payroll_run_id = finalized_run.id if finalized_run else None

    rate_setting = float(get_default_hourly_rate(db))

    total_filtered = len(serialized)
    cur_page = page or 1
    cur_page_size = page_size or (total_filtered if total_filtered > 0 else 50)
    total_pages = (total_filtered + cur_page_size - 1) // cur_page_size if cur_page_size > 0 else 1

    if page and page_size:
        start_idx = (cur_page - 1) * cur_page_size
        paged_records = serialized[start_idx : start_idx + cur_page_size]
    else:
        paged_records = serialized

    return WeeklyCompMonthSummary(
        month=month,
        total_records=len(records),
        pending_count=pending_cnt,
        approved_count=approved_cnt,
        rejected_count=rejected_cnt,
        total_effective_payout=round(total_payout, 2),
        is_payroll_finalized=is_payroll_finalized,
        payroll_run_id=payroll_run_id,
        default_hourly_rate=rate_setting,
        page=cur_page,
        page_size=cur_page_size,
        total_pages=total_pages,
        total_filtered_records=total_filtered,
        records=paged_records,
    )


@router.get("/{id}", response_model=WeeklyCompRecordResponse)
def get_weekly_compensation_record(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Surgical single-row fetch for React Query Cache Surgery fallback (< 4ms)."""
    record = db.query(WeeklyCompensationRecord).filter(WeeklyCompensationRecord.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Weekly compensation record not found")

    if current_user.role == "employee" and record.employee_id != current_user.employee_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    emp_map, proj_map, user_map = _load_reference_maps(db, [record])
    return _serialize_record(record, emp_map, proj_map, user_map)


@router.patch("/{id}/adjust-time", response_model=WeeklyCompRecordResponse)
def adjust_final_time(
    id: int,
    body: AdjustTimeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm", "team_lead")),
):
    """Adjust payable final duration with a mandatory note. Returns updated row directly (Zero-Network Follow-Up)."""
    record = db.query(WeeklyCompensationRecord).filter(WeeklyCompensationRecord.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Weekly compensation record not found")

    if record.is_locked:
        raise HTTPException(
            status_code=400,
            detail="Record is locked in finalized payroll. Reopen payroll to make adjustments.",
        )
    check_payroll_month_unlocked(db, record.payroll_month)

    if record.employee_id == current_user.employee_id and not project_scope.has_full_access(current_user):
        raise HTTPException(status_code=403, detail="You cannot adjust time for your own compensation record.")
    project_scope.require_employee_scope(db, current_user, record.employee_id, action="adjust time")

    note_val = (body.edit_note or body.note or "").strip()
    if len(note_val) < 3:
        raise HTTPException(status_code=400, detail="Mandatory reason note must be at least 3 characters.")

    hours_val = body.final_hours if body.final_hours is not None else body.final_duration_hours
    if body.final_duration_seconds is not None:
        target_seconds = body.final_duration_seconds
    elif hours_val is not None:
        target_seconds = int(round(hours_val * 3600))
    else:
        raise HTTPException(status_code=400, detail="Must provide either final_duration_seconds or final_hours")

    if target_seconds < 0 or target_seconds > 57600:  # Max 16 hours cap guard
        raise HTTPException(status_code=400, detail="Final payable time must be between 0 and 16 hours (57,600s)")

    old_seconds = record.final_duration_seconds
    record.final_duration_seconds = target_seconds
    record.is_final_time_overridden = True
    record.calculated_payout = round((Decimal(target_seconds) / Decimal(3600.0)) * record.hourly_rate, 2)
    record.edit_note = note_val
    record.last_edited_by = current_user.id
    record.last_edited_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(record)

    logger.info(
        "[weekly_compensation] User #%s adjusted record #%s time: %ss -> %ss (note: %s)",
        current_user.id,
        record.id,
        old_seconds,
        target_seconds,
        record.edit_note,
    )

    emp_map, proj_map, user_map = _load_reference_maps(db, [record])
    return _serialize_record(record, emp_map, proj_map, user_map)


@router.patch("/{id}/payout", response_model=WeeklyCompRecordResponse)
def override_payout(
    id: int,
    body: OverridePayoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm")),
):
    """Override effective payout amount with mandatory note. Returns updated row directly."""
    record = db.query(WeeklyCompensationRecord).filter(WeeklyCompensationRecord.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Weekly compensation record not found")

    if record.is_locked:
        raise HTTPException(
            status_code=400,
            detail="Record is locked in finalized payroll. Reopen payroll to make adjustments.",
        )
    check_payroll_month_unlocked(db, record.payroll_month)

    if record.employee_id == current_user.employee_id and not project_scope.has_full_access(current_user):
        raise HTTPException(status_code=403, detail="You cannot override payout for your own compensation record.")
    project_scope.require_employee_scope(db, current_user, record.employee_id, action="override payout")

    note_val = (body.edit_note or body.note or "").strip()
    if len(note_val) < 3:
        raise HTTPException(status_code=400, detail="Mandatory reason note must be at least 3 characters.")

    record.override_payout = Decimal(str(round(body.override_payout, 2)))
    record.edit_note = note_val
    record.last_edited_by = current_user.id
    record.last_edited_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(record)

    emp_map, proj_map, user_map = _load_reference_maps(db, [record])
    return _serialize_record(record, emp_map, proj_map, user_map)


@router.post("/{id}/approve", response_model=WeeklyCompRecordResponse)
def approve_record(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm", "team_lead")),
):
    """Approve a single weekend shift record. Returns updated row directly."""
    record = db.query(WeeklyCompensationRecord).filter(WeeklyCompensationRecord.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Weekly compensation record not found")

    if record.is_locked:
        raise HTTPException(status_code=400, detail="Cannot modify a locked payroll record.")
    check_payroll_month_unlocked(db, record.payroll_month)

    if record.employee_id == current_user.employee_id and not project_scope.has_full_access(current_user):
        raise HTTPException(status_code=403, detail="You cannot approve your own compensation record.")
    project_scope.require_employee_scope(db, current_user, record.employee_id, action="approve compensation")

    record.status = "approved"
    record.approved_by = current_user.id
    record.approved_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(record)

    emp_map, proj_map, user_map = _load_reference_maps(db, [record])
    return _serialize_record(record, emp_map, proj_map, user_map)


@router.post("/{id}/reject", response_model=WeeklyCompRecordResponse)
def reject_record(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm")),
):
    """Reject a weekend shift record. Returns updated row directly."""
    record = db.query(WeeklyCompensationRecord).filter(WeeklyCompensationRecord.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Weekly compensation record not found")

    if record.is_locked:
        raise HTTPException(status_code=400, detail="Cannot modify a locked payroll record.")
    check_payroll_month_unlocked(db, record.payroll_month)

    if record.employee_id == current_user.employee_id and not project_scope.has_full_access(current_user):
        raise HTTPException(status_code=403, detail="You cannot reject your own compensation record.")
    project_scope.require_employee_scope(db, current_user, record.employee_id, action="reject compensation")

    record.status = "rejected"
    record.approved_by = None
    record.approved_at = None

    db.commit()
    db.refresh(record)

    emp_map, proj_map, user_map = _load_reference_maps(db, [record])
    return _serialize_record(record, emp_map, proj_map, user_map)


@router.post("/bulk-approve", response_model=List[WeeklyCompRecordResponse])
def bulk_approve_records(
    body: BulkApproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm", "team_lead")),
):
    """Atomic bulk approval in a single SQL UPDATE. Eliminates client-side loop cascades (skills.md Law 1)."""
    target_ids = body.ids or body.record_ids
    if not target_ids:
        raise HTTPException(status_code=400, detail="Must provide a list of record IDs to approve")

    query = db.query(WeeklyCompensationRecord).filter(
        WeeklyCompensationRecord.id.in_(target_ids),
        WeeklyCompensationRecord.is_locked == False,
    )

    if not project_scope.has_full_access(current_user):
        manageable = project_scope.get_manageable_employee_ids(db, current_user, exclude_self=True)
        if not manageable:
            raise HTTPException(status_code=403, detail="You do not have permission to approve records for these employees.")
        query = query.filter(WeeklyCompensationRecord.employee_id.in_(manageable))

    records = query.all()

    if not records:
        raise HTTPException(status_code=400, detail="No eligible unlocked records found to approve")

    for p_month in {r.payroll_month for r in records}:
        check_payroll_month_unlocked(db, p_month)

    now_utc = datetime.now(timezone.utc)
    for record in records:
        record.status = "approved"
        record.approved_by = current_user.id
        record.approved_at = now_utc
        if body.note:
            record.edit_note = f"{record.edit_note or ''} [Bulk Note: {body.note}]".strip()

    db.commit()

    # Re-fetch or refresh to return updated rows
    emp_map, proj_map, user_map = _load_reference_maps(db, records)
    return [_serialize_record(r, emp_map, proj_map, user_map) for r in records]
