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
    # Check duplicate in users table
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    # Check duplicate pending/approved request
    existing = db.query(SignupRequest).filter(SignupRequest.email == payload.email).first()
    if existing:
        if existing.status == "pending":
            raise HTTPException(status_code=409, detail="A signup request for this email is already pending review.")
        if existing.status == "approved":
            raise HTTPException(status_code=409, detail="This email has already been approved. Please sign in.")
        # Rejected — allow re-application by deleting old record
        db.delete(existing)
        db.flush()

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


@router.get("", response_model=List[SignupRequestResponse])
def list_signup_requests(
    status: Optional[str] = Query(None, description="Filter by status: pending | approved | rejected"),
    db: Session = Depends(get_db),
):
    """List all signup requests (admin use)."""
    q = db.query(SignupRequest)
    if status:
        q = q.filter(SignupRequest.status == status)
    requests = q.order_by(SignupRequest.created_at.desc()).all()
    return [_to_response(r) for r in requests]


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

    # Guard: email must not have been registered in the meantime
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

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
