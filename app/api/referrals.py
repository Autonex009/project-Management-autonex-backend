"""
Employee Referral API
---------------------
Internal routes (JWT Bearer auth):
  POST   /api/referrals              — employee submits a referral
  GET    /api/referrals              — list (admin: all, employee: own)
  GET    /api/referrals/{id}         — single referral
  PATCH  /api/referrals/{id}/status  — admin updates status
  DELETE /api/referrals/{id}         — employee withdraws (pending only) / admin deletes

External (hiring software) route (API-key auth):
  GET    /api/external/referrals     — all referral candidates for ATS integration
    Headers : X-API-Key: <REFERRAL_API_KEY>
    Params  : status (filter), since (ISO date), position (filter)
"""
import logging
import os
from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.notification import Notification
from app.models.referral import Referral
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.email_service import try_send_referral_confirmation_email, try_send_referral_status_update_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["referrals"])

# ── Valid status transitions ──────────────────────────────────────────────────
VALID_STATUSES = {"pending", "reviewing", "interview_scheduled", "hired", "rejected"}

STATUS_LABELS = {
    "pending": "Pending Review",
    "reviewing": "Under Review",
    "interview_scheduled": "Interview Scheduled",
    "hired": "Hired",
    "rejected": "Not Moving Forward",
}


# ── Schemas ───────────────────────────────────────────────────────────────────

class ReferralCreate(BaseModel):
    candidate_name: str
    candidate_email: EmailStr
    candidate_phone: Optional[str] = None
    candidate_linkedin: Optional[str] = None
    position_applied: str
    department: Optional[str] = None
    relationship: str
    note: Optional[str] = None


class StatusUpdate(BaseModel):
    status: str
    status_note: Optional[str] = None


class ReferralResponse(BaseModel):
    id: int
    referrer_id: Optional[int]
    referrer_name: Optional[str] = None
    referrer_email: Optional[str] = None
    candidate_name: str
    candidate_email: str
    candidate_phone: Optional[str]
    candidate_linkedin: Optional[str]
    position_applied: str
    department: Optional[str]
    relationship: str
    note: Optional[str]
    status: str
    status_label: Optional[str] = None
    status_note: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _push_notification(db: Session, user_id: int, title: str, message: str, notif_type: str = "referral"):
    db.add(Notification(user_id=user_id, title=title, message=message, type=notif_type))


def _enrich(ref: Referral, db: Session) -> ReferralResponse:
    referrer_name = None
    referrer_email = None
    if ref.referrer_id:
        emp = db.query(Employee).filter(Employee.id == ref.referrer_id).first()
        if emp:
            referrer_name = emp.name
            referrer_email = emp.email
    return ReferralResponse(
        **{c.name: getattr(ref, c.name) for c in ref.__table__.columns},
        referrer_name=referrer_name,
        referrer_email=referrer_email,
        status_label=STATUS_LABELS.get(ref.status, ref.status),
    )


# ── Internal routes ───────────────────────────────────────────────────────────

