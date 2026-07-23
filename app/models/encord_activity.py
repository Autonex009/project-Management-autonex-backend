"""
Encord per-user daily activity (task actions + label creation).

Complements `encord_daily_time_spent` (which stores time only). One row per
(Encord project, day, user), populated by the daily Encord sync from
`project.get_task_actions` (SUBMIT / APPROVE / REJECT) and
`project.get_label_logs` (ADD). Portal analytics aggregate from these rows.
"""
from sqlalchemy import Column, Integer, Text, Date, TIMESTAMP, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class EncordDailyActivity(Base):
    __tablename__ = "encord_daily_activity"

    id = Column(Integer, primary_key=True, index=True)

    sub_project_id = Column(Integer, ForeignKey("daily_sheets.id"), nullable=True, index=True)
    encord_project_hash = Column(Text, nullable=False, index=True)

    metric_date = Column(Date, nullable=False, index=True)
    user_email = Column(Text, nullable=False, index=True)

    tasks_submitted = Column(Integer, nullable=False, default=0)   # SUBMIT actions
    labels_created = Column(Integer, nullable=False, default=0)    # ADD label logs
    review_actions = Column(Integer, nullable=False, default=0)    # APPROVE + REJECT actions

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "encord_project_hash", "metric_date", "user_email",
            name="uq_encord_activity_day_user",
        ),
    )
