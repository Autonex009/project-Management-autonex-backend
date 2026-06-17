"""add_half_day_leave

Revision ID: bb5fa3b8221c
Revises: 79c196e248db
Create Date: 2026-06-16 18:04:23.897376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb5fa3b8221c'
down_revision: Union[str, Sequence[str], None] = '79c196e248db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leaves', sa.Column('is_half_day', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('leaves', sa.Column('half_day_slot', sa.String(), nullable=True))
    with op.batch_alter_table('payroll_leave_adjustments', schema=None) as batch_op:
        batch_op.alter_column('unpaid_days',
                   existing_type=sa.INTEGER(),
                   type_=sa.Float(),
                   existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('payroll_leave_adjustments', schema=None) as batch_op:
        batch_op.alter_column('unpaid_days',
                   existing_type=sa.Float(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)
    op.drop_column('leaves', 'half_day_slot')
    op.drop_column('leaves', 'is_half_day')
    # ### end Alembic commands ###
