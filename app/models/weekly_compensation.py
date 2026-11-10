from sqlalchemy import Column, Integer, String, Numeric, Boolean, Text, Date, TIMESTAMP, ForeignKey, UniqueConstraint, Index
from sqlalchemy.sql import func

from app.db.database import Base


class WeeklyCompensationRecord(Base):
    __tablename__ = "weekly_compensation_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_weekly_comp_emp_date"),
        Index("idx_weekly_comp_month_status", "payroll_month", "status"),
        Index("idx_weekly_comp_emp_month", "employee_id", "payroll_month"),
        Index("idx_weekly_comp_work_date", "work_date"),
        Index("idx_weekly_comp_daily_checkin", "daily_checkin_id"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("daily_sheets.id", ondelete="SET NULL"), nullable=True)
    pm_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)

    # Logical reference to partitioned daily_checkins (NO physical FK to avoid partition constraint failures)
    daily_checkin_id = Column(Integer, nullable=True)

    # Date & Grouping (Strict Asia/Kolkata IST)
    work_date = Column(Date, nullable=False)
    payroll_month = Column(String(7), nullable=False)  # Format: 'YYYY-MM'
    week_number = Column(Integer, nullable=False)       # Calendar week 1-53
    is_weekend = Column(Boolean, default=True, nullable=False)

    # The 3 Time Columns (Stored in seconds, formatted as 'Xh Ym' in UI)
    portal_duration_seconds = Column(Integer, nullable=True)        # NULL if missed checkout
    encord_duration_seconds = Column(Integer, default=0, nullable=False)
    final_duration_seconds = Column(Integer, nullable=False)         # Effective payable duration
    is_final_time_overridden = Column(Boolean, default=False, nullable=False)

    # Hourly Financial Engine
    hourly_rate = Column(Numeric(10, 2), nullable=False)             # Snapshotted hourly rate
    calculated_payout = Column(Numeric(10, 2), nullable=False)       # round((final_seconds / 3600.0) * hourly_rate, 2)
    override_payout = Column(Numeric(10, 2), nullable=True)          # Custom amount if overridden

    @property
    def effective_payout(self) -> float:
        if self.override_payout is not None:
            return float(self.override_payout)
        return float(min(self.calculated_payout or 0.0, 1000.0))

    # In-Row Audit Trail (Zero join overhead for tooltip rendering)
    edit_note = Column(Text, nullable=True)
    last_edited_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_edited_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Approval Lifecycle
    status = Column(String(20), default="pending", nullable=False)   # 'pending', 'approved', 'rejected', 'payroll_locked'
    approved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Payroll Linkage & Freeze
    is_locked = Column(Boolean, default=False, nullable=False)
    payroll_run_id = Column(Integer, ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
