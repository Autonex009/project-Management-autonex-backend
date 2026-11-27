"""drop assignment_reminder_logs table

Revision ID: l5m6n7o8p9q0
Revises: k4l5m6n7o8p9
Create Date: 2026-11-27 15:05:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "l5m6n7o8p9q0"
down_revision: Union[str, Sequence[str], None] = "k4l5m6n7o8p9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    if is_offline:
        return

    insp = sa.inspect(conn)
    existing_tables = set(insp.get_table_names())

    if "assignment_reminder_logs" in existing_tables:
        op.drop_table("assignment_reminder_logs")


def downgrade() -> None:
    op.create_table(
        "assignment_reminder_logs",
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reminder_date", sa.Date(), primary_key=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
