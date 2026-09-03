"""merge heads

Revision ID: ce26ed0bd222
Revises: 2db68da99901, b539269dd6af, eebc29bcfbc9
Create Date: 2026-09-03 17:25:08.405881

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce26ed0bd222'
down_revision: Union[str, Sequence[str], None] = ('2db68da99901', 'b539269dd6af', 'eebc29bcfbc9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
