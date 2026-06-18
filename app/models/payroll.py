from sqlalchemy import Column, Integer, String, Float, Boolean, Text, TIMESTAMP, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class PayrollRun(Base):
    """One record per month when a payroll is finalized."""
    __tablename__ = "payroll_runs"

    id = Column(Integer, primary_key=True, index=True)
    month = Column(String(7), nullable=False, index=True)       # YYYY-MM
    status = Column(String(20), nullable=False, default="draft") # draft | finalized
    working_days = Column(Integer, nullable=False, default=22)
    notes = Column(Text, nullable=True)
    processed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("month", name="uq_payroll_runs_month"),)


class PayrollLeaveAdjustment(Base):
    """
    Per-leave payroll decision for a given run.
    deduct=True  → deduct salary for this leave
    deduct=False → treat leave as paid (no deduction)
    """
    __tablename__ = "payroll_leave_adjustments"

    id = Column(Integer, primary_key=True, index=True)
    payroll_run_id = Column(Integer, ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    leave_id = Column(Integer, ForeignKey("leaves.id", ondelete="CASCADE"), nullable=False)
    deduct = Column(Boolean, nullable=False, default=True)
    # Snapshot of unpaid working-days for this leave within the run's month.
    # Null on legacy rows → fall back to (days_in_month if deduct else 0).
    unpaid_days = Column(Float, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("payroll_run_id", "leave_id", name="uq_payroll_adj_run_leave"),)
