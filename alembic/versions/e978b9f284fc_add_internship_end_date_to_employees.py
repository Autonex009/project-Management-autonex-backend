"""add internship_end_date to employees

Revision ID: e978b9f284fc
Revises: 4383c2e2adb8
Create Date: 2026-09-08 13:01:23.579753
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e978b9f284fc"
down_revision: Union[str, Sequence[str], None] = "4383c2e2adb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("internship_end_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("employees", "internship_end_date")
