"""add is_special_case to leaves

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-10-22 13:25:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE leaves ADD COLUMN IF NOT EXISTS is_special_case BOOLEAN DEFAULT FALSE;")


def downgrade() -> None:
    op.execute("ALTER TABLE leaves DROP COLUMN IF EXISTS is_special_case;")
