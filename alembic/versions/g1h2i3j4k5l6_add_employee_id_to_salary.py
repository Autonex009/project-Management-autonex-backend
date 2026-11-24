"""add employee_id to salary table

Revision ID: g1h2i3j4k5l6
Revises: f3a4b5c6d7e8
Create Date: 2026-11-24 13:20:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    # 1. Add employee_id column to salary table if not present
    salary_columns = [col["name"] for col in insp.get_columns("salary")]
    if "employee_id" not in salary_columns:
        op.add_column(
            "salary",
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            op.f("ix_salary_employee_id"), "salary", ["employee_id"], unique=False
        )

    # 2. Backfill employee_id by matching normalized names
    try:
        op.execute(
            """
            UPDATE salary s
            SET employee_id = e.id
            FROM employees e
            WHERE lower(regexp_replace(trim(s.full_name), '\\s+', ' ', 'g')) = 
                  lower(regexp_replace(trim(e.name), '\\s+', ' ', 'g'))
              AND s.employee_id IS NULL;
            """
        )
    except Exception:
        pass


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    salary_columns = [col["name"] for col in insp.get_columns("salary")]
    if "employee_id" in salary_columns:
        op.drop_index(op.f("ix_salary_employee_id"), table_name="salary")
        op.drop_column("salary", "employee_id")
