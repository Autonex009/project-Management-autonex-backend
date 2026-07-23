"""add encord_id to employees

Revision ID: c1d2e3f4a5b6
Revises: a6a2a3a4a5a6
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'a6a2a3a4a5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    existing_columns = [col['name'] for col in inspector.get_columns('employees')]
    if 'encord_id' not in existing_columns:
        op.add_column('employees', sa.Column('encord_id', sa.Text(), nullable=True))

    existing_indexes = [ix['name'] for ix in inspector.get_indexes('employees')]
    if 'ix_employees_encord_id' not in existing_indexes:
        op.create_index('ix_employees_encord_id', 'employees', ['encord_id'])


def downgrade() -> None:
    op.drop_index('ix_employees_encord_id', table_name='employees')
    op.drop_column('employees', 'encord_id')
