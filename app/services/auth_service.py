"""
Authentication service: JWT token creation/verification + password hashing.
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User, RefreshToken

# ── Config ──────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("FATAL ERROR: JWT_SECRET_KEY environment variable is missing. Refusing to start.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15  # reduced to 15 min for refresh token strategy
PASSWORD_RESET_EXPIRE_MINUTES = int(os.getenv("PASSWORD_RESET_EXPIRE_MINUTES", "15"))

# ── Password hashing ───────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def _truncate_password(password: str) -> str:
    return password.encode("utf-8")[:72].decode("utf-8", errors="ignore")

def hash_password(password: str) -> str:
    return pwd_context.hash(_truncate_password(password))

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(_truncate_password(plain_password), hashed_password)

# ── JWT ─────────────────────────────────────────────────────────────
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    """Decode and return the JWT payload. Raises JWTError on failure."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

def create_password_reset_token(user_id: int, expires_delta: Optional[timedelta] = None) -> tuple[str, datetime]:
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES))
    token = jwt.encode(
        {
            "sub": str(user_id),
            "exp": expire,
            "purpose": "password_reset",
            "jti": uuid.uuid4().hex,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return token, expire

def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

import secrets

def create_refresh_token(user_id: int, db: Session, expires_days: int = 7) -> str:
    """Generates a secure random refresh token and saves it to the database."""
    token = secrets.token_urlsafe(64)
    expires_at = datetime.utcnow() + timedelta(days=expires_days)
    db_token = RefreshToken(user_id=user_id, token=token, expires_at=expires_at)
    db.add(db_token)
    db.commit()
    return token

def verify_and_delete_refresh_token(token: str, db: Session) -> Optional[int]:
    """Verifies a refresh token. If valid, deletes it (to prevent reuse) and returns the user_id.
    The caller must immediately issue a new refresh token (token rotation)."""
    db_token = db.query(RefreshToken).filter(RefreshToken.token == token).first()
    if not db_token:
        return None
    user_id = db_token.user_id
    expires_at = db_token.expires_at
    # Delete the token immediately (Rotation)
    db.delete(db_token)
    db.commit()

    if datetime.utcnow() > expires_at:
        return None
    return user_id

# ── In-memory blacklist (will be replaced by Redis if needed) ──────────────────
# _blacklisted_tokens: set = set()
#
# def blacklist_token(token: str):
#     _blacklisted_tokens.add(token)
#
# def is_token_blacklisted(token: str) -> bool:
#     return token in _blacklisted_tokens

import json
import threading
from app.services.project_cache import InMemoryTTLCache, get_redis_client

# 5-minute RAM TTL cache for authenticated users to protect the 8-slot DB pool
_user_auth_cache = InMemoryTTLCache(ttl_seconds=300.0, max_size=2000)
_user_fetch_lock = threading.Lock()


def _cache_user_in_redis(user: User) -> None:
    """Store authenticated user in L2 Redis (15-min TTL matching JWT expiry)."""
    redis_client = get_redis_client()
    if not redis_client:
        return
    try:
        payload = {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "employee_id": user.employee_id,
            "skills": user.skills,
            "is_active": user.is_active,
            "must_change_password": getattr(user, "must_change_password", False),
            "employment_type": getattr(user, "employment_type", None),
            "work_description": getattr(user, "work_description", None),
            "password_hash": getattr(user, "password_hash", ""),
            "razorpay_email": getattr(user, "razorpay_email", None),
        }
        redis_client.set(f"auth:user:{user.id}", json.dumps(payload), ex=900)
    except Exception:
        pass


def _get_user_from_redis(user_id: int) -> Optional[User]:
    """Retrieve user from L2 Redis."""
    redis_client = get_redis_client()
    if not redis_client:
        return None
    try:
        raw = redis_client.get(f"auth:user:{user_id}")
        if raw:
            d = json.loads(raw)
            return User(
                id=d["id"],
                email=d["email"],
                name=d["name"],
                role=d["role"],
                employee_id=d.get("employee_id"),
                skills=d.get("skills"),
                is_active=d.get("is_active", True),
                must_change_password=d.get("must_change_password", False),
                employment_type=d.get("employment_type"),
                work_description=d.get("work_description"),
                password_hash=d.get("password_hash", ""),
                razorpay_email=d.get("razorpay_email"),
            )
    except Exception:
        pass
    return None


def cache_authenticated_user(user: User) -> None:
    """Pre-warm L1 RAM and L2 Redis when user logs in so initial page loads are instantaneous."""
    _user_auth_cache.set(user.id, _clone_user(user))
    _cache_user_in_redis(user)


def invalidate_user_auth_cache(user_id: Optional[int] = None) -> None:
    """Invalidate cached auth user in RAM and Redis."""
    _user_auth_cache.invalidate(user_id)
    redis_client = get_redis_client()
    if redis_client:
        try:
            if user_id is not None:
                redis_client.delete(f"auth:user:{user_id}")
            else:
                for k in redis_client.scan_iter("auth:user:*"):
                    redis_client.delete(k)
        except Exception:
            pass


def _clone_user(u: User) -> User:
    """Create a clean detached User copy to prevent cross-request mutation."""
    return User(
        id=u.id,
        email=u.email,
        password_hash=getattr(u, "password_hash", ""),
        name=u.name,
        role=u.role,
        employee_id=u.employee_id,
        skills=u.skills,
        is_active=u.is_active,
        must_change_password=getattr(u, "must_change_password", False),
        employment_type=getattr(u, "employment_type", None),
        work_description=getattr(u, "work_description", None),
        razorpay_email=getattr(u, "razorpay_email", None),
    )


# ── Dependencies ────────────────────────────────────────────────────
def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency – extracts the current user from the JWT.
    Use in route signatures: user: User = Depends(get_current_user)
    Uses Dual-Tier L1 RAM (<0.001ms) + L2 Redis (~0.8ms) with single-flight mutex
    to prevent connection pool starvation during simultaneous dashboard loads.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if token is None:
        token = request.cookies.get("access_token")

    if token is None:
        raise credentials_exception

    # if is_token_blacklisted(token):
    #     raise credentials_exception

    try:
        payload = decode_token(token)
        sub = payload.get("sub")
        if sub is None:
            raise credentials_exception
        user_id = int(sub)
    except (JWTError, ValueError):
        raise credentials_exception

    # 1. Tier 1: Check L1 RAM (< 0.001 ms)
    cached_user = _user_auth_cache.get(user_id)
    if cached_user is not None:
        user = _clone_user(cached_user)
    else:
        # 2. Tier 2: Check L2 Redis (~0.8 ms)
        redis_user = _get_user_from_redis(user_id)
        if redis_user is not None and redis_user.is_active:
            _user_auth_cache.set(user_id, _clone_user(redis_user))
            user = _clone_user(redis_user)
        else:
            # 3. Tier 3: PostgreSQL Database Fallback (Protected by Single-Flight Mutex)
            with _user_fetch_lock:
                # Double-check if another concurrent thread already resolved it
                double_check = _user_auth_cache.get(user_id)
                if double_check is not None:
                    user = _clone_user(double_check)
                else:
                    user_record = db.query(User).filter(User.id == user_id).first()
                    if user_record is None or not user_record.is_active:
                        raise credentials_exception
                    _user_auth_cache.set(user_id, _clone_user(user_record))
                    _cache_user_in_redis(user_record)
                    user = _clone_user(user_record)

    view_role = request.headers.get("X-Role-View")
    if user.role == "hr" and view_role == "pm":
        from sqlalchemy.orm.attributes import set_committed_value
        set_committed_value(user, 'role', 'pm')

    return user

# Roles that read the whole team's records rather than only their own.
#
# These endpoints answer "may I see other people's leave/WFH/allocations?", which is a
# different question from "may I approve them" — a team lead sees everything a PM sees and
# approves nothing. Kept as one list because the alternative is repeating the literal in
# every list endpoint, and a site that misses a role fails in a confusing way: the request
# succeeds but silently narrows to the caller's own records, so the page renders empty
# rather than erroring.
#
# Do NOT use this for writes. Ownership checks (assigning a project's manager, editing
# admin-only employee fields, submitting an evaluation for someone else) must name their
# roles explicitly, and per-project action rights live in services/project_scope.py.
TEAM_READ_ROLES = ("admin", "pm", "hr", "team_lead")


def has_team_read(user: Optional[User]) -> bool:
    return user is not None and user.role in TEAM_READ_ROLES


def require_role(*roles):
    """
    Returns a dependency that checks the user's role.
    Usage: user = Depends(require_role("admin", "pm"))
    """
    def role_checker(user: User = Depends(get_current_user)) -> User:
        if user.role in roles:
            return user
        # HR has combined Admin + PM access: it passes any check that permits
        # either an admin or a pm.
        if user.role == "hr" and ("admin" in roles or "pm" in roles):
            return user
        # A team lead reads everything a PM reads — same portal, same pages — so it
        # passes every pm-gated check here. What it must NOT do is *act*, and that is
        # deliberately not enforced at this layer: role tells you nothing about which
        # project a request touches. Mutating endpoints additionally call
        # app.services.project_scope, which allows an action only for the PM of the
        # project in question. Adding a pm-gated MUTATION without that call grants it
        # to every team lead.
        if user.role == "team_lead" and "pm" in roles:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required role: {', '.join(roles)}",
        )
    return role_checker
