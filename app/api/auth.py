"""
Authentication API: signup, login, logout, forgot-password, reset-password, me.
"""
import logging
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from typing import Optional, List, Literal
from jose import ExpiredSignatureError, JWTError

from app.db.database import get_db
from app.models.user import User, RefreshToken
from app.models.employee import Employee
# Aliased: the Pydantic request schema below is also called SignupRequest.
from app.models.signup_request import SignupRequest as SignupRequestRecord
from app.services.auth_service import (
    hash_password,
    verify_password,
    create_access_token,
    create_password_reset_token,
    decode_token,
    hash_reset_token,
    get_current_user,
    create_refresh_token,
    verify_and_delete_refresh_token,
)
from app.services.email_service import send_password_reset_email
from app.services.identity_validator import check_duplicate_identity, check_duplicate_user_for_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Schemas ─────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    portal: Optional[Literal["admin", "pm", "employee"]] = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    designation: Optional[str] = None
    employee_id: Optional[int] = None
    employee_type: Optional[str] = None
    avatar_url: Optional[str] = None
    skills: Optional[list] = None
    must_change_password: bool = False

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


class MessageResponse(BaseModel):
    message: str
    # Only populated in dev mode (DEV_RETURN_RESET_TOKEN=true) — never expose in production
    reset_token: Optional[str] = None
    reset_link: Optional[str] = None


DESIGNATION_ACCESS = {
    "Admin": "admin",
    "HR": "hr",   # combined Admin + PM access (see require_role)
    "Program Manager": "pm",
    # Team leads live in the PM portal and see everything a PM sees; they simply
    # cannot approve/edit. Kept in step with DESIGNATION_ROLE_MAP in api/employees.py
    # — this map decides the token role, that one decides the stored users.role, and
    # a team lead missing from either lands in the employee portal instead.
    "Team Lead": "team_lead",
    "Annotator/ Reviewer": "employee",
    "Annotator/Reviewer": "employee",
    "Annotator": "employee",
    "Reviewer": "employee",
    "Developer": "employee",
}


def _lookup_employee(user: User, db: Session) -> Optional[Employee]:
    employee = None
    if user.employee_id:
        employee = db.query(Employee).filter(Employee.id == user.employee_id).first()
    if employee is None:
        employee = db.query(Employee).filter(Employee.email == user.email).first()
    return employee


def get_user_designation(user: User, db: Session) -> Optional[str]:
    employee = _lookup_employee(user, db)
    if employee and employee.designation:
        return employee.designation
    if user.role == "admin":
        return "Admin"
    return None


def get_access_role(designation: Optional[str], fallback_role: str) -> str:
    return DESIGNATION_ACCESS.get(designation, fallback_role)


def build_user_response(user: User, db: Session) -> UserResponse:
    designation = get_user_designation(user, db)
    access_role = get_access_role(designation, user.role)
    employee = _lookup_employee(user, db)
    return UserResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=access_role,
        designation=designation,
        employee_id=user.employee_id,
        employee_type=employee.employee_type if employee else None,
        avatar_url=employee.avatar_url if employee else None,
        skills=user.skills,
        must_change_password=bool(user.must_change_password),
    )


def get_frontend_base_url(request: Request) -> str:
    return (
        os.getenv("RESET_PASSWORD_FRONTEND_URL")
        or os.getenv("FRONTEND_URL")
        or request.headers.get("origin")
        or "http://localhost:5173"
    ).strip().rstrip("/")


def _dev_mode() -> bool:
    """Return True when DEV_RETURN_RESET_TOKEN=true — exposes reset token in API response."""
    return os.getenv("DEV_RETURN_RESET_TOKEN", "false").lower() == "true"


# ── Endpoints ───────────────────────────────────────────────────────

import re

