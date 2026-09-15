"""add_wfo_fields_to_daily_checkin

Revision ID: 699119f340dc
Revises: 4383c2e2adb8
Create Date: 2026-09-09 16:22:36.197749

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '699119f340dc'
down_revision: Union[str, Sequence[str], None] = '4383c2e2adb8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('daily_checkins', sa.Column('office_floor', sa.Text(), nullable=True))
    op.add_column('daily_checkins', sa.Column('lunch_preference', sa.Text(), nullable=True))
    op.add_column('daily_checkins', sa.Column('tiffin_type', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('daily_checkins', 'tiffin_type')
    op.drop_column('daily_checkins', 'lunch_preference')
    op.drop_column('daily_checkins', 'office_floor')
