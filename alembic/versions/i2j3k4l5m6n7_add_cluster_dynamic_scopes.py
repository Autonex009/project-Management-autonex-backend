"""add dynamic_scopes JSON column on clusters for multi-rule cohorts

Revision ID: i2j3k4l5m6n7
Revises: h1i2j3k4l5m6
Create Date: 2026-11-26 15:58:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "i2j3k4l5m6n7"
down_revision: Union[str, Sequence[str], None] = "h1i2j3k4l5m6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    if not is_offline:
        inspector = sa.inspect(conn)
        if "clusters" not in inspector.get_table_names():
            return
        cols = {c["name"] for c in inspector.get_columns("clusters")}
        if "dynamic_scopes" in cols:
            return

    op.add_column("clusters", sa.Column("dynamic_scopes", JSONB_TYPE, nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    is_offline = conn is None or conn.__class__.__name__ == "MockConnection"
    if not is_offline:
        inspector = sa.inspect(conn)
        if "clusters" not in inspector.get_table_names():
            return
        cols = {c["name"] for c in inspector.get_columns("clusters")}
        if "dynamic_scopes" not in cols:
            return

    op.drop_column("clusters", "dynamic_scopes")
