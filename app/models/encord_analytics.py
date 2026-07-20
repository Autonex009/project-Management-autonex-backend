"""
Encord analytics time-series.

One row per (Encord project, day, user, workflow stage), populated by the daily
Encord sync (app/services/encord_sync_service.py) from `project.list_time_spent`.
All portal analytics are aggregated from these rows — the portal never queries
Encord live.
"""
from sqlalchemy import Column, Integer, String, Text, Date, TIMESTAMP, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class EncordDailyTimeSpent(Base):
    __tablename__ = "encord_daily_time_spent"

    id = Column(Integer, primary_key=True, index=True)

    sub_project_id = Column(Integer, ForeignKey("daily_sheets.id"), nullable=True, index=True)
    encord_project_hash = Column(Text, nullable=False, index=True)

    metric_date = Column(Date, nullable=False, index=True)          # the day the time was spent
    user_email = Column(Text, nullable=False, index=True)
    project_user_role = Column(String(32), nullable=True)            # e.g. "ANNOTATOR", "REVIEWER"
    workflow_stage = Column(Text, nullable=True)                     # stage title, may be null

    time_spent_seconds = Column(Integer, nullable=False, default=0)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "encord_project_hash", "metric_date", "user_email", "workflow_stage",
            name="uq_encord_day_user_stage",
        ),
    )
