"""WFH (Work From Home) request management API."""
import logging
from datetime import date, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from app.services.auth_service import get_current_user, require_role
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, validator

from app.db.database import get_db
from app.models.wfh import WFHRequest
from app.models.employee import Employee
from app.models.user import User
from app.models.notification import Notification
from types import SimpleNamespace
from app.api.leaves import _get_pm_notification_targets, _get_admin_notification_targets
from app.constants.leave_types import is_intern_or_contractor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wfh", tags=["wfh"], dependencies=[Depends(get_current_user)])


def check_wfh_access(wfh_employee_id: int, current_user: User, db: Session):
    if current_user.role not in ["admin", "pm"]:
        is_self = current_user.employee_id == wfh_employee_id
        if not is_self:
            emp = db.query(Employee).filter(Employee.id == wfh_employee_id).first()
            if not emp or emp.email != current_user.email:
                raise HTTPException(status_code=403, detail="Access denied")


# ── Schemas ──────────────────────────────────────────────────────────────────

class WFHCreate(BaseModel):
    employee_id: int
    wfh_date: date          # start date
    end_date: Optional[date] = None   # defaults to wfh_date for single-day
    reason: str = Field(..., min_length=1)

    @validator("reason")
    def validate_reason(cls, v):
        if not v or not v.strip():
            raise ValueError("Reason cannot be empty or just whitespace.")
        return v.strip()


class WFHResponse(BaseModel):
    id: int
    employee_id: int
    wfh_date: date
    end_date: Optional[date] = None
    reason: Optional[str] = None
    status: str
    approved_by: Optional[int] = None
    remark: Optional[str] = None
    employee_name: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class WFHApproveBody(BaseModel):
    remark: Optional[str] = None


def _push_notification(db: Session, user_id: int, title: str, message: str, notif_type: str):
    db.add(Notification(user_id=user_id, title=title, message=message, type=notif_type))


def _validate_wfh_limit(db: Session, employee: Employee, start_date: date, end_date: date, exclude_wfh_id: Optional[int] = None):
    # Collect proposed WFH working days (skip weekends)
    proposed_dates = []
    curr = start_date
    while curr <= end_date:
        if curr.weekday() < 5:  # Monday=0, Friday=4, Saturday=5, Sunday=6
            proposed_dates.append(curr)
        curr += timedelta(days=1)

    if not proposed_dates:
        return

    # Query existing active WFH requests for this employee (excluding the one being updated, if any)
    query = db.query(WFHRequest).filter(
        WFHRequest.employee_id == employee.id,
        WFHRequest.status != "rejected"
    )
    if exclude_wfh_id is not None:
        query = query.filter(WFHRequest.id != exclude_wfh_id)
    existing_requests = query.all()

    existing_dates = set()
    for r in existing_requests:
        r_end = r.end_date or r.wfh_date
        curr_d = r.wfh_date
        while curr_d <= r_end:
            if curr_d.weekday() < 5:
                existing_dates.add(curr_d)
            curr_d += timedelta(days=1)

    # Check limits based on employee type
    if is_intern_or_contractor(employee.employee_type):
        # Limit: 2 WFH per calendar month
        month_counts = {}
        for d in existing_dates:
            key = (d.year, d.month)
            month_counts[key] = month_counts.get(key, 0) + 1
        
        for d in proposed_dates:
            key = (d.year, d.month)
            new_count = month_counts.get(key, 0) + 1
            if new_count > 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"Interns and contractors are limited to 2 WFH days per calendar month. This request would exceed that limit in {d.strftime('%B %Y')}."
                )
            month_counts[key] = new_count
    else:
        # Limit: 1 WFH per week (Mon-Sun)
        week_counts = {}
        for d in existing_dates:
            monday = d - timedelta(days=d.weekday())
            week_counts[monday] = week_counts.get(monday, 0) + 1
            
        for d in proposed_dates:
            monday = d - timedelta(days=d.weekday())
            new_count = week_counts.get(monday, 0) + 1
            if new_count > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Full-time employees are limited to 1 WFH day per week. This request would exceed that limit in the week of {monday.strftime('%Y-%m-%d')}."
                )
            week_counts[monday] = new_count


