"""add device_type to daily_checkins

Revision ID: c2d3e4f5a6b7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-16 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('daily_checkins', sa.Column('device_type', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('daily_checkins', 'device_type')