@router.post("/api/referrals", response_model=ReferralResponse, status_code=201)
def submit_referral(
    body: ReferralCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Employee submits a candidate referral."""
    # Resolve referrer employee record
    referrer_id = None
    referrer_name = current_user.name
    referrer_email = current_user.email
    if current_user.employee_id:
        referrer_id = current_user.employee_id
    else:
        emp = db.query(Employee).filter(Employee.email == current_user.email).first()
        if emp:
            referrer_id = emp.id

    ref = Referral(
        referrer_id=referrer_id,
        candidate_name=body.candidate_name,
        candidate_email=body.candidate_email.lower(),
        candidate_phone=body.candidate_phone,
        candidate_linkedin=body.candidate_linkedin,
        position_applied=body.position_applied,
        department=body.department,
        relationship=body.relationship,
        note=body.note,
        status="pending",
    )
    db.add(ref)
    db.flush()

    # Notify all admins
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for admin in admins:
        _push_notification(
            db, admin.id,
            title="New Candidate Referral",
            message=f"{referrer_name} referred {body.candidate_name} for '{body.position_applied}'.",
        )

    db.commit()
    db.refresh(ref)

    # Email confirmation to referrer
    try_send_referral_confirmation_email(
        referrer_name=referrer_name,
        referrer_email=referrer_email,
        candidate_name=body.candidate_name,
        position=body.position_applied,
    )

    return _enrich(ref, db)


@router.get("/api/referrals", response_model=List[ReferralResponse])
def list_referrals(
    referrer_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Admin: all referrals (optionally filtered).
    Employee/PM: only their own referrals.
    """
    q = db.query(Referral)

    if current_user.role not in ("admin", "hr"):
        # Scope to logged-in employee's own referrals
        emp_id = current_user.employee_id
        if not emp_id:
            emp = db.query(Employee).filter(Employee.email == current_user.email).first()
            emp_id = emp.id if emp else -1
        q = q.filter(Referral.referrer_id == emp_id)
    elif referrer_id:
        q = q.filter(Referral.referrer_id == referrer_id)

    if status_filter and status_filter in VALID_STATUSES:
        q = q.filter(Referral.status == status_filter)

    refs = q.order_by(Referral.created_at.desc()).all()
    return [_enrich(r, db) for r in refs]


@router.get("/api/referrals/{referral_id}", response_model=ReferralResponse)
def get_referral(
    referral_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ref = db.query(Referral).filter(Referral.id == referral_id).first()
    if not ref:
        raise HTTPException(status_code=404, detail="Referral not found")

    # Non-admins can only view their own
    if current_user.role not in ("admin", "hr"):
        emp_id = current_user.employee_id
        if not emp_id:
            emp = db.query(Employee).filter(Employee.email == current_user.email).first()
            emp_id = emp.id if emp else -1
        if ref.referrer_id != emp_id:
            raise HTTPException(status_code=403, detail="Access denied")

    return _enrich(ref, db)


@router.patch("/api/referrals/{referral_id}/status", response_model=ReferralResponse)
def update_referral_status(
    referral_id: int,
    body: StatusUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Admin-only: update referral status and optionally add a note."""
    if current_user.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin access required")

    if body.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
        )

    ref = db.query(Referral).filter(Referral.id == referral_id).first()
    if not ref:
        raise HTTPException(status_code=404, detail="Referral not found")

    old_status = ref.status
    ref.status = body.status
    if body.status_note is not None:
        ref.status_note = body.status_note

    # Notify referrer
    if ref.referrer_id:
        referrer_user = db.query(User).join(Employee, User.employee_id == Employee.id).filter(
            Employee.id == ref.referrer_id
        ).first()
        if not referrer_user:
            referrer_emp = db.query(Employee).filter(Employee.id == ref.referrer_id).first()
            if referrer_emp:
                referrer_user = db.query(User).filter(User.email == referrer_emp.email).first()

        if referrer_user:
            _push_notification(
                db, referrer_user.id,
                title="Referral Status Updated",
                message=f"Your referral for {ref.candidate_name} is now: {STATUS_LABELS.get(body.status, body.status)}.",
            )
            # Email notification
            try_send_referral_status_update_email(
                referrer_name=referrer_user.name,
                referrer_email=referrer_user.email,
                candidate_name=ref.candidate_name,
                position=ref.position_applied,
                new_status=STATUS_LABELS.get(body.status, body.status),
            )

    db.commit()
    db.refresh(ref)
    return _enrich(ref, db)


@router.delete("/api/referrals/{referral_id}", status_code=204)
def delete_referral(
    referral_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Employee: can withdraw only their own pending referrals.
    Admin: can delete any referral.
    """
    ref = db.query(Referral).filter(Referral.id == referral_id).first()
    if not ref:
        raise HTTPException(status_code=404, detail="Referral not found")

    if current_user.role not in ("admin", "hr"):
        emp_id = current_user.employee_id
        if not emp_id:
            emp = db.query(Employee).filter(Employee.email == current_user.email).first()
            emp_id = emp.id if emp else -1
        if ref.referrer_id != emp_id:
            raise HTTPException(status_code=403, detail="Access denied")
        if ref.status != "pending":
            raise HTTPException(status_code=400, detail="Only pending referrals can be withdrawn")

    db.delete(ref)
    db.commit()


# ── External / ATS API ────────────────────────────────────────────────────────

class ExternalReferralResponse(BaseModel):
    referral_id: int
    referrer_name: Optional[str]
    referrer_email: Optional[str]
    candidate_name: str
    candidate_email: str
    candidate_phone: Optional[str]
    candidate_linkedin: Optional[str]
    position_applied: str
    department: Optional[str]
    relationship: str
    note: Optional[str]
    status: str
    status_label: str
    status_note: Optional[str]
    referred_at: str   # ISO-8601


external_router = APIRouter(prefix="/api/external", tags=["external"])


def _verify_api_key(x_api_key: Optional[str] = Header(None)):
    expected = os.getenv("REFERRAL_API_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="External API not configured (REFERRAL_API_KEY not set)",
        )
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


@external_router.get(
    "/referrals",
    response_model=List[ExternalReferralResponse],
    summary="Referred candidates (ATS integration)",
    description=(
        "Returns all employee referrals for consumption by external hiring/ATS software. "
        "Authenticate with `X-API-Key: <REFERRAL_API_KEY>` header. "
        "Filter by `status`, `position`, or `since` (ISO date, e.g. 2025-01-01)."
    ),
)
def external_list_referrals(
    status_filter: Optional[str] = Query(None, alias="status"),
    position: Optional[str] = Query(None),
    since: Optional[date] = Query(None, description="Return only referrals submitted on or after this date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    _: None = Depends(_verify_api_key),
):
    q = db.query(Referral)

    if status_filter and status_filter in VALID_STATUSES:
        q = q.filter(Referral.status == status_filter)
    if position:
        q = q.filter(Referral.position_applied.ilike(f"%{position}%"))
    if since:
        q = q.filter(Referral.created_at >= datetime.combine(since, datetime.min.time()))

    refs = q.order_by(Referral.created_at.desc()).all()

    results = []
    for ref in refs:
        referrer_name = None
        referrer_email = None
        if ref.referrer_id:
            emp = db.query(Employee).filter(Employee.id == ref.referrer_id).first()
            if emp:
                referrer_name = emp.name
                referrer_email = emp.email
        results.append(ExternalReferralResponse(
            referral_id=ref.id,
            referrer_name=referrer_name,
            referrer_email=referrer_email,
            candidate_name=ref.candidate_name,
            candidate_email=ref.candidate_email,
            candidate_phone=ref.candidate_phone,
            candidate_linkedin=ref.candidate_linkedin,
            position_applied=ref.position_applied,
            department=ref.department,
            relationship=ref.relationship,
            note=ref.note,
            status=ref.status,
            status_label=STATUS_LABELS.get(ref.status, ref.status),
            status_note=ref.status_note,
            referred_at=ref.created_at.isoformat() if ref.created_at else "",
        ))

    return results
