from datetime import timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Numeric,
    Boolean,
    TIMESTAMP,
    JSON,
    BigInteger,
    ForeignKey,
    UniqueConstraint,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.db.database import Base


JSONB_TYPE = JSON().with_variant(JSONB(), "postgresql")


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Assessment(Base):
    """
    Reusable Assessment Blueprint Studio.
    Defines questions, pooled categories, timing, and proctoring rules.
    """
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # test_type: 'mcq' or 'typing' (coding and subjective deferred until graders exist)
    test_type = Column(String(32), nullable=False, default="mcq", server_default="mcq")
    status = Column(String(32), nullable=False, default="draft", server_default="draft")  # draft, published, archived
    version = Column(Integer, nullable=False, default=1, server_default="1")
    duration_minutes = Column(Integer, nullable=False, default=20, server_default="20")

    # passing_score is required for MCQ at publish time, nullable for typing (which uses WPM/accuracy in test_config)
    passing_score = Column(Numeric(5, 2, asdecimal=False), nullable=True)
    max_attempts = Column(Integer, nullable=False, default=1, server_default="1")

    test_config = Column(JSONB_TYPE, nullable=False, default=dict, server_default=text("'{}'"))
    proctoring_config = Column(
        JSONB_TYPE,
        nullable=False,
        default=lambda: {"require_camera": True, "max_tab_switches": 3},
        server_default=text("'{\"require_camera\": true, \"max_tab_switches\": 3}'"),
    )

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    assignments = relationship("Assignment", back_populates="assessment")
    enrollments = relationship("AssessmentEnrollment", back_populates="assessment")

    __table_args__ = (
        CheckConstraint("test_type IN ('mcq', 'typing')", name="chk_assessments_test_type"),
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="chk_assessments_status"),
    )


class AssessmentEnrollment(Base):
    """
    Candidate Assessment Enrollment (one per employee per test).
    Decoupled from sitting attempts; tracks individual deadline overrides and status.
    Counts (attempts_used) and scores (best_score) are computed directly from attempts.
    """
    __tablename__ = "assessment_enrollments"

    id = Column(Integer, primary_key=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="RESTRICT"), nullable=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id", ondelete="RESTRICT"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False)

    status = Column(String(32), nullable=False, default="assigned", server_default="assigned")  # assigned, in_progress, passed, failed, cancelled
    deadline = Column(TIMESTAMP(timezone=True), nullable=True)  # Tier 2 override (NULL = inherits assignments.deadline)

    extra_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    # Preserves candidate's original attempt allowance if blueprint max_attempts is lowered later
    max_attempts_snapshot = Column(Integer, nullable=True)
    last_reminded_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships (no ORM delete cascade so ON DELETE RESTRICT protects audit history)
    assignment = relationship("Assignment", back_populates="enrollments")
    assessment = relationship("Assessment", back_populates="enrollments")
    employee = relationship("Employee")
    attempts = relationship(
        "AssessmentAttempt",
        back_populates="enrollment",
        order_by="AssessmentAttempt.attempt_number",
    )

    @property
    def attempts_used(self) -> int:
        if self.attempts is not None:
            return len(self.attempts)
        return 0

    @property
    def best_score(self) -> float | None:
        if self.attempts:
            scores = [a.score for a in self.attempts if a.score is not None]
            return float(max(scores)) if scores else None
        return None

    __table_args__ = (
        UniqueConstraint("employee_id", "assessment_id", name="uq_assessment_enrollment"),
        CheckConstraint(
            "status IN ('assigned', 'in_progress', 'passed', 'failed', 'cancelled')",
            name="chk_enrollment_status",
        ),
        Index("ix_assessment_enrollments_assignment", "assignment_id"),
        Index("ix_assessment_enrollments_scorecard", "assessment_id", "status"),
        Index("ix_assessment_enrollments_updated_id", "updated_at", "id"),
    )


