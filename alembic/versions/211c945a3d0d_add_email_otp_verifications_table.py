"""Add email_otp_verifications table

Revision ID: 211c945a3d0d
Revises: 40380c7246f7
Create Date: 2026-08-10 18:35:22.371627

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '211c945a3d0d'
down_revision: Union[str, Sequence[str], None] = '40380c7246f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'email_otp_verifications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('new_email', sa.Text(), nullable=False),
        sa.Column('encrypted_otp', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('email_otp_verifications')
