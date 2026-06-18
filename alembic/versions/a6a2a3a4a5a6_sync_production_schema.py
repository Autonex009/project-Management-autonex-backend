"""sync production schema

Revision ID: a6a2a3a4a5a6
Revises: bb5fa3b8221c
Create Date: 2026-06-18 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6a2a3a4a5a6'
down_revision: Union[str, Sequence[str], None] = 'bb5fa3b8221c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # 1. Add missing columns to 'employees' table
    existing_employee_columns = [col['name'] for col in inspector.get_columns('employees')]
    
    if 'base_salary_enc' not in existing_employee_columns:
        op.add_column('employees', sa.Column('base_salary_enc', sa.Text(), nullable=True))
    if 'previous_employee_type' not in existing_employee_columns:
        op.add_column('employees', sa.Column('previous_employee_type', sa.Text(), nullable=True))
    if 'converted_to_fulltime_at' not in existing_employee_columns:
        op.add_column('employees', sa.Column('converted_to_fulltime_at', sa.TIMESTAMP(), nullable=True))
    if 'converted_by' not in existing_employee_columns:
        op.add_column('employees', sa.Column('converted_by', sa.Integer(), nullable=True))

    # 2. Add missing columns to 'leaves' table
    existing_leaves_columns = [col['name'] for col in inspector.get_columns('leaves')]
    
    if 'reason' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('reason', sa.Text(), nullable=True))
    if 'status' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('status', sa.String(length=50), server_default='pending', nullable=True))
    if 'approved_by' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('approved_by', sa.Integer(), nullable=True))
    if 'razorpay_applied' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('razorpay_applied', sa.Boolean(), server_default='false', nullable=True))
    if 'flagged' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('flagged', sa.Boolean(), server_default='false', nullable=False))
    if 'approval_remark' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('approval_remark', sa.Text(), nullable=True))
    if 'created_at' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True))
    if 'updated_at' not in existing_leaves_columns:
        op.add_column('leaves', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True))


def downgrade() -> None:
    op.drop_column('leaves', 'updated_at')
    op.drop_column('leaves', 'created_at')
    op.drop_column('leaves', 'approval_remark')
    op.drop_column('leaves', 'flagged')
    op.drop_column('leaves', 'razorpay_applied')
    op.drop_column('leaves', 'approved_by')
    op.drop_column('leaves', 'status')
    op.drop_column('leaves', 'reason')

    op.drop_column('employees', 'converted_by')
    op.drop_column('employees', 'converted_to_fulltime_at')
    op.drop_column('employees', 'previous_employee_type')
    op.drop_column('employees', 'base_salary_enc')
