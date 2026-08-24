"""
Employee Signup Request API

Signup is a TWO-STEP flow, because an unverified email address was creating real
admin work: an applicant who typed kisan12@ instead of kisan123@ still got
approved, then couldn't log in, and an admin had to correct the address by hand.

- Public: POST /api/signup-requests/verify-email        — step 1: email a signup link
- Public: GET  /api/signup-requests/verify-email/check  — validate a link's token
- Public: POST /api/signup-requests                     — step 2: submit (token required)
- Admin:  GET  /api/signup-requests                     — list all requests
- Admin:  PATCH /api/signup-requests/{id}/approve
- Admin:  PATCH /api/signup-requests/{id}/reject

Step 2 takes the email from the signed token, never from the request body, so the
address on an admin's queue is always one the applicant can receive mail at.
"""
import logging
import os
import secrets
import string
from datetime import datetime, timedelta
from typing import List, Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jose import JWTError, jwt
from app.services.auth_service import ALGORITHM, SECRET_KEY, get_current_user, require_role
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.notification import Notification
from app.models.signup_request import SignupRequest
from app.models.user import User
from app.services.email_service import (
    send_signup_verification_email,
    try_send_signup_approved_email,
    try_send_signup_rejected_email,
)
from app.services.identity_validator import check_duplicate_identity
from app.services import audit_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/signup-requests", tags=["signup-requests"])

PORTAL_URL = "https://pmportal.autonexai360.com/login/employee"

# Email-verification tokens are stateless signed JWTs — no table, no cleanup job.
# Reuse within the TTL is fine and deliberate: it just reopens the form if the
# applicant closes the tab.
SIGNUP_VERIFY_PURPOSE = "signup_email_verify"
SIGNUP_VERIFY_TTL_MINUTES = int(os.getenv("SIGNUP_VERIFY_TTL_MINUTES", "60"))


# ── Schemas ───────────────────────────────────────────────────────────────────

class EmailVerifyRequest(BaseModel):
    email: EmailStr


class SignupRequestCreate(BaseModel):
    """Step 2 payload. Note there is deliberately NO email field — the address is
    read out of verification_token, so a client can't substitute a different one."""
    name: str
    verification_token: str
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


def _normalize_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _frontend_base_url(request: Request) -> str:
    """Base URL the signup link points at — mirrors the password-reset flow."""
    return (
        os.getenv("SIGNUP_FRONTEND_URL")
        or os.getenv("FRONTEND_URL")
        or request.headers.get("origin")
        or "http://localhost:5173"
    ).strip().rstrip("/")


def _dev_return_link() -> bool:
    """DEV_RETURN_SIGNUP_LINK=true returns the link in the API response instead of
    requiring a working mailbox. Local testing only — it hands the link to whoever
    called the endpoint, which defeats the whole point of verifying the address."""
    return os.getenv("DEV_RETURN_SIGNUP_LINK", "false").lower() == "true"


