"""
User model for authentication.
Supports roles: admin, pm, employee.
Links to Employee model via employee_id for PM/Employee users.
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, TIMESTAMP, JSON, ForeignKey
from sqlalchemy.sql import func

from app.db.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    
    # Role: admin, pm, employee
    role = Column(String(20), nullable=False, default="employee")
    
    # Link to employee record (for pm/employee users)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    
    # Skills stored as JSON array (used during signup)
    skills = Column(JSON, nullable=True)
    
    is_active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False, nullable=False)
    employment_type = Column(String(50), nullable=True)
    password_reset_token_hash = Column(String(64), nullable=True)
    password_reset_expires_at = Column(TIMESTAMP, nullable=True)
    
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now()
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    expires_at = Column(TIMESTAMP, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

