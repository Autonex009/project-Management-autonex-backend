"""Add employee_notes table for complaints, warnings, and recognition

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-08-12 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
# Replace with the output of: alembic heads
down_revision: Union[str, Sequence[str], None] = "50a9500f3fc9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    op.create_table(
        "employee_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("issued_by", sa.Integer(), nullable=True),
        sa.Column("issued_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_employee_notes_employee_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"],
            ["users.id"],
            name="fk_employee_notes_issued_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name="fk_employee_notes_resolved_by",
            ondelete="SET NULL",
        ),
    )

    op.create_index("ix_employee_notes_id", "employee_notes", ["id"])
    op.create_index("ix_employee_notes_employee_id", "employee_notes", ["employee_id"])
    op.create_index("ix_employee_notes_type", "employee_notes", ["type"])
    op.create_index(
        "ix_employee_notes_employee_type",
        "employee_notes",
        ["employee_id", "type"],
    )
    op.create_index(
        "ix_employee_notes_type_status",
        "employee_notes",
        ["type", "status"],
    )
    op.create_index("ix_employee_notes_issued_at", "employee_notes", ["issued_at"])



def downgrade() -> None:
    op.drop_index("ix_employee_notes_issued_at", table_name="employee_notes")
    op.drop_index("ix_employee_notes_type_status", table_name="employee_notes")
    op.drop_index("ix_employee_notes_employee_type", table_name="employee_notes")
    op.drop_index("ix_employee_notes_type", table_name="employee_notes")
    op.drop_index("ix_employee_notes_employee_id", table_name="employee_notes")
    op.drop_index("ix_employee_notes_id", table_name="employee_notes")
    op.drop_table("employee_notes")