class AssessmentAttempt(Base):
    """
    Individual Test Sitting.
    Holds immutable question paper snapshot, monotonic draft autosave answers,
    and server-evaluated grades.
    """
    __tablename__ = "assessment_attempts"

    id = Column(Integer, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("assessment_enrollments.id", ondelete="RESTRICT"), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    assessment_version = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="in_progress", server_default="in_progress")  # in_progress, completed, abandoned

    # Immutable question paper snapshot frozen at sitting start time
    question_snapshot = Column(JSONB_TYPE, nullable=False)

    started_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    submitted_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Single answers JSON payload guarded by monotonic sequence counter
    answers = Column(JSONB_TYPE, nullable=False, default=dict, server_default=text("'{}'"))
    answers_seq = Column(Integer, nullable=False, default=0, server_default="0")

    score = Column(Numeric(5, 2, asdecimal=False), nullable=True)
    passed = Column(Boolean, nullable=True)

    camera_verified = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_flagged = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    flag_reason = Column(Text, nullable=True)

    # result_summary stores purely diagnostic output (breakdowns, wpm, etc.), never duplicate score/passed
    result_summary = Column(JSONB_TYPE, nullable=True, default=dict, server_default=text("'{}'"))

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships (no ORM delete cascade so ON DELETE RESTRICT protects audit history)
    enrollment = relationship("AssessmentEnrollment", back_populates="attempts")
    logs = relationship(
        "AssessmentLog",
        back_populates="attempt",
        order_by="AssessmentLog.event_time",
    )
    recording = relationship("AssessmentRecording", back_populates="attempt", uselist=False)

    @property
    def time_spent_seconds(self) -> int:
        if self.submitted_at and self.started_at:
            delta = (_utc(self.submitted_at) - _utc(self.started_at)).total_seconds()
            return max(0, int(delta))
        return 0

    @property
    def tab_switch_count(self) -> int:
        if self.logs:
            return sum(1 for l in self.logs if l.event_type == "tab_switch")
        return 0

    @property
    def window_blur_count(self) -> int:
        if self.logs:
            return sum(1 for l in self.logs if l.event_type == "window_blur")
        return 0

    @property
    def camera_lost_count(self) -> int:
        if self.logs:
            return sum(1 for l in self.logs if l.event_type == "camera_lost")
        return 0

    __table_args__ = (
        UniqueConstraint("enrollment_id", "attempt_number", name="uq_attempt_number"),
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'abandoned')",
            name="chk_attempt_status",
        ),
        Index(
            "uq_one_live_attempt",
            "enrollment_id",
            unique=True,
            postgresql_where=(status == "in_progress"),
            sqlite_where=(status == "in_progress"),
        ),
        Index("ix_assessment_attempts_enrollment_status", "enrollment_id", "status"),
        Index(
            "ix_assessment_attempts_flagged",
            "enrollment_id",
            postgresql_where=(is_flagged == True),
            sqlite_where=(is_flagged == True),
        ),
        Index(
            "ix_assessment_attempts_inflight_expiry",
            "expires_at",
            postgresql_where=(status == "in_progress"),
            sqlite_where=(status == "in_progress"),
        ),
    )


class AssessmentLog(Base):
    """
    Real-time security and proctoring audit log.
    Tab switches, window blurs, and camera losses are counted directly from here.
    """
    __tablename__ = "assessment_logs"

    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey("assessment_attempts.id", ondelete="RESTRICT"), nullable=False)
    event_type = Column(String(32), nullable=False)
    event_time = Column(TIMESTAMP(timezone=True), server_default=func.now())
    details = Column(JSONB_TYPE, nullable=True, default=dict, server_default=text("'{}'"))

    # Relationships
    attempt = relationship("AssessmentAttempt", back_populates="logs")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('tab_switch', 'window_blur', 'fullscreen_exit', 'camera_lost', 'multiple_faces', 'no_face')",
            name="chk_log_event_type",
        ),
        Index("ix_assessment_logs_attempt_time", "attempt_id", "event_time"),
    )


class AssessmentRecording(Base):
    """
    Proctoring video recording metadata.
    Videos are stored directly in S3/Supabase storage with 7-day TTL retention.
    """
    __tablename__ = "assessment_recordings"

    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey("assessment_attempts.id", ondelete="RESTRICT"), nullable=False)
    video_storage_path = Column(Text, nullable=False)
    file_size_bytes = Column(BigInteger, nullable=True)

    status = Column(String(32), nullable=False, default="pending_upload", server_default="pending_upload")  # pending_upload, ready, purged, failed

    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    purged_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    attempt = relationship("AssessmentAttempt", back_populates="recording")

    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_assessment_recording_attempt"),
        CheckConstraint(
            "status IN ('pending_upload', 'ready', 'purged', 'failed')",
            name="chk_recording_status",
        ),
        Index(
            "ix_assessment_recordings_expired_purge",
            "expires_at",
            "id",
            postgresql_where=(status == "ready"),
            sqlite_where=(status == "ready"),
        ),
    )