def _create_verify_token(email: str) -> str:
    """Sign a short-lived token carrying the address the link is mailed to."""
    return jwt.encode(
        {
            "sub": _normalize_email(email),
            "exp": datetime.utcnow() + timedelta(minutes=SIGNUP_VERIFY_TTL_MINUTES),
            "purpose": SIGNUP_VERIFY_PURPOSE,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _email_from_verify_token(token: str) -> str:
    """Return the verified address inside a signup token, or raise 400.

    Because the address is *inside* the signature, it can only be the one the link
    was mailed to — that is the whole basis for trusting it downstream.
    """
    expired_or_bad = "This verification link is invalid or has expired. Please request a new one."
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=400, detail=expired_or_bad) from exc

    if payload.get("purpose") != SIGNUP_VERIFY_PURPOSE:
        # e.g. someone pasting a password-reset token here.
        raise HTTPException(status_code=400, detail=expired_or_bad)

    email = _normalize_email(payload.get("sub"))
    if not email:
        raise HTTPException(status_code=400, detail=expired_or_bad)
    return email


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

@router.post("/verify-email")
def request_email_verification(
    payload: EmailVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Step 1 — public. Email the applicant a link to the actual signup form.

    Nothing is written to the database here; the token is self-contained. Duplicate
    identity is checked up front so an applicant who already has an account (or a
    request in flight) is told immediately, instead of after filling in the form.
    """
    email = _normalize_email(payload.email)

    # Raises 409 with a specific reason (existing employee / user / request in flight).
    # A previously *rejected* request is not counted, so re-applying still works.
    check_duplicate_identity(db, email=email)

    link = f"{_frontend_base_url(request)}/employee-signup?token={_create_verify_token(email)}"

    if _dev_return_link():
        logger.warning(
            "[signup-verify] DEV_RETURN_SIGNUP_LINK=true — returning the signup link "
            "in the response. NEVER enable this in production!"
        )
        return {
            "message": "[DEV MODE] Verification email skipped — use the link below.",
            "email": email,
            "expires_in_minutes": SIGNUP_VERIFY_TTL_MINUTES,
            "verification_link": link,
        }

    try:
        send_signup_verification_email(
            to_email=email,
            signup_link=link,
            expires_minutes=SIGNUP_VERIFY_TTL_MINUTES,
        )
    except Exception as exc:
        logger.error("[signup-verify] Could not email %s: %s", email, exc)
        raise HTTPException(
            status_code=503,
            detail="Could not send the verification email. Please check the address and try again.",
        ) from exc

    logger.info("[signup-verify] Verification link sent to %s", email)
    return {
        "message": f"Verification link sent to {email}. Open it to finish signing up.",
        "email": email,
        "expires_in_minutes": SIGNUP_VERIFY_TTL_MINUTES,
    }


@router.get("/verify-email/check")
def check_email_verification(
    token: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    """Validate a signup link and hand back the address it proves — public.

    The form uses this to pre-fill and lock the email field. Duplicates are
    re-checked because an admin may have created the account between the link
    being sent and it being opened.
    """
    email = _email_from_verify_token(token)
    check_duplicate_identity(db, email=email)
    return {"email": email, "verified": True}


@router.post("", response_model=SignupRequestResponse, status_code=201)
def submit_signup_request(
    payload: SignupRequestCreate,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Step 2 — public, but only reachable with a valid verification token.

    The email comes from the token, not the body, so the stored address is always
    the one that received the link.
    """
    email = _email_from_verify_token(payload.verification_token)

    # Clean up old rejected signup request(s) so a rejected applicant can re-apply.
    # Matched case-insensitively: the token's address is normalized to lower case,
    # while an older row may hold whatever casing was typed at the time.
    for stale in db.query(SignupRequest).filter(
        SignupRequest.email.ilike(email),
        SignupRequest.status == "rejected",
    ).all():
        db.delete(stale)
    db.flush()

    # Enforce unique identity check
    check_duplicate_identity(db, email=email, phone=payload.phone)

    req = SignupRequest(
        name=payload.name,
        email=email,
        phone=payload.phone,
        designation=payload.designation,
        employee_type=payload.employee_type,
        skills=payload.skills or [],
        reason=payload.reason,
        status="pending",
    )
    db.add(req)
    db.flush()

    # actor=None: this endpoint is public and the submitter has no account yet, so
    # there is no authenticated identity to attribute. The verified email address on
    # the entity is the strongest identifier available.
    audit_service.record(
        db,
        actor=None,
        action="signup_request.submitted",
        category="Access",
        action_type="Applied",
        entity_type="signup_request",
        entity_id=req.id,
        entity_name=req.name,
        subject_name=req.name,
        details=audit_service.changes(
            audit_service.field_diff("Email", None, req.email),
            audit_service.field_diff("Designation", None, req.designation),
            audit_service.field_diff("Employee type", None, req.employee_type),
            audit_service.field_diff("Status", None, req.status),
        ),
        summary=f"{req.name} ({req.email}) requested portal access",
        request=http_request,
    )
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


@router.get("/counts", response_model=SignupRequestCountsResponse, dependencies=[Depends(require_role("admin"))])
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


@router.get("", response_model=PaginatedSignupRequestResponse, dependencies=[Depends(require_role("admin"))])
def list_signup_requests(
    status: Optional[str] = Query(None, description="Filter by status: pending | approved | rejected"),
    search: Optional[str] = Query(None, description="Case-insensitive search on name, email, or designation"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List signup requests with server-side pagination and search."""
    q = db.query(SignupRequest)
    if status:
        q = q.filter(SignupRequest.status == status)
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            SignupRequest.name.ilike(term) |
            SignupRequest.email.ilike(term) |
            SignupRequest.designation.ilike(term)
        )
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
    http_request: Request,
    reviewed_by: int = Query(0, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Approve a signup request — creates employee + user accounts and emails credentials.

    The reviewer is taken from the authenticated session. ``reviewed_by`` is still
    accepted so existing callers keep working, but it is ignored: a client-supplied
    reviewer id let any admin attribute an approval to a colleague."""
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {req.status}")

    # Check for existing inactive records from a previous undo-approve
    existing_employee = db.query(Employee).filter(Employee.email == req.email).first()
    existing_user = db.query(User).filter(User.email == req.email).first()

    # Guard: ensure no conflict from OTHER records (exclude the ones we may reactivate)
    check_duplicate_identity(
        db,
        email=req.email,
        phone=req.phone,
        exclude_signup_request_id=request_id,
        exclude_employee_id=existing_employee.id if existing_employee else None,
        exclude_user_id=existing_user.id if existing_user else None,
    )

    # Reactivate existing employee or create a new one
    if existing_employee:
        existing_employee.status = "active"
        existing_employee.name = req.name
        existing_employee.phone = req.phone
        existing_employee.designation = req.designation or existing_employee.designation or "Annotator/ Reviewer"
        existing_employee.employee_type = req.employee_type
        existing_employee.skills = req.skills or []
        employee = existing_employee
    else:
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

    # Generate new temp password
    temp_password = _gen_temp_password()
    pw_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()

    # Reactivate existing user or create a new one
    if existing_user:
        existing_user.is_active = True
        existing_user.password_hash = pw_hash
        existing_user.employee_id = employee.id
        existing_user.skills = req.skills or []
        existing_user.must_change_password = True
        user = existing_user
    else:
        user = User(
            name=req.name,
            email=req.email,
            password_hash=pw_hash,
            role="employee",
            employee_id=employee.id,
            is_active=True,
            must_change_password=True,
            skills=req.skills or [],
        )
        db.add(user)
    db.flush()

    # Mark request approved
    previous_status = req.status
    req.status = "approved"
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()

    audit_service.record(
        db,
        actor=current_user,
        action="signup_request.approved",
        category="Access",
        action_type="Approved",
        entity_type="signup_request",
        entity_id=req.id,
        entity_name=req.name,
        subject_employee_id=employee.id,
        subject_name=req.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "approved"),
            audit_service.field_diff("Employee account", None, f"#{employee.id}"),
            audit_service.field_diff("User account", None, f"#{user.id}"),
            audit_service.field_diff("Designation", None, req.designation),
            audit_service.field_diff("Employee type", None, req.employee_type),
        ),
        summary=(
            f"Approved signup for {req.name} ({req.email}) — employee and login "
            f"account created"
        ),
        request=http_request,
    )
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
    if current_user.id:
        _push_notification(
            db, current_user.id,
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
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Update editable fields (employee_type, designation) on a pending signup request."""
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be edited")

    old_type = req.employee_type
    old_designation = req.designation

    if payload.employee_type is not None:
        req.employee_type = payload.employee_type
    if payload.designation is not None:
        req.designation = payload.designation

    details = audit_service.changes(
        audit_service.field_diff("Employee type", old_type, req.employee_type),
        audit_service.field_diff("Designation", old_designation, req.designation),
    )
    if details:
        # Designation decides the login role granted at approval time, so editing it
        # pre-approval is effectively choosing what access this person will get.
        audit_service.record(
            db,
            actor=current_user,
            action="signup_request.updated",
            category="Access",
            action_type="Updated",
            entity_type="signup_request",
            entity_id=req.id,
            entity_name=req.name,
            subject_name=req.name,
            details=details,
            summary=(
                f"Edited pending signup request for {req.name} — "
                + ", ".join(d["field"] for d in details)
            ),
            request=http_request,
        )

    db.commit()
    db.refresh(req)
    logger.info("[signup-request] Updated id=%s employee_type=%s", req.id, req.employee_type)
    return _to_response(req)


@router.patch("/{request_id}/reject")
def reject_signup_request(
    request_id: int,
    http_request: Request,
    reviewed_by: int = Query(0, deprecated=True),
    body: RejectBody = RejectBody(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Reject a signup request and optionally notify the applicant.

    Reviewer comes from the session; ``reviewed_by`` is ignored."""
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {req.status}")

    previous_status = req.status
    req.status = "rejected"
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    req.rejection_reason = body.reason

    audit_service.record(
        db,
        actor=current_user,
        action="signup_request.rejected",
        category="Access",
        action_type="Rejected",
        entity_type="signup_request",
        entity_id=req.id,
        entity_name=req.name,
        subject_name=req.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "rejected"),
            audit_service.field_diff("Reason", None, body.reason),
        ),
        summary=f"Rejected signup request from {req.name} ({req.email})",
        request=http_request,
    )
    db.commit()

    logger.info("[signup-request] Rejected id=%s email=%s reason=%s", req.id, req.email, body.reason)

    # Email the applicant
    try_send_signup_rejected_email(to_email=req.email, to_name=req.name, reason=body.reason or "")

    return {"message": f"Signup request rejected. {req.email} has been notified.", "request_id": request_id}


@router.patch("/{request_id}/undo-reject")
def undo_reject_signup_request(
    request_id: int,
    http_request: Request,
    reviewed_by: int = Query(0, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "rejected":
        raise HTTPException(status_code=400, detail=f"Request is not rejected (status: {req.status})")
    previous_status = req.status
    previous_reason = req.rejection_reason
    req.status = "pending"
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    req.rejection_reason = None

    audit_service.record(
        db,
        actor=current_user,
        action="signup_request.reject_undone",
        category="Access",
        action_type="Restored",
        entity_type="signup_request",
        entity_id=req.id,
        entity_name=req.name,
        subject_name=req.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "pending"),
            audit_service.field_diff("Reason", previous_reason, None),
        ),
        summary=f"Reopened rejected signup request from {req.name} ({req.email})",
        request=http_request,
    )
    db.commit()
    logger.info("[signup-request] Undo-reject id=%s email=%s", req.id, req.email)
    return {"message": "Signup request reopened.", "request_id": request_id}


@router.patch("/{request_id}/undo-approve")
def undo_approve_signup_request(
    request_id: int,
    http_request: Request,
    reviewed_by: int = Query(0, deprecated=True),
    body: RejectBody = RejectBody(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    req = db.query(SignupRequest).filter(SignupRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if req.status != "approved":
        raise HTTPException(status_code=400, detail=f"Request is not approved (status: {req.status})")
    user = db.query(User).filter(User.email == req.email).first()
    if user:
        user.is_active = False
    employee = db.query(Employee).filter(Employee.email == req.email).first()
    if employee:
        employee.status = "inactive"
    previous_status = req.status
    req.status = "rejected"
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    req.rejection_reason = body.reason

    # This one revokes a live login and deactivates a person's employee record, so
    # it is the most consequential action in this file — worth the detail.
    audit_service.record(
        db,
        actor=current_user,
        action="signup_request.approval_revoked",
        category="Access",
        action_type="Restored",
        entity_type="signup_request",
        entity_id=req.id,
        entity_name=req.name,
        subject_employee_id=employee.id if employee else None,
        subject_name=req.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", previous_status, "rejected"),
            audit_service.field_diff("Login access", "active", "disabled" if user else None),
            audit_service.field_diff("Employee status", "active", "inactive" if employee else None),
            audit_service.field_diff("Reason", None, body.reason),
        ),
        summary=(
            f"Revoked approved signup for {req.name} ({req.email}) — login disabled "
            f"and employee record deactivated"
        ),
        request=http_request,
    )
    db.commit()
    logger.info("[signup-request] Undo-approve id=%s email=%s — account deactivated", req.id, req.email)
    return {"message": "Approval revoked. Employee account has been deactivated.", "request_id": request_id}