def _build_response(req: WFHRequest, db: Session) -> WFHResponse:
    employee = db.query(Employee).filter(Employee.id == req.employee_id).first()
    return WFHResponse(
        id=req.id,
        employee_id=req.employee_id,
        wfh_date=req.wfh_date,
        end_date=req.end_date or req.wfh_date,
        reason=req.reason,
        status=req.status,
        approved_by=req.approved_by,
        remark=req.remark,
        employee_name=employee.name if employee else None,
        created_at=req.created_at.isoformat() if req.created_at else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[WFHResponse])
def get_wfh_requests(
    employee_id: Optional[int] = Query(None),
    month: Optional[str] = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get WFH requests. Filter by employee_id and/or month (YYYY-MM)."""
    if current_user.role not in ["admin", "pm"]:
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
    q = db.query(WFHRequest)
    if employee_id:
        q = q.filter(WFHRequest.employee_id == employee_id)
    if month:
        try:
            year, mo = int(month[:4]), int(month[5:7])
            from datetime import date as dt
            start = dt(year, mo, 1)
            end_mo = mo + 1 if mo < 12 else 1
            end_yr = year if mo < 12 else year + 1
            end = dt(end_yr, end_mo, 1)
            q = q.filter(WFHRequest.wfh_date >= start, WFHRequest.wfh_date < end)
        except Exception:
            pass
    requests = q.order_by(WFHRequest.wfh_date.desc()).all()
    emp_ids = list({r.employee_id for r in requests})
    employees = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(emp_ids)).all()}
    result = []
    for req in requests:
        emp = employees.get(req.employee_id)
        result.append(WFHResponse(
            id=req.id,
            employee_id=req.employee_id,
            wfh_date=req.wfh_date,
            end_date=req.end_date or req.wfh_date,
            reason=req.reason,
            status=req.status,
            approved_by=req.approved_by,
            remark=req.remark,
            employee_name=emp.name if emp else None,
            created_at=req.created_at.isoformat() if req.created_at else None,
        ))
    return result


@router.post("", response_model=WFHResponse, status_code=201)
def create_wfh_request(
    payload: WFHCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Submit a WFH request."""
    check_wfh_access(payload.employee_id, current_user, db)
    employee = db.query(Employee).filter(Employee.id == payload.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    end_date = payload.end_date or payload.wfh_date
    if end_date < payload.wfh_date:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")

    # Check for overlap with existing non-rejected WFH requests
    overlap = db.query(WFHRequest).filter(
        WFHRequest.employee_id == payload.employee_id,
        WFHRequest.status != "rejected",
        WFHRequest.wfh_date <= end_date,
        (WFHRequest.end_date >= payload.wfh_date) | (WFHRequest.wfh_date >= payload.wfh_date),
    ).first()
    if overlap:
        overlap_end = overlap.end_date or overlap.wfh_date
        raise HTTPException(
            status_code=409,
            detail=f"A WFH request already exists overlapping this period ({overlap.wfh_date} – {overlap_end}).",
        )

    # Validate WFH limit
    _validate_wfh_limit(db, employee, payload.wfh_date, end_date)

    req = WFHRequest(
        employee_id=payload.employee_id,
        wfh_date=payload.wfh_date,
        end_date=end_date,
        reason=payload.reason,
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # Notify employee (in-app)
    emp_user = db.query(User).filter(User.employee_id == employee.id).first()
    if emp_user:
        _push_notification(db, emp_user.id, "WFH request submitted",
            f"Your WFH request for {req.wfh_date} has been submitted and is pending approval.",
            "wfh_applied")
        db.commit()

    # WFH PM & Admin Notification Routing:
    dummy_leave = SimpleNamespace(start_date=req.wfh_date, end_date=end_date)
    is_pm_applicant = emp_user is not None and emp_user.role == "pm"
    pm_targets = [] if is_pm_applicant else _get_pm_notification_targets(db, employee, dummy_leave)
    
    notified_user_ids: set[int] = set()
    for target in pm_targets:
        # In-app notification for PM
        pm_emp_id = getattr(target["pm_employee"], "id", None)
        if pm_emp_id:
            pm_user = db.query(User).filter(User.employee_id == pm_emp_id).first()
            if pm_user and pm_user.id not in notified_user_ids:
                notified_user_ids.add(pm_user.id)
                _push_notification(
                    db, pm_user.id,
                    f"New WFH request from {employee.name}",
                    f"{employee.name} has requested WFH on {req.wfh_date}.",
                    "wfh_applied",
                )

    # Admin fallback: notify each admin exactly once if no PM is assigned
    if not pm_targets:
        for admin_user in db.query(User).filter(User.role == "admin", User.is_active == True).all():
            if admin_user.id not in notified_user_ids:
                notified_user_ids.add(admin_user.id)
                _push_notification(
                    db, admin_user.id,
                    f"WFH request from {employee.name}",
                    f"{employee.name} has requested WFH on {req.wfh_date}.",
                    "wfh_applied",
                )
    db.commit()

    return _build_response(req, db)


@router.patch("/{wfh_id}/approve", dependencies=[Depends(require_role("admin", "pm"))])
def approve_wfh(
    wfh_id: int,
    approved_by: int = Query(0),
    body: WFHApproveBody = WFHApproveBody(),
    db: Session = Depends(get_db),
):
    """Approve a WFH request."""
    req = db.query(WFHRequest).filter(WFHRequest.id == wfh_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")

    employee = db.query(Employee).filter(Employee.id == req.employee_id).first()
    approver = db.query(User).filter(User.id == approved_by).first() if approved_by else None
    approver_name = approver.name if approver else "Admin"

    req.status = "approved"
    req.approved_by = approved_by
    req.remark = body.remark
    db.commit()

    emp_user = db.query(User).filter(User.employee_id == req.employee_id).first()
    if emp_user:
        _push_notification(db, emp_user.id, "WFH approved",
            f"Your WFH request for {req.wfh_date} has been approved by {approver_name}.",
            "wfh_approved")
        db.commit()

    return {"message": "WFH request approved", "wfh_id": wfh_id, "status": "approved"}


@router.patch("/{wfh_id}/reject", dependencies=[Depends(require_role("admin", "pm"))])
def reject_wfh(
    wfh_id: int,
    approved_by: int = Query(0),
    body: WFHApproveBody = WFHApproveBody(),
    db: Session = Depends(get_db),
):
    """Reject a WFH request."""
    req = db.query(WFHRequest).filter(WFHRequest.id == wfh_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")

    approver = db.query(User).filter(User.id == approved_by).first() if approved_by else None
    approver_name = approver.name if approver else "Admin"

    req.status = "rejected"
    req.approved_by = approved_by
    req.remark = body.remark
    db.commit()

    emp_user = db.query(User).filter(User.employee_id == req.employee_id).first()
    if emp_user:
        _push_notification(db, emp_user.id, "WFH request declined",
            f"Your WFH request for {req.wfh_date} was declined by {approver_name}.",
            "wfh_rejected")
        db.commit()

    return {"message": "WFH request rejected", "wfh_id": wfh_id, "status": "rejected"}


@router.patch("/{wfh_id}/undo-reject", dependencies=[Depends(require_role("admin", "pm"))])
def undo_reject_wfh(wfh_id: int, approved_by: int = Query(0), db: Session = Depends(get_db)):
    """Reopen a rejected WFH request back to pending."""
    req = db.query(WFHRequest).filter(WFHRequest.id == wfh_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")
    if req.status != "rejected":
        raise HTTPException(status_code=400, detail=f"WFH request is not rejected (status: {req.status})")

    end_date = req.end_date or req.wfh_date
    # Re-check overlap since another non-rejected WFH request may have been created in this
    # window while this one was rejected (rejected requests don't block new ones).
    overlap = db.query(WFHRequest).filter(
        WFHRequest.employee_id == req.employee_id,
        WFHRequest.id != wfh_id,
        WFHRequest.status != "rejected",
        WFHRequest.wfh_date <= end_date,
        (WFHRequest.end_date >= req.wfh_date) | (WFHRequest.wfh_date >= req.wfh_date),
    ).first()
    if overlap:
        overlap_end = overlap.end_date or overlap.wfh_date
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reopen — it now overlaps another active WFH request ({overlap.wfh_date} – {overlap_end}).",
        )

    req.status = "pending"
    req.approved_by = approved_by
    db.commit()

    emp_user = db.query(User).filter(User.employee_id == req.employee_id).first()
    if emp_user:
        _push_notification(db, emp_user.id, "WFH request reopened",
            f"Your WFH request for {req.wfh_date} has been reopened and is pending approval.",
            "wfh_applied")
        db.commit()

    return {"message": "WFH request reopened", "wfh_id": wfh_id, "status": "pending"}


@router.patch("/{wfh_id}/undo-approve", dependencies=[Depends(require_role("admin", "pm"))])
def undo_approve_wfh(wfh_id: int, approved_by: int = Query(0), db: Session = Depends(get_db)):
    """Revoke an approval, reverting the WFH request back to pending."""
    req = db.query(WFHRequest).filter(WFHRequest.id == wfh_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")
    if req.status != "approved":
        raise HTTPException(status_code=400, detail=f"WFH request is not approved (status: {req.status})")

    req.status = "pending"
    req.approved_by = approved_by
    db.commit()

    emp_user = db.query(User).filter(User.employee_id == req.employee_id).first()
    if emp_user:
        _push_notification(db, emp_user.id, "WFH approval revoked",
            f"Your WFH request for {req.wfh_date} is back to pending — approval was revoked.",
            "wfh_applied")
        db.commit()

    return {"message": "WFH approval revoked, reverted to pending", "wfh_id": wfh_id, "status": "pending"}


@router.put("/{wfh_id}", response_model=WFHResponse)
def update_wfh_request(
    wfh_id: int,
    payload: WFHCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Edit a WFH request. Only allowed before the WFH date."""
    req = db.query(WFHRequest).filter(WFHRequest.id == wfh_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")
    check_wfh_access(req.employee_id, current_user, db)
    check_wfh_access(payload.employee_id, current_user, db)
    if req.wfh_date <= date.today():
        raise HTTPException(status_code=400, detail="Cannot edit a WFH request on or after its date")

    end_date = payload.end_date or payload.wfh_date
    if end_date < payload.wfh_date:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")

    # Check for overlap with other non-rejected WFH requests (excluding this one)
    overlap = db.query(WFHRequest).filter(
        WFHRequest.employee_id == req.employee_id,
        WFHRequest.id != wfh_id,
        WFHRequest.status != "rejected",
        WFHRequest.wfh_date <= end_date,
        (WFHRequest.end_date >= payload.wfh_date) | (WFHRequest.wfh_date >= payload.wfh_date),
    ).first()
    if overlap:
        overlap_end = overlap.end_date or overlap.wfh_date
        raise HTTPException(
            status_code=409,
            detail=f"A WFH request already exists overlapping this period ({overlap.wfh_date} – {overlap_end}).",
        )

    employee = db.query(Employee).filter(Employee.id == req.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Validate WFH limit
    _validate_wfh_limit(db, employee, payload.wfh_date, end_date, exclude_wfh_id=wfh_id)

    req.wfh_date = payload.wfh_date
    req.end_date = end_date
    req.reason = payload.reason
    req.status = "pending"  # reset so manager re-reviews
    db.commit()
    db.refresh(req)
    return _build_response(req, db)


@router.delete("/{wfh_id}")
def delete_wfh(
    wfh_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    req = db.query(WFHRequest).filter(WFHRequest.id == wfh_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")
    check_wfh_access(req.employee_id, current_user, db)
    if req.wfh_date <= date.today():
        raise HTTPException(status_code=400, detail="Cannot delete a WFH request on or after its date")
    db.delete(req)
    db.commit()
    return {"message": "WFH request deleted"}
