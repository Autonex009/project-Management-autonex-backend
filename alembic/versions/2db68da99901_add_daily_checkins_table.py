"""Add daily_checkins table

Revision ID: 2db68da99901
Revises: 5b6be00b91f8
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2db68da99901'
down_revision: Union[str, Sequence[str], None] = '5b6be00b91f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'daily_checkins',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('checkin_date', sa.Date(), nullable=False),
        sa.Column('work_mode', sa.Text(), nullable=False),
        sa.Column('project_ids', sa.JSON(), nullable=True),
        sa.Column('mood', sa.Text(), nullable=True),
        sa.Column('checked_in_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('checked_out_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('pm_confirmed_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('pm_confirmed_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('employee_id', 'checkin_date', name='uq_daily_checkin_employee_date'),
    )
    op.create_index(op.f('ix_daily_checkins_id'), 'daily_checkins', ['id'], unique=False)
    op.create_index(op.f('ix_daily_checkins_employee_id'), 'daily_checkins', ['employee_id'], unique=False)
    op.create_index(op.f('ix_daily_checkins_checkin_date'), 'daily_checkins', ['checkin_date'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_daily_checkins_checkin_date'), table_name='daily_checkins')
    op.drop_index(op.f('ix_daily_checkins_employee_id'), table_name='daily_checkins')
    op.drop_index(op.f('ix_daily_checkins_id'), table_name='daily_checkins')
    op.drop_table('daily_checkins')
