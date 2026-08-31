from sqlalchemy import Column, Integer, Date, Text, TIMESTAMP, JSON, UniqueConstraint
from sqlalchemy.sql import func
from app.db.database import Base


class DailyCheckIn(Base):
    __tablename__ = "daily_checkins"
    __table_args__ = (
        UniqueConstraint("employee_id", "checkin_date", name="uq_daily_checkin_employee_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    checkin_date = Column(Date, nullable=False, index=True)
    work_mode = Column(Text, nullable=False)  # WFO, WFH
    project_ids = Column(JSON, default=[])
    mood = Column(Text, nullable=True)  # great, okay, low, stressed
    checked_in_at = Column(TIMESTAMP, nullable=True)
    checked_out_at = Column(TIMESTAMP, nullable=True)
    pm_confirmed_at = Column(TIMESTAMP, nullable=True)
    pm_confirmed_by = Column(Integer, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
