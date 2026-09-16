"""add employee_documents table

Revision ID: a1b2c3d4e5f6
Revises: e978b9f284fc
Create Date: 2026-09-08 13:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e978b9f284fc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type first — checkfirst=True prevents error if it already exists
    doc_type_enum = postgresql.ENUM(
        "internship_offer_letter",
        "fulltime_offer_letter",
        "internship_completion_certificate",
        "experience_letter",
        "salary_structure",
        "org_policy",
        name="doc_type_enum",
        create_type=False,
    )
    doc_type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "employee_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "doc_type",
            postgresql.ENUM(
                "internship_offer_letter",
                "fulltime_offer_letter",
                "internship_completion_certificate",
                "experience_letter",
                "salary_structure",
                "org_policy",
                name="doc_type_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("file_url", sa.Text(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(32), nullable=False, server_default="generated"),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("generated_at", sa.TIMESTAMP(), server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now()),
    )
    op.create_index("ix_employee_documents_employee_id", "employee_documents", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_employee_documents_employee_id", table_name="employee_documents")
    op.drop_table("employee_documents")
    # Only drop the enum if nothing else is using it
    op.execute("DROP TYPE IF EXISTS doc_type_enum")
