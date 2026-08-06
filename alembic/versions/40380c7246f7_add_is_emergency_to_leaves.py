"""add_is_emergency_to_leaves

Revision ID: 40380c7246f7
Revises: d4e5f6a7b8c9
Create Date: 2026-08-05 17:26:07.417439

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '40380c7246f7'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspect_obj = sa.inspect(conn)
    columns = [c['name'] for c in inspect_obj.get_columns('leaves')]
    if 'is_emergency' not in columns:
        op.add_column('leaves', sa.Column('is_emergency', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    conn = op.get_bind()
    inspect_obj = sa.inspect(conn)
    columns = [c['name'] for c in inspect_obj.get_columns('leaves')]
    if 'is_emergency' in columns:
        op.drop_column('leaves', 'is_emergency')
