"""add_allocation_is_active_soft_hide

Revision ID: 50a9500f3fc9
Revises: 211c945a3d0d
Create Date: 2026-08-12 16:14:36.813064

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50a9500f3fc9'
down_revision: Union[str, Sequence[str], None] = '211c945a3d0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('allocations', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('allocations', sa.Column('deactivated_at', sa.TIMESTAMP(), nullable=True))
    op.add_column('allocations', sa.Column('deactivated_reason', sa.String(length=50), nullable=True))
    op.create_index(op.f('ix_allocations_is_active'), 'allocations', ['is_active'], unique=False)
    op.create_index('ix_allocations_sub_project_is_active', 'allocations', ['sub_project_id', 'is_active'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_allocations_sub_project_is_active', table_name='allocations')
    op.drop_index(op.f('ix_allocations_is_active'), table_name='allocations')
    op.drop_column('allocations', 'deactivated_reason')
    op.drop_column('allocations', 'deactivated_at')
    op.drop_column('allocations', 'is_active')
