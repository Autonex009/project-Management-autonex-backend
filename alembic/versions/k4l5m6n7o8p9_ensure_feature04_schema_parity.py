"""ensure feature 04 schema parity across pre-existing postgres tables

Revision ID: k4l5m6n7o8p9
Revises: j3k4l5m6n7o8
Create Date: 2026-11-26 16:56:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "k4l5m6n7o8p9"
down_revision: Union[str, Sequence[str], None] = "j3k4l5m6n7o8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    if is_offline:
        return

    insp = sa.inspect(conn)
    existing_tables = set(insp.get_table_names())

    if "clusters" in existing_tables:
        c_cols = {c["name"] for c in insp.get_columns("clusters")}
        if "dynamic_scopes" not in c_cols:
            op.add_column("clusters", sa.Column("dynamic_scopes", JSONB_TYPE, nullable=True))
        if "joined_on_or_after" not in c_cols:
            op.add_column("clusters", sa.Column("joined_on_or_after", sa.Date(), nullable=True))

    if "assessment_enrollments" in existing_tables:
        e_cols = {c["name"] for c in insp.get_columns("assessment_enrollments")}
        if "max_attempts_snapshot" not in e_cols:
            op.add_column("assessment_enrollments", sa.Column("max_attempts_snapshot", sa.Integer(), nullable=True))
        if "last_reminded_at" not in e_cols:
            op.add_column("assessment_enrollments", sa.Column("last_reminded_at", sa.TIMESTAMP(timezone=True), nullable=True))

    if "assessment_attempts" in existing_tables:
        att_cols = {c["name"] for c in insp.get_columns("assessment_attempts")}
        if "answers" not in att_cols:
            op.add_column(
                "assessment_attempts",
                sa.Column("answers", JSONB_TYPE, nullable=False, server_default=sa.text("'{}'")),
            )
        if "answers_seq" not in att_cols:
            op.add_column(
                "assessment_attempts",
                sa.Column("answers_seq", sa.Integer(), nullable=False, server_default="0"),
            )
        att_indexes = {idx["name"] for idx in insp.get_indexes("assessment_attempts")}
        if "ix_assessment_attempts_flagged" not in att_indexes:
            op.create_index(
                "ix_assessment_attempts_flagged",
                "assessment_attempts",
                ["enrollment_id"],
                postgresql_where=sa.text("is_flagged = true"),
                sqlite_where=sa.text("is_flagged = 1"),
            )
        if "ix_assessment_attempts_inflight_expiry" not in att_indexes:
            op.create_index(
                "ix_assessment_attempts_inflight_expiry",
                "assessment_attempts",
                ["expires_at"],
                postgresql_where=sa.text("status = 'in_progress'"),
                sqlite_where=sa.text("status = 'in_progress'"),
            )


def downgrade() -> None:
    # No-op: canonical Feature-04 columns and indexes belong to h1i2j3k4l5m6..j3k4l5m6n7o8
    pass
