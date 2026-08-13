from sqlalchemy import Column, ForeignKey, Integer, String, Text, TIMESTAMP, Index
from sqlalchemy.sql import func

from app.db.database import Base


class EmployeeNote(Base):
    __tablename__ = "employee_notes"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # complaint | warning | recognition
    type = Column(String(20), nullable=False, index=True)

    title = Column(Text, nullable=False)
    content = Column(Text, nullable=False)

    # low | medium | high (optional; mainly for complaint/warning)
    severity = Column(String(20), nullable=True)

    # open | resolved
    status = Column(String(20), nullable=False, default="open")

    issued_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    issued_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    resolved_at = Column(TIMESTAMP, nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_note = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_employee_notes_employee_type", "employee_id", "type"),
        Index("ix_employee_notes_type_status", "type", "status"),
        Index("ix_employee_notes_issued_at", issued_at.desc()),
    )