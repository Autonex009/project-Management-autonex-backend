"""add last_reminded_at on assessment_enrollments for durable reminder cooldown

Revision ID: j3k4l5m6n7o8
Revises: i2j3k4l5m6n7
Create Date: 2026-11-26 16:40:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "j3k4l5m6n7o8"
down_revision: Union[str, Sequence[str], None] = "i2j3k4l5m6n7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    if not is_offline:
        inspector = sa.inspect(conn)
        if "assessment_enrollments" not in inspector.get_table_names():
            return
        cols = {c["name"] for c in inspector.get_columns("assessment_enrollments")}
        if "last_reminded_at" in cols:
            return

    op.add_column(
        "assessment_enrollments",
        sa.Column("last_reminded_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    if not is_offline:
        inspector = sa.inspect(conn)
        if "assessment_enrollments" not in inspector.get_table_names():
            return
        cols = {c["name"] for c in inspector.get_columns("assessment_enrollments")}
        if "last_reminded_at" not in cols:
            return

    op.drop_column("assessment_enrollments", "last_reminded_at")
