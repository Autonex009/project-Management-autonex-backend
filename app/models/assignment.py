from sqlalchemy import (
    Column,
    Integer,
    Boolean,
    TIMESTAMP,
    Date,
    ForeignKey,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.db.database import Base


class Assignment(Base):
    """
    Unified Assignment table scheduling modules to cohorts.
    In Phase 1, schedules assessment blueprints to clusters.
    (Phase 2 will introduce training_course_id referencing training_courses).
    """
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True)
    cluster_id = Column(Integer, ForeignKey("clusters.id", ondelete="RESTRICT"), nullable=False)

    # Phase 1: Only assessments are scheduled
    assessment_id = Column(Integer, ForeignKey("assessments.id", ondelete="RESTRICT"), nullable=False, index=True)

    # Tier 1 baseline deadline (fixed) OR rolling onboarding deadline (relative)
    deadline = Column(TIMESTAMP(timezone=True), nullable=True)
    relative_due_days = Column(Integer, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    cluster = relationship("Cluster", back_populates="assignments")
    assessment = relationship("Assessment", back_populates="assignments")
    enrollments = relationship("AssessmentEnrollment", back_populates="assignment")

    __table_args__ = (
        CheckConstraint(
            "assessment_id IS NOT NULL",
            name="chk_assignment_target",
        ),
        CheckConstraint(
            "((deadline IS NOT NULL AND relative_due_days IS NULL) OR "
            "(deadline IS NULL AND relative_due_days IS NOT NULL AND relative_due_days > 0))",
            name="chk_assignment_deadline",
        ),
        Index(
            "uq_cluster_assessment_assignment",
            "cluster_id",
            "assessment_id",
            unique=True,
            postgresql_where=(is_active == True),
            sqlite_where=(is_active == True),
        ),
        Index("ix_assignments_cluster", "cluster_id", "is_active"),
    )
