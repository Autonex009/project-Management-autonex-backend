"""Add onboarding pipeline

Revision ID: eebc29bcfbc9
Revises: 5b6be00b91f8
Create Date: 2026-08-31 11:46:49.127253

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eebc29bcfbc9'
down_revision: Union[str, Sequence[str], None] = '5b6be00b91f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('onboarding_pipeline',
        sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column('candidate_id', sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
        sa.Column('buddy_id', sa.INTEGER(), autoincrement=False, nullable=True),
        sa.Column('status', sa.VARCHAR(length=30), autoincrement=False, nullable=False),
        sa.Column('started_at', sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column('evaluated_at', sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column('eval_score', sa.INTEGER(), autoincrement=False, nullable=True),
        sa.Column('eval_notes', sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), autoincrement=False, nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), autoincrement=False, nullable=True),
        sa.ForeignKeyConstraint(['buddy_id'], ['employees.id'], name=op.f('onboarding_pipeline_buddy_id_fkey'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['candidate_id'], ['users.id'], name=op.f('onboarding_pipeline_candidate_id_fkey'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['main_projects.id'], name=op.f('onboarding_pipeline_project_id_fkey'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('onboarding_pipeline_pkey'))
    )
    op.create_index(op.f('ix_onboarding_pipeline_id'), 'onboarding_pipeline', ['id'], unique=False)
    op.create_index(op.f('ix_onboarding_pipeline_candidate_id'), 'onboarding_pipeline', ['candidate_id'], unique=False)
    op.create_index(op.f('idx_pipeline_status'), 'onboarding_pipeline', ['status'], unique=False)
    op.create_index(op.f('idx_pipeline_candidate_status'), 'onboarding_pipeline', ['candidate_id', 'status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('idx_pipeline_candidate_status'), table_name='onboarding_pipeline')
    op.drop_index(op.f('idx_pipeline_status'), table_name='onboarding_pipeline')
    op.drop_index(op.f('ix_onboarding_pipeline_candidate_id'), table_name='onboarding_pipeline')
    op.drop_index(op.f('ix_onboarding_pipeline_id'), table_name='onboarding_pipeline')
    op.drop_table('onboarding_pipeline')
