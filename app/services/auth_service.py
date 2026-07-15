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
from app.models.user import User

# ── Config ──────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "autonex-super-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
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

# ── In-memory blacklist (use Redis in production) ──────────────────
_blacklisted_tokens: set = set()

def blacklist_token(token: str):
    _blacklisted_tokens.add(token)

def is_token_blacklisted(token: str) -> bool:
    return token in _blacklisted_tokens

# ── Dependencies ────────────────────────────────────────────────────
def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency – extracts the current user from the JWT.
    Use in route signatures: user: User = Depends(get_current_user)
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

    if is_token_blacklisted(token):
        raise credentials_exception

    try:
        payload = decode_token(token)
        sub = payload.get("sub")
        if sub is None:
            raise credentials_exception
        user_id = int(sub)
    except (JWTError, ValueError):
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise credentials_exception

    return user

def require_role(*roles):
    """
    Returns a dependency that checks the user's role.
    Usage: user = Depends(require_role("admin", "pm"))
    """
    def role_checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role: {', '.join(roles)}",
            )
        return user
    return role_checker
