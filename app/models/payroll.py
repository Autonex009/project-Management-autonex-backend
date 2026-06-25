from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, Text, TIMESTAMP, ForeignKey, UniqueConstraint
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


class PayrollBonus(Base):
    """A discretionary bonus granted to one employee within a payroll run.

    The amount is capped at the employee's bonus limit (the `opt_bonus_monthly`
    figure from the salary table) and is added on top of their final salary.
    """
    __tablename__ = "payroll_bonuses"

    id = Column(Integer, primary_key=True, index=True)
    payroll_run_id = Column(Integer, ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False, default=0.0)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("payroll_run_id", "employee_id", name="uq_payroll_bonus_run_emp"),)


class PayrollAdditionalPayment(Base):
    """A free-form additional payment for one employee within a payroll run.

    Unlike a bonus there is no cap — it's any extra amount the admin enters, and
    it's added on top of the final salary.
    """
    __tablename__ = "payroll_additional_payments"

    id = Column(Integer, primary_key=True, index=True)
    payroll_run_id = Column(Integer, ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False, default=0.0)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("payroll_run_id", "employee_id", name="uq_payroll_addl_run_emp"),)


class Salary(Base):
    """Read-only view of the externally-managed `salary` table.

    Holds each person's actual pay (e.g. "₹100,000"), stored as text. The app
    only reads from it for the Pay tab — it never writes back. (A masked mirror
    of the same data lives in `masked_salaries`, which we deliberately ignore.)
    """
    __tablename__ = "salary"

    id = Column(BigInteger, primary_key=True, index=True)
    full_name = Column(Text, nullable=True)
    status = Column(Text, nullable=True)
    employment_type = Column(Text, nullable=True)
    base_pay_annual = Column(Text, nullable=True)
    optional_bonus_annual = Column(Text, nullable=True)
    base_pay_monthly = Column(Text, nullable=True)
    opt_bonus_monthly = Column(Text, nullable=True)
