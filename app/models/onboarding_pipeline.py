"""
SQLAlchemy model for the Onboarding Pipeline tracker.

Tracks candidates through:
  pending_confirmation → in_progress → day_5_pending → passed / failed
"""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, Index
from sqlalchemy.sql import func

from app.db.database import Base


class OnboardingPipeline(Base):
    __tablename__ = "onboarding_pipeline"

    id = Column(Integer, primary_key=True, index=True)

    # The new hire being onboarded
    candidate_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Which project they train on (legacy field)
    project_id = Column(
        Integer,
        ForeignKey("main_projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Which specific sheet/sub-team they train on
    sub_project_id = Column(
        Integer,
        ForeignKey("daily_sheets.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The assigned mentor / buddy (points to employees table)
    buddy_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )

    # State machine:
    #   pending_confirmation  – PM assigned, waiting for candidate to accept
    #   in_progress           – candidate accepted, 5-day clock running
    #   day_5_pending         – 5 days elapsed, awaiting evaluation
    #   passed                – buddy evaluated positively, allocation created
    #   failed                – buddy evaluated negatively, candidate notified
    status = Column(String(30), nullable=False, default="pending_confirmation")

    # Set when the candidate clicks "Accept & Start Onboarding"
    started_at = Column(TIMESTAMP, nullable=True)

    # Set when the buddy/PM submits the Day-5 evaluation
    evaluated_at = Column(TIMESTAMP, nullable=True)
    eval_score = Column(Integer, nullable=True)          # 0-100
    eval_notes = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Composite index for the dashboard query (filter by status, sort by date)
    __table_args__ = (
        Index("idx_pipeline_status", "status"),
        Index("idx_pipeline_candidate_status", "candidate_id", "status"),
    )
