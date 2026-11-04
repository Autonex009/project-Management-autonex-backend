"""add weekly_compensation_records table

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-11-03 15:30:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing_tables = insp.get_table_names()

    if "weekly_compensation_records" not in existing_tables:
        op.create_table(
            "weekly_compensation_records",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("employee_id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("pm_id", sa.Integer(), nullable=True),
            sa.Column("daily_checkin_id", sa.Integer(), nullable=True),
            sa.Column("work_date", sa.Date(), nullable=False),
            sa.Column("payroll_month", sa.String(length=7), nullable=False),
            sa.Column("week_number", sa.Integer(), nullable=False),
            sa.Column("is_weekend", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("portal_duration_seconds", sa.Integer(), nullable=True),
            sa.Column("encord_duration_seconds", sa.Integer(), server_default="0", nullable=False),
            sa.Column("final_duration_seconds", sa.Integer(), nullable=False),
            sa.Column("is_final_time_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("hourly_rate", sa.Numeric(precision=10, scale=2), nullable=False),
            sa.Column("calculated_payout", sa.Numeric(precision=10, scale=2), nullable=False),
            sa.Column("override_payout", sa.Numeric(precision=10, scale=2), nullable=True),
            sa.Column("edit_note", sa.Text(), nullable=True),
            sa.Column("last_edited_by", sa.Integer(), nullable=True),
            sa.Column("last_edited_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
            sa.Column("approved_by", sa.Integer(), nullable=True),
            sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("is_locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("payroll_run_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["daily_sheets.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["pm_id"], ["employees.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["last_edited_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["payroll_run_id"], ["payroll_runs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("employee_id", "work_date", name="uq_weekly_comp_emp_date"),
        )

    # Check and add indexes if missing
    existing_indexes = {idx["name"] for idx in insp.get_indexes("weekly_compensation_records")}
    if "ix_weekly_compensation_records_id" not in existing_indexes:
        op.create_index("ix_weekly_compensation_records_id", "weekly_compensation_records", ["id"])
    if "idx_weekly_comp_month_status" not in existing_indexes:
        op.create_index("idx_weekly_comp_month_status", "weekly_compensation_records", ["payroll_month", "status"])
    if "idx_weekly_comp_emp_month" not in existing_indexes:
        op.create_index("idx_weekly_comp_emp_month", "weekly_compensation_records", ["employee_id", "payroll_month"])
    if "idx_weekly_comp_work_date" not in existing_indexes:
        op.create_index("idx_weekly_comp_work_date", "weekly_compensation_records", ["work_date"])
    if "idx_weekly_comp_daily_checkin" not in existing_indexes:
        op.create_index("idx_weekly_comp_daily_checkin", "weekly_compensation_records", ["daily_checkin_id"])


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "weekly_compensation_records" in insp.get_table_names():
        existing_indexes = {idx["name"] for idx in insp.get_indexes("weekly_compensation_records")}
        if "idx_weekly_comp_daily_checkin" in existing_indexes:
            op.drop_index("idx_weekly_comp_daily_checkin", table_name="weekly_compensation_records")
        if "idx_weekly_comp_work_date" in existing_indexes:
            op.drop_index("idx_weekly_comp_work_date", table_name="weekly_compensation_records")
        if "idx_weekly_comp_emp_month" in existing_indexes:
            op.drop_index("idx_weekly_comp_emp_month", table_name="weekly_compensation_records")
        if "idx_weekly_comp_month_status" in existing_indexes:
            op.drop_index("idx_weekly_comp_month_status", table_name="weekly_compensation_records")
        if "ix_weekly_compensation_records_id" in existing_indexes:
            op.drop_index("ix_weekly_compensation_records_id", table_name="weekly_compensation_records")
        op.drop_table("weekly_compensation_records")
