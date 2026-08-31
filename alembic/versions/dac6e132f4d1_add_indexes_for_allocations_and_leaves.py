"""add indexes for allocations and leaves

Revision ID: dac6e132f4d1
Revises: 5b6be00b91f8
Create Date: 2026-08-27 13:40:57.301711

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'dac6e132f4d1'
down_revision: Union[str, Sequence[str], None] = '5b6be00b91f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_allocations_sub_project_active', 'allocations', ['sub_project_id', 'is_active'], unique=False)
    op.create_index('ix_leaves_emp_end_date', 'leaves', ['employee_id', 'end_date'], unique=False)

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_leaves_emp_end_date', table_name='leaves')
    op.drop_index('ix_allocations_sub_project_active', table_name='allocations')
