"""add clusters, assignments, and assessments tables (normalized phase 1 schema)

Revision ID: h1i2j3k4l5m6
Revises: g1h2i3j4k5l6
Create Date: 2026-11-25 18:45:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "h1i2j3k4l5m6"
down_revision: Union[str, Sequence[str], None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    existing_tables = set() if is_offline else set(sa.inspect(conn).get_table_names())

    f04_drop_order = [
        "assessment_recordings",
        "assessment_logs",
        "assessment_attempts",
        "assessment_enrollments",
        "assignment_reminder_logs",
        "assignments",
        "assessments",
        "cluster_members",
        "clusters",
    ]
    if not is_offline:
        insp = sa.inspect(conn)
        present_f04 = [t for t in f04_drop_order if t in existing_tables]
        if present_f04:
            schema_drift = False
            if "clusters" in existing_tables:
                c_cols = {c["name"] for c in insp.get_columns("clusters")}
                if "joined_on_or_after" not in c_cols or "cluster_type" in c_cols:
                    schema_drift = True
            if "assignments" in existing_tables:
                a_cols = {c["name"] for c in insp.get_columns("assignments")}
                if "assignment_type" in a_cols:
                    schema_drift = True
            if "assessment_enrollments" in existing_tables:
                e_cols = {c["name"] for c in insp.get_columns("assessment_enrollments")}
                if "max_attempts_snapshot" not in e_cols or "attempts_used" in e_cols:
                    schema_drift = True
            if "assessment_attempts" in existing_tables:
                att_cols = {c["name"] for c in insp.get_columns("assessment_attempts")}
                if "answers" not in att_cols or "answers_seq" not in att_cols or "draft_answers" in att_cols:
                    schema_drift = True
            if "assessment_recordings" in existing_tables:
                r_cols = {c["name"] for c in insp.get_columns("assessment_recordings")}
                if "bucket_name" in r_cols:
                    schema_drift = True

            if schema_drift:
                all_empty = all(
                    conn.execute(sa.text(f"SELECT 1 FROM {t} LIMIT 1")).first() is None
                    for t in present_f04
                )
                if all_empty:
                    # Drop stale pre-refactor empty prototype tables so canonical tables/indexes/constraints apply cleanly
                    for t in present_f04:
                        op.drop_table(t)
                    existing_tables = set(sa.inspect(conn).get_table_names())

    # 1. clusters
    if "clusters" not in existing_tables:
        op.create_table(
            "clusters",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("dynamic_scope", sa.String(64), nullable=False, server_default="custom"),
            sa.Column("dynamic_scopes", JSONB_TYPE, nullable=True),
            sa.Column("joined_on_or_after", sa.Date(), nullable=True),
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.CheckConstraint(
                "dynamic_scope IN ('custom', 'everyone', 'full_time', 'annotators_reviewers', 'developers', 'project_managers', 'team_leads', 'new_joiners')",
                name="chk_clusters_dynamic_scope",
            ),
            sa.CheckConstraint(
                "dynamic_scope <> 'new_joiners' OR joined_on_or_after IS NOT NULL",
                name="chk_new_joiners_date",
            ),
        )
        op.create_index(
            "uq_clusters_name_active",
            "clusters",
            ["name"],
            unique=True,
            postgresql_where=sa.text("is_archived = false"),
            sqlite_where=sa.text("is_archived = 0"),
        )
        op.create_index(
            "ix_clusters_dynamic_active",
            "clusters",
            ["dynamic_scope"],
            postgresql_where=sa.text("dynamic_scope <> 'custom' AND is_archived = false"),
            sqlite_where=sa.text("dynamic_scope <> 'custom' AND is_archived = 0"),
        )
    else:
        existing_cluster_cols = {c["name"] for c in sa.inspect(conn).get_columns("clusters")}
        if "dynamic_scopes" not in existing_cluster_cols:
            op.add_column("clusters", sa.Column("dynamic_scopes", JSONB_TYPE, nullable=True))

    # 2. cluster_members
    if "cluster_members" not in existing_tables:
        op.create_table(
            "cluster_members",
            sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True),
            sa.Column("added_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )

    # 3. assessments
    if "assessments" not in existing_tables:
        op.create_table(
            "assessments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("test_type", sa.String(32), nullable=False, server_default="mcq"),
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("passing_score", sa.Numeric(5, 2, asdecimal=False), nullable=True),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("test_config", JSONB_TYPE, nullable=False, server_default=sa.text("'{}'")),
            sa.Column("proctoring_config", JSONB_TYPE, nullable=False, server_default=sa.text("'{\"require_camera\": true, \"max_tab_switches\": 3}'")),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.CheckConstraint("test_type IN ('mcq', 'typing')", name="chk_assessments_test_type"),
            sa.CheckConstraint("status IN ('draft', 'published', 'archived')", name="chk_assessments_status"),
        )

    # 4. assignments (Phase 1: assessments only)
    if "assignments" not in existing_tables:
        op.create_table(
            "assignments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("assessment_id", sa.Integer(), sa.ForeignKey("assessments.id", ondelete="RESTRICT"), nullable=False, index=True),
            sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("relative_due_days", sa.Integer(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.CheckConstraint("assessment_id IS NOT NULL", name="chk_assignment_target"),
            sa.CheckConstraint(
                "((deadline IS NOT NULL AND relative_due_days IS NULL) OR "
                "(deadline IS NULL AND relative_due_days IS NOT NULL AND relative_due_days > 0))",
                name="chk_assignment_deadline",
            ),
        )
        op.create_index(
            "uq_cluster_assessment_assignment",
            "assignments",
            ["cluster_id", "assessment_id"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
            sqlite_where=sa.text("is_active = 1"),
        )
        op.create_index("ix_assignments_cluster", "assignments", ["cluster_id", "is_active"])

    # 5. assignment_reminder_logs
    if "assignment_reminder_logs" not in existing_tables:
        op.create_table(
            "assignment_reminder_logs",
            sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("reminder_date", sa.Date(), primary_key=True),
            sa.Column("sent_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )

    # 6. assessment_enrollments
    if "assessment_enrollments" not in existing_tables:
        op.create_table(
            "assessment_enrollments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("assessment_id", sa.Integer(), sa.ForeignKey("assessments.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="assigned"),
            sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("extra_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts_snapshot", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("employee_id", "assessment_id", name="uq_assessment_enrollment"),
            sa.CheckConstraint(
                "status IN ('assigned', 'in_progress', 'passed', 'failed', 'cancelled')",
                name="chk_enrollment_status",
            ),
        )
        op.create_index("ix_assessment_enrollments_assignment", "assessment_enrollments", ["assignment_id"])
        op.create_index("ix_assessment_enrollments_scorecard", "assessment_enrollments", ["assessment_id", "status"])

    # 7. assessment_attempts
    if "assessment_attempts" not in existing_tables:
        op.create_table(
            "assessment_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("assessment_enrollments.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("assessment_version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="in_progress"),
            sa.Column("question_snapshot", JSONB_TYPE, nullable=False),
            sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("answers", JSONB_TYPE, nullable=False, server_default=sa.text("'{}'")),
            sa.Column("answers_seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("score", sa.Numeric(5, 2, asdecimal=False), nullable=True),
            sa.Column("passed", sa.Boolean(), nullable=True),
            sa.Column("camera_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("flag_reason", sa.Text(), nullable=True),
            sa.Column("result_summary", JSONB_TYPE, nullable=True, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("enrollment_id", "attempt_number", name="uq_attempt_number"),
            sa.CheckConstraint(
                "status IN ('in_progress', 'completed', 'abandoned')",
                name="chk_attempt_status",
            ),
        )
        op.create_index(
            "uq_one_live_attempt",
            "assessment_attempts",
            ["enrollment_id"],
            unique=True,
            postgresql_where=sa.text("status = 'in_progress'"),
            sqlite_where=sa.text("status = 'in_progress'"),
        )
        op.create_index("ix_assessment_attempts_enrollment_status", "assessment_attempts", ["enrollment_id", "status"])
        op.create_index(
            "ix_assessment_attempts_flagged",
            "assessment_attempts",
            ["enrollment_id"],
            postgresql_where=sa.text("is_flagged = true"),
            sqlite_where=sa.text("is_flagged = 1"),
        )
        op.create_index(
            "ix_assessment_attempts_inflight_expiry",
            "assessment_attempts",
            ["expires_at"],
            postgresql_where=sa.text("status = 'in_progress'"),
            sqlite_where=sa.text("status = 'in_progress'"),
        )
    else:
        existing_att_indexes = {idx["name"] for idx in sa.inspect(conn).get_indexes("assessment_attempts")}
        if "ix_assessment_attempts_flagged" not in existing_att_indexes:
            op.create_index(
                "ix_assessment_attempts_flagged",
                "assessment_attempts",
                ["enrollment_id"],
                postgresql_where=sa.text("is_flagged = true"),
                sqlite_where=sa.text("is_flagged = 1"),
            )

    # 8. assessment_logs
    if "assessment_logs" not in existing_tables:
        op.create_table(
            "assessment_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("attempt_id", sa.Integer(), sa.ForeignKey("assessment_attempts.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("event_time", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("details", JSONB_TYPE, nullable=True, server_default=sa.text("'{}'")),
            sa.CheckConstraint(
                "event_type IN ('tab_switch', 'window_blur', 'fullscreen_exit', 'camera_lost', 'multiple_faces', 'no_face')",
                name="chk_log_event_type",
            ),
        )
        op.create_index("ix_assessment_logs_attempt_time", "assessment_logs", ["attempt_id", "event_time"])

    # 9. assessment_recordings
    if "assessment_recordings" not in existing_tables:
        op.create_table(
            "assessment_recordings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("attempt_id", sa.Integer(), sa.ForeignKey("assessment_attempts.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("video_storage_path", sa.Text(), nullable=False),
            sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending_upload"),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("attempt_id", name="uq_assessment_recording_attempt"),
            sa.CheckConstraint(
                "status IN ('pending_upload', 'ready', 'purged', 'failed')",
                name="chk_recording_status",
            ),
        )
        op.create_index(
            "ix_assessment_recordings_expired_purge",
            "assessment_recordings",
            ["expires_at", "id"],
            postgresql_where=sa.text("status = 'ready'"),
            sqlite_where=sa.text("status = 'ready'"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    existing_tables = set() if is_offline else set(sa.inspect(conn).get_table_names())

    for table in [
        "assessment_recordings",
        "assessment_logs",
        "assessment_attempts",
        "assessment_enrollments",
        "assignment_reminder_logs",
        "assignments",
        "assessments",
        "cluster_members",
        "clusters",
    ]:
        if table in existing_tables:
            op.drop_table(table)
