from sqlalchemy import Column, Integer, Date, Text, TIMESTAMP, UniqueConstraint, Index
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from app.db.database import Base
from datetime import timezone

class UTCDateTime(TypeDecorator):
    impl = TIMESTAMP
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value

class DailyCheckIn(Base):
    __tablename__ = "daily_checkins"
    __table_args__ = (
        UniqueConstraint("employee_id", "checkin_date", name="uq_daily_checkin_employee_date"),
        Index("idx_daily_checkin_projects", "project_ids", postgresql_using="gin"),
        {"postgresql_partition_by": "RANGE (checkin_date)"}
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    employee_id = Column(Integer, nullable=False, index=True)
    checkin_date = Column(Date, primary_key=True, nullable=False, index=True)
    work_mode = Column(Text, nullable=False)  # WFO, WFH
    project_ids = Column(JSONB, default=[])
    mood = Column(Text, nullable=True)  # great, okay, low, stressed
    checked_in_at = Column(UTCDateTime, nullable=True)
    checked_out_at = Column(UTCDateTime, nullable=True)
    pm_confirmed_at = Column(UTCDateTime, nullable=True)
    pm_confirmed_by = Column(Integer, nullable=True)

    created_at = Column(UTCDateTime, server_default=func.now())
    updated_at = Column(UTCDateTime, server_default=func.now(), onupdate=func.now())
