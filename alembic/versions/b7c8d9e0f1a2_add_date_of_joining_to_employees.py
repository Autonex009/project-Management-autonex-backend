"""add date_of_joining to employees

Revision ID: b7c8d9e0f1a2
Revises: 699119f340dc, a1b2c3d4e5f6
Create Date: 2026-10-21 17:45:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = ("699119f340dc", "a1b2c3d4e5f6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Safe metadata-only addition: NULLABLE date_of_joining column
    op.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS date_of_joining DATE;")
    op.execute("CREATE INDEX IF NOT EXISTS ix_employees_date_of_joining ON employees (date_of_joining);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_employees_date_of_joining;")
    op.execute("ALTER TABLE employees DROP COLUMN IF EXISTS date_of_joining;")
