"""
Employee Signup Request API
- Public: POST /api/signup-requests         — submit a request
- Admin:  GET  /api/signup-requests         — list all requests
- Admin:  PATCH /api/signup-requests/{id}/approve
- Admin:  PATCH /api/signup-requests/{id}/reject
"""
import logging
import secrets
import string
from datetime import datetime
from typing import List, Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.notification import Notification
from app.models.signup_request import SignupRequest
from app.models.user import User
from app.services.email_service import try_send_signup_approved_email, try_send_signup_rejected_email
from app.services.identity_validator import check_duplicate_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/signup-requests", tags=["signup-requests"])

PORTAL_URL = "https://autonex-frontend.vercel.app/login/employee"


# ── Schemas ───────────────────────────────────────────────────────────────────

class SignupRequestCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    designation: Optional[str] = None
    employee_type: str = "Full-time"
    skills: Optional[List[str]] = []
    reason: Optional[str] = None


class SignupRequestResponse(BaseModel):
    id: int
    name: str
    email: str
    phone: Optional[str] = None
    designation: Optional[str] = None
    employee_type: str
    skills: Optional[List[str]] = []
    reason: Optional[str] = None
    status: str
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class RejectBody(BaseModel):
    reason: Optional[str] = None


class SignupRequestCountsResponse(BaseModel):
    pending: int
    approved: int
    rejected: int
    total: int


