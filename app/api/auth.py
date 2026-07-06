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
from app.models.user import User
from app.models.employee import Employee
from app.services.auth_service import (
    hash_password,
    verify_password,
    create_access_token,
    create_password_reset_token,
    decode_token,
    hash_reset_token,
    blacklist_token,
    is_token_blacklisted,
    get_current_user,
)
from app.services.email_service import send_password_reset_email
from app.services.identity_validator import check_duplicate_identity, check_duplicate_user_for_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Schemas ─────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    skills: Optional[List[str]] = None
    role: Optional[str] = "employee"       # admin, pm, employee


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    portal: Optional[Literal["admin", "pm", "employee"]] = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    password: str


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
    "Program Manager": "pm",
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

@router.post("/signup", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    """Register a new user. Defaults to 'employee' role."""
    logger.info("[signup] Attempt: email=%s role=%s", body.email, body.role)

    check_duplicate_identity(db, email=body.email)

    # Only allow 'employee' or 'pm' via signup; admin is seed-only
    role = body.role if body.role in ("employee", "pm") else "employee"

    employee = Employee(
        name=body.name,
        email=body.email,
        employee_type="Full-time",
        designation="Program Manager" if role == "pm" else "Annotator/ Reviewer",
        skills=body.skills or [],
        status="active",
    )
    db.add(employee)
    db.flush()

    user = User(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role=role,
        employee_id=employee.id,
        skills=body.skills or [],
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("[signup] Created user id=%s email=%s role=%s", user.id, user.email, role)
    response_user = build_user_response(user, db)
    token = create_access_token({
        "sub": str(user.id),
        "role": response_user.role,
        "designation": response_user.designation,
        "employee_id": user.employee_id,
    })

    # Return JSONResponse to include both cookie and JSON body
    response = JSONResponse(content={
        "token": token,
        "user": response_user.model_dump(),
    }, status_code=status.HTTP_201_CREATED)

    is_prod = os.getenv("ENVIRONMENT", "development") != "development"
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=60 * 60 * 24,  # 24 hours
        path="/",
    )
    return response


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with email + password, returns JWT."""
    logger.info("[login] Attempt: email=%s portal=%s", body.email, body.portal)

    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        logger.warning("[login] User not found: %s", body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    logger.debug("[login] User found: id=%s is_active=%s role=%s", user.id, user.is_active, user.role)

    password_ok = verify_password(body.password, user.password_hash)
    logger.debug("[login] bcrypt.verify result: %s for user id=%s", password_ok, user.id)

    if not password_ok:
        logger.warning("[login] Wrong password for email=%s", body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        logger.warning("[login] Deactivated account: email=%s", body.email)
        raise HTTPException(status_code=403, detail="Account is deactivated")

    # Auto-link PM/employee users to an Employee record if not yet linked
    if user.employee_id is None:
        employee = db.query(Employee).filter(Employee.email == user.email).first()
        if employee is None and user.role in ("pm", "employee"):
            # Create a fresh Employee record for this user
            employee = Employee(
                name=user.name,
                email=user.email,
                employee_type="Full-time",
                designation="Program Manager" if user.role == "pm" else "Annotator/ Reviewer",
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
    if body.portal and response_user.role != body.portal:
        logger.warning(
            "[login] Portal mismatch: email=%s role=%s requested_portal=%s",
            body.email, response_user.role, body.portal,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. This account must sign in through the {response_user.role} portal.",
        )

    token = create_access_token({
        "sub": str(user.id),
        "role": response_user.role,
        "designation": response_user.designation,
        "employee_id": user.employee_id,
    })

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
        max_age=60 * 60 * 24,  # 24 hours
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
    generic_message = "If an account exists for that email, reset instructions have been sent."
    logger.info("[forgot-password] Request for email=%s", body.email)

    user = db.query(User).filter(User.email == body.email).first()
    if not user or not user.is_active:
        logger.warning("[forgot-password] Email not found or inactive: %s", body.email)
        # Always return the same message to prevent user enumeration
        return MessageResponse(message=generic_message)

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

    return MessageResponse(message=generic_message)


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

    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")

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
def logout(request: Request):
    """Invalidate current token."""
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            
    if token:
        blacklist_token(token)
        logger.info("[logout] Token blacklisted")
        
    response = JSONResponse(content={"message": "Logged out successfully"})
    response.delete_cookie("access_token", path="/")
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
    if is_token_blacklisted(token):
        return {"valid": False, "reason": "Token invalidated"}
    return {"valid": True}
