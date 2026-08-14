"""Add employee_badges and employee_badge_logs tables

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-12 19:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_badges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("badge_code", sa.String(length=40), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("awarded_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("awarded_by", sa.Integer(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["awarded_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "employee_id",
            "badge_code",
            "period_start",
            "period_end",
            name="uq_employee_badge_period",
        ),
    )
    op.create_index("ix_employee_badges_id", "employee_badges", ["id"])
    op.create_index("ix_employee_badges_employee_id", "employee_badges", ["employee_id"])
    op.create_index("ix_employee_badges_badge_code", "employee_badges", ["badge_code"])
    op.create_index("ix_employee_badges_expires_at", "employee_badges", ["expires_at"])
    op.create_index("ix_employee_badges_status", "employee_badges", ["status"])
    op.create_index(
        "ix_employee_badges_employee_status",
        "employee_badges",
        ["employee_id", "status"],
    )

    op.create_table(
        "employee_badge_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_badge_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("badge_code", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["employee_badge_id"], ["employee_badges.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_employee_badge_logs_id", "employee_badge_logs", ["id"])
    op.create_index("ix_employee_badge_logs_employee_id", "employee_badge_logs", ["employee_id"])
    op.create_index("ix_employee_badge_logs_badge_code", "employee_badge_logs", ["badge_code"])
    op.create_index(
        "ix_employee_badge_logs_employee_badge_id",
        "employee_badge_logs",
        ["employee_badge_id"],
    )


def downgrade() -> None:
    op.drop_table("employee_badge_logs")
    op.drop_table("employee_badges")