class PaginatedSignupRequestResponse(BaseModel):
    items: List[SignupRequestResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gen_temp_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _push_notification(db: Session, user_id: int, title: str, message: str, notif_type: str):
    db.add(Notification(user_id=user_id, title=title, message=message, type=notif_type))


def _to_response(req: SignupRequest) -> SignupRequestResponse:
    return SignupRequestResponse(
        id=req.id,
        name=req.name,
        email=req.email,
        phone=req.phone,
        designation=req.designation,
        employee_type=req.employee_type,
        skills=req.skills or [],
        reason=req.reason,
        status=req.status,
        reviewed_by=req.reviewed_by,
        reviewed_at=req.reviewed_at.isoformat() if req.reviewed_at else None,
        rejection_reason=req.rejection_reason,
        created_at=req.created_at.isoformat() if req.created_at else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", response_model=SignupRequestResponse, status_code=201)
def submit_signup_request(payload: SignupRequestCreate, db: Session = Depends(get_db)):
    """Public endpoint — anyone can submit a signup request."""
    # Clean up old rejected signup request if it exists, to allow re-application
    existing_rejected = db.query(SignupRequest).filter(
        SignupRequest.email == payload.email,
        SignupRequest.status == "rejected"
    ).first()
    if existing_rejected:
        db.delete(existing_rejected)
        db.flush()

    # Enforce unique identity check
    check_duplicate_identity(db, email=payload.email, phone=payload.phone)

    req = SignupRequest(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        designation=payload.designation,
        employee_type=payload.employee_type,
        skills=payload.skills or [],
        reason=payload.reason,
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # In-app notification for all admins
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for admin in admins:
        _push_notification(
            db, admin.id,
            f"New signup request from {req.name}",
            f"{req.name} ({req.email}) has submitted an employee signup request and is awaiting approval.",
            "signup_request",
        )
    db.commit()

    logger.info("[signup-request] New request id=%s email=%s", req.id, req.email)
    return _to_response(req)


@router.get("/counts", response_model=SignupRequestCountsResponse)
def get_signup_request_counts(db: Session = Depends(get_db)):
    """Return pending/approved/rejected/total counts for tab badges."""
    from sqlalchemy import func
    rows = (
        db.query(SignupRequest.status, func.count(SignupRequest.id))
        .group_by(SignupRequest.status)
        .all()
    )
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for status, count in rows:
        if status in counts:
            counts[status] = count
    counts["total"] = sum(counts.values())
    return counts


@router.get("", response_model=PaginatedSignupRequestResponse)
def list_signup_requests(
    status: Optional[str] = Query(None, description="Filter by status: pending | approved | rejected"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List signup requests with server-side pagination."""
    q = db.query(SignupRequest)
    if status:
        q = q.filter(SignupRequest.status == status)
    q = q.order_by(SignupRequest.created_at.desc())

    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    items = q.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedSignupRequestResponse(
        items=[_to_response(r) for r in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.patch("/{request_id}/approve")
def approve_signup_request(
    request_id: int,
    reviewed_by: int = Query(0),
    db: Session = Depends(get_db),
):
    """Approve a signup request — creates employee + user accounts and emails credentials."""
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {req.status}")

    # Guard: ensure no conflict exists before approving
    check_duplicate_identity(
        db,
        email=req.email,
        phone=req.phone,
        exclude_signup_request_id=request_id
    )

    # Create Employee record
    employee = Employee(
        name=req.name,
        email=req.email,
        phone=req.phone,
        designation=req.designation or "Annotator/ Reviewer",
        employee_type=req.employee_type,
        skills=req.skills or [],
        status="active",
        working_hours_per_day=8,
        weekly_availability=40,
        productivity_baseline=1.0,
    )
    db.add(employee)
    db.flush()

    # Create User with temp password
    temp_password = _gen_temp_password()
    pw_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()

    user = User(
        name=req.name,
        email=req.email,
        password_hash=pw_hash,
        role="employee",
        employee_id=employee.id,
        is_active=True,
        skills=req.skills or [],
    )
    db.add(user)
    db.flush()

    # Mark request approved
    req.status = "approved"
    req.reviewed_by = reviewed_by
    req.reviewed_at = datetime.utcnow()
    db.commit()

    logger.info("[signup-request] Approved id=%s → employee id=%s user id=%s", req.id, employee.id, user.id)

    # Send approval email to employee
    try_send_signup_approved_email(
        to_email=req.email,
        to_name=req.name,
        temp_password=temp_password,
        portal_url=PORTAL_URL,
    )

    # In-app notification to the approving admin
    if reviewed_by:
        _push_notification(
            db, reviewed_by,
            f"Account created for {req.name}",
            f"Employee account for {req.name} ({req.email}) has been created successfully. Login credentials were sent via email.",
            "signup_approved",
        )
        db.commit()

    return {
        "message": f"Signup approved. Employee account created and credentials emailed to {req.email}.",
        "employee_id": employee.id,
        "user_id": user.id,
    }


class UpdateSignupRequest(BaseModel):
    employee_type: Optional[str] = None
    designation: Optional[str] = None


@router.patch("/{request_id}", response_model=SignupRequestResponse)
def update_signup_request(
    request_id: int,
    payload: UpdateSignupRequest,
    db: Session = Depends(get_db),
):
    """Update editable fields (employee_type, designation) on a pending signup request."""
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be edited")

    if payload.employee_type is not None:
        req.employee_type = payload.employee_type
    if payload.designation is not None:
        req.designation = payload.designation

    db.commit()
    db.refresh(req)
    logger.info("[signup-request] Updated id=%s employee_type=%s", req.id, req.employee_type)
    return _to_response(req)


@router.patch("/{request_id}/reject")
def reject_signup_request(
    request_id: int,
    reviewed_by: int = Query(0),
    body: RejectBody = RejectBody(),
    db: Session = Depends(get_db),
):
    """Reject a signup request and optionally notify the applicant."""
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {req.status}")

    req.status = "rejected"
    req.reviewed_by = reviewed_by
    req.reviewed_at = datetime.utcnow()
    req.rejection_reason = body.reason
    db.commit()

    logger.info("[signup-request] Rejected id=%s email=%s reason=%s", req.id, req.email, body.reason)

    # Email the applicant
    try_send_signup_rejected_email(to_email=req.email, to_name=req.name, reason=body.reason or "")

    return {"message": f"Signup request rejected. {req.email} has been notified.", "request_id": request_id}


# ── Undo endpoints (to be enabled in a future release) ────────────────────────
# These endpoints allow admins to reverse approve/reject decisions.
#
# undo-reject: resets rejected → pending so admin can re-review and approve.
# undo-approve: sets approved → rejected AND deactivates user.is_active = False
#               + employee.status = inactive so the employee loses portal access.
#
# Frontend: add an "Undo" button (RotateCcw icon, secondary style) to the left
# of the Details button for approved and rejected rows in SignupRequestsPage.jsx.
# API methods to add in signupRequestApi:
#   undoReject:  PATCH /signup-requests/{id}/undo-reject  (no body)
#   undoApprove: PATCH /signup-requests/{id}/undo-approve (body: { reason? })
#
# @router.patch("/{request_id}/undo-reject")
# def undo_reject_signup_request(
#     request_id: int,
#     reviewed_by: int = Query(0),
#     db: Session = Depends(get_db),
# ):
#     req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
#     if not req:
#         raise HTTPException(status_code=404, detail="Signup request not found")
#     if req.status != "rejected":
#         raise HTTPException(status_code=400, detail=f"Request is not rejected (status: {req.status})")
#     req.status = "pending"
#     req.reviewed_by = reviewed_by
#     req.reviewed_at = datetime.utcnow()
#     req.rejection_reason = None
#     db.commit()
#     logger.info("[signup-request] Undo-reject id=%s email=%s", req.id, req.email)
#     return {"message": "Signup request reopened.", "request_id": request_id}
#
#
# @router.patch("/{request_id}/undo-approve")
# def undo_approve_signup_request(
#     request_id: int,
#     reviewed_by: int = Query(0),
#     body: RejectBody = RejectBody(),
#     db: Session = Depends(get_db),
# ):
#     req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
#     if not req:
#         raise HTTPException(status_code=404, detail="Signup request not found")
#     if req.status != "approved":
#         raise HTTPException(status_code=400, detail=f"Request is not approved (status: {req.status})")
#     user = db.query(User).filter(User.email == req.email).first()
#     if user:
#         user.is_active = False
#     employee = db.query(Employee).filter(Employee.email == req.email).first()
#     if employee:
#         employee.status = "inactive"
#     req.status = "rejected"
#     req.reviewed_by = reviewed_by
#     req.reviewed_at = datetime.utcnow()
#     req.rejection_reason = body.reason
#     db.commit()
#     logger.info("[signup-request] Undo-approve id=%s email=%s — account deactivated", req.id, req.email)
#     return {"message": "Approval revoked. Employee account has been deactivated.", "request_id": request_id}
