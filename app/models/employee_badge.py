from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    Text,
    TIMESTAMP,
    JSON,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.db.database import Base


class EmployeeBadge(Base):
    __tablename__ = "employee_badges"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    badge_code = Column(String(40), nullable=False, index=True)

    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    expires_at = Column(TIMESTAMP, nullable=True, index=True)

    # active | expired | revoked
    status = Column(String(20), nullable=False, default="active", index=True)

    awarded_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    awarded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    meta = Column(JSON, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "badge_code",
            "period_start",
            "period_end",
            name="uq_employee_badge_period",
        ),
        Index("ix_employee_badges_employee_status", "employee_id", "status"),
    )


class EmployeeBadgeLog(Base):
    __tablename__ = "employee_badge_logs"

    id = Column(Integer, primary_key=True, index=True)

    employee_badge_id = Column(
        Integer, ForeignKey("employee_badges.id", ondelete="SET NULL"), nullable=True, index=True
    )
    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    badge_code = Column(String(40), nullable=False, index=True)

    # awarded | expired | revoked
    action = Column(String(20), nullable=False)

    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    details = Column(JSON, nullable=True)

    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_employee_badge_logs_employee_created", "employee_id", created_at.desc()),
    )