from sqlalchemy import Column, Integer, Float, Date, ForeignKey, Boolean, Text, JSON, TIMESTAMP, String, Index
from sqlalchemy.sql import func
from app.db.database import Base


class Allocation(Base):
    """
    Enhanced Allocation model supporting:
    - Time-splitting across role tags
    - Mid-project join/release dates
    - Override flagging for forced allocations
    Links to DailySheet (daily_sheets table) via sub_project_id for backward compat.
    """
    __tablename__ = "allocations"
    __table_args__ = (Index('ix_allocations_sub_project_active', 'sub_project_id', 'is_active'),)

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, nullable=False)
    
    # Points to daily_sheets table (kept as sub_project_id for API backward compat)
    sub_project_id = Column(Integer, ForeignKey("daily_sheets.id"))
    
    # === Advanced Allocation Fields ===
    total_daily_hours = Column(Integer, default=8)
    active_start_date = Column(Date, nullable=True)
    active_end_date = Column(Date, nullable=True)
    
    # Role tagging and time distribution
    role_tags = Column(JSON, default=[])
    time_distribution = Column(JSON, default={})
    
    # Override for forced allocations despite warnings
    override_flag = Column(Boolean, default=False)
    override_reason = Column(Text, nullable=True)

    # Soft-hide / Archive fields
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    deactivated_at = Column(TIMESTAMP, nullable=True)
    deactivated_reason = Column(String(50), nullable=True)
    
    # Legacy fields
    weekly_hours_allocated = Column(Float, nullable=True)
    weekly_tasks_allocated = Column(Integer, nullable=True)
    productivity_override = Column(Float, default=1.0)
    effective_week = Column(Date, nullable=True)
    
    # Timestamps
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now()
    )