def validate_password_strength(password: str):
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if not re.search(r"\d", password):
        raise HTTPException(status_code=400, detail="Password must contain at least one number.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        raise HTTPException(status_code=400, detail="Password must contain at least one special character.")

@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with email + password, returns JWT."""
    logger.info("[login] Attempt: email=%s portal=%s", body.email, body.portal)

    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        # Say WHICH thing is wrong, and where the email stands. The generic
        # "invalid email or password" is the textbook answer because it hides
        # whether an address is registered, but on a closed staff portal that
        # secrecy only cost people time — they could not tell a typo from a
        # missing account. `field` tells the form which input to mark.
        logger.warning("[login] User not found: %s", body.email)
        employee = db.query(Employee).filter(Employee.email == body.email).first()
        request_row = (
            db.query(SignupRequestRecord)
            .filter(SignupRequestRecord.email == body.email)
            .first()
        )
        request_status = (request_row.status or "").lower() if request_row else None

        if request_status == "pending":
            detail = {
                "code": "signup_pending",
                "field": "email",
                "message": "Your access request is still waiting for approval. You'll be able to sign in once an admin approves it.",
            }
        elif request_status == "rejected":
            detail = {
                "code": "signup_rejected",
                "field": "email",
                "message": "Your access request was declined. Contact an admin if you think that's a mistake.",
            }
        elif employee:
            detail = {
                "code": "no_login_yet",
                "field": "email",
                "message": "This email is on the employee roster but has no login yet. Use Request Access or ask an admin to create one.",
            }
        else:
            detail = {
                "code": "email_not_found",
                "field": "email",
                "message": "No account exists for this email. Check the spelling, or use Request Access.",
            }
        raise HTTPException(status_code=401, detail=detail)

    logger.debug("[login] User found: id=%s is_active=%s role=%s", user.id, user.is_active, user.role)

    password_ok = verify_password(body.password, user.password_hash)
    logger.debug("[login] bcrypt.verify result: %s for user id=%s", password_ok, user.id)

    if not password_ok:
        # The email is confirmed good at this point, so name the real problem.
        logger.warning("[login] Wrong password for email=%s", body.email)
        raise HTTPException(
            status_code=401,
            detail={
                "code": "wrong_password",
                "field": "password",
                "message": "Wrong password for this email. Try again or use Reset Password.",
            },
        )

    if not user.is_active:
        logger.warning("[login] Deactivated account: email=%s", body.email)
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_deactivated",
                "field": "email",
                "message": "This account has been deactivated. Contact an admin to restore access.",
            },
        )

    # Auto-link PM/employee users to an Employee record if not yet linked
    if user.employee_id is None:
        employee = db.query(Employee).filter(Employee.email == user.email).first()
        if employee is None and user.role in ("pm", "team_lead", "employee"):
            # Create a fresh Employee record for this user. The designation has to
            # match the role we already trust, or the next employee update would read
            # the designation back and silently rewrite the role (see
            # api/employees.py — a team lead defaulted to "Program Manager" here would
            # be promoted to a real PM by an unrelated profile edit).
            _designation_for_role = {
                "pm": "Program Manager",
                "team_lead": "Team Lead",
            }
            employee = Employee(
                name=user.name,
                email=user.email,
                employee_type="Full-time",
                designation=_designation_for_role.get(user.role, "Annotator/ Reviewer"),
                status="active",
            )
            db.add(employee)
            db.flush()
        if employee is not None:
            check_duplicate_user_for_employee(db, employee.id, exclude_user_id=user.id)
            user.employee_id = employee.id
            db.commit()
            db.refresh(user)
            logger.info("[login] Auto-linked user id=%s to employee id=%s", user.id, user.employee_id)

    response_user = build_user_response(user, db)
    # HR uses the Admin login page but carries its own combined role.
    # Team leads use the PM login page: same portal, same layout, view-only once
    # inside. Without this they would be turned away as a "wrong portal" account.
    portal_ok = (
        not body.portal
        or response_user.role == body.portal
        or (response_user.role == "hr" and body.portal == "admin")
        or (response_user.role == "team_lead" and body.portal == "pm")
    )
    if not portal_ok:
        logger.warning(
            "[login] Portal mismatch: email=%s role=%s requested_portal=%s",
            body.email, response_user.role, body.portal,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "wrong_portal",
                "field": "email",
                "message": f"Correct email and password, but this is a {response_user.role} account — sign in through the {response_user.role} portal instead.",
            },
        )

    token = create_access_token({
        "sub": str(user.id),
        "role": response_user.role,
        "designation": response_user.designation,
        "employee_id": user.employee_id,
        "must_change_password": bool(user.must_change_password),
    })
    refresh_token = create_refresh_token(user.id, db)

    # Return JSONResponse to include both cookie and JSON body
    response = JSONResponse(content={
        "token": token,
        "user": response_user.model_dump(),
    })

    is_prod = os.getenv("ENVIRONMENT", "development") != "development"
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=15 * 60,  # 15 mins
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,  # 7 days
        path="/",
    )
    return response


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(body: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """
    Request a password reset link.

    Production:  Sends email via SMTP. Requires SMTP_HOST / SMTP_USER / SMTP_PASSWORD.
    Dev/testing: Set DEV_RETURN_RESET_TOKEN=true to skip email and return the token directly.
    """
    logger.info("[forgot-password] Request for email=%s", body.email)

    # Say whether the address is actually registered, and distinguish "no such
    # account" from "account switched off" — the old single generic reply hid both
    # and left people waiting for mail that was never going to arrive. It was
    # there to stop the endpoint confirming which addresses exist; that guard is
    # deliberately dropped here, matching the login form.
    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        logger.warning("[forgot-password] Email not found: %s", body.email)
        raise HTTPException(
            status_code=404,
            detail={
                "code": "email_not_found",
                "field": "email",
                "message": "No account exists for this email.",
            },
        )

    if not user.is_active:
        logger.warning("[forgot-password] Inactive account: %s", body.email)
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_deactivated",
                "field": "email",
                "message": "This account has been deactivated, so its password can't be reset. Contact an admin.",
            },
        )

    logger.debug("[forgot-password] Generating reset token for user id=%s", user.id)
    reset_token, expires_at = create_password_reset_token(user.id)
    token_hash = hash_reset_token(reset_token)
    logger.debug("[forgot-password] Token hash (sha256): %s...  expires_at=%s", token_hash[:12], expires_at)

    user.password_reset_token_hash = token_hash
    user.password_reset_expires_at = expires_at
    db.add(user)
    db.commit()

    reset_link = (
        f"{get_frontend_base_url(request)}/reset-password"
        f"?token={reset_token}"
        f"&role={get_access_role(get_user_designation(user, db), user.role)}"
    )

    # ── Dev mode: skip email and return token directly ──────────────
    if _dev_mode():
        logger.warning(
            "[forgot-password] DEV_RETURN_RESET_TOKEN=true — returning token in response. "
            "NEVER enable this in production!"
        )
        return MessageResponse(
            message="[DEV MODE] Reset token generated. Use reset_token or reset_link below.",
            reset_token=reset_token,
            reset_link=reset_link,
        )

    # ── Production: send email ──────────────────────────────────────
    try:
        logger.info("[forgot-password] Sending reset email to %s", user.email)
        send_password_reset_email(to_email=user.email, to_name=user.name, reset_link=reset_link)
        logger.info("[forgot-password] Email sent to %s", user.email)
    except Exception as exc:
        logger.error("[forgot-password] Email send failed for %s: %s", user.email, exc)
        # Roll back token so user can retry
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        db.add(user)
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Failed to send reset email. Please try again later.",
        ) from exc

    # The address is confirmed at this point, so name it: people check the wrong
    # mailbox otherwise.
    return MessageResponse(
        message=f"Reset link sent to {user.email}. It expires in 15 minutes."
    )


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(
    body: ResetPasswordRequest,
    token: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    """
    Reset password using a token from forgot-password.
    Pass the token as a query parameter: POST /api/auth/reset-password?token=<token>
    Body: { "password": "<new_password>" }
    """
    logger.info("[reset-password] Attempt with token (first 12 chars): %s...", token[:12])

    validate_password_strength(body.password)

    # Decode and validate JWT
    try:
        payload = decode_token(token)
    except ExpiredSignatureError:
        logger.warning("[reset-password] Token expired")
        raise HTTPException(status_code=400, detail="Reset link has expired")
    except JWTError as exc:
        logger.warning("[reset-password] Invalid JWT: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    if payload.get("purpose") != "password_reset":
        logger.warning("[reset-password] Wrong token purpose: %s", payload.get("purpose"))
        raise HTTPException(status_code=400, detail="Invalid reset link")

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid reset link")

    # Look up user
    user = db.query(User).filter(User.id == user_id).first()
    logger.debug("[reset-password] User lookup: id=%s found=%s", user_id, user is not None)
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid reset link")

    # Verify stored token hash
    if not user.password_reset_token_hash or not user.password_reset_expires_at:
        logger.warning("[reset-password] No pending reset token for user id=%s", user_id)
        raise HTTPException(status_code=400, detail="Invalid reset link")

    incoming_hash = hash_reset_token(token)
    logger.debug(
        "[reset-password] Hash comparison: incoming=%s... stored=%s...",
        incoming_hash[:12], user.password_reset_token_hash[:12],
    )
    if user.password_reset_token_hash != incoming_hash:
        logger.warning("[reset-password] Token hash mismatch for user id=%s", user_id)
        raise HTTPException(status_code=400, detail="Invalid reset link")

    # Secondary expiry check (belt-and-suspenders alongside JWT exp)
    if user.password_reset_expires_at < datetime.utcnow():
        logger.warning("[reset-password] Token expired (DB check) for user id=%s", user_id)
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        db.add(user)
        db.commit()
        raise HTTPException(status_code=400, detail="Reset link has expired")

    # Hash and store new password
    logger.info("[reset-password] Hashing and storing new password for user id=%s", user_id)
    user.password_hash = hash_password(body.password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    db.add(user)
    db.commit()

    logger.info("[reset-password] Password reset successful for user id=%s email=%s", user_id, user.email)
    return MessageResponse(message="Password reset successful. You can now sign in with your new password.")


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    """Invalidate current tokens."""
    # Delete refresh token from DB if present
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        verify_and_delete_refresh_token(refresh_token, db)
        
    response = JSONResponse(content={"message": "Logged out successfully"})
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return response

@router.post("/refresh")
def refresh_access_token(request: Request, db: Session = Depends(get_db)):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token missing")
    
    user_id = verify_and_delete_refresh_token(refresh_token, db)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
        
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive or deleted")
        
    response_user = build_user_response(user, db)
    
    access_token = create_access_token({
        "sub": str(user.id),
        "role": response_user.role,
        "designation": response_user.designation,
        "employee_id": user.employee_id,
    })
    new_refresh_token = create_refresh_token(user.id, db)
    
    response = JSONResponse(content={
        "token": access_token,
        "user": response_user.model_dump(),
    })
    
    is_prod = os.getenv("ENVIRONMENT", "development") != "development"
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=15 * 60,  # 15 mins
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,  # 7 days
        path="/",
    )
    return response


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the currently authenticated user profile."""
    return build_user_response(user, db)


@router.get("/verify")
def verify_token(request: Request):
    """Quick check: is the bearer token still valid?"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"valid": False, "reason": "No token provided"}
    token = auth_header[7:]
    
    # TODO: Check if token is blacklisted in Redis (Memory blacklist was removed)
    # if is_token_blacklisted(token):
    #     return {"valid": False, "reason": "Token invalidated"}
        
    return {"valid": True}


@router.post("/change-password", response_model=UserResponse)
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Allow an authenticated user to change their password.
    If must_change_password is False, current_password is required and verified.
    """
    validate_password_strength(body.new_password)

    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # If the user is NOT in forced first-time reset mode, verify current password
    if not user.must_change_password:
        if not body.current_password:
            raise HTTPException(
                status_code=400,
                detail="Current password is required.",
            )
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(
                status_code=400,
                detail="Current password does not match.",
            )
    else:
        # If current_password is provided in forced reset, verify it
        if body.current_password and not verify_password(body.current_password, user.password_hash):
            raise HTTPException(
                status_code=400,
                detail="Current password does not match.",
            )

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("[change-password] User id=%s changed password successfully", user.id)
    return build_user_response(user, db)


