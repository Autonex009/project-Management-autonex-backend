"""add token_version to users and indexes on refresh_tokens

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-12 17:30:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    # 1. Add token_version column to users table (default = 1)
    user_columns = [col["name"] for col in insp.get_columns("users")]
    if "token_version" not in user_columns:
        op.add_column(
            "users",
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="1")
        )

    # 2. Add performance index on refresh_tokens(expires_at)
    rt_indexes = [idx["name"] for idx in insp.get_indexes("refresh_tokens")]
    if "idx_refresh_tokens_expires_at" not in rt_indexes:
        op.create_index(
            "idx_refresh_tokens_expires_at",
            "refresh_tokens",
            ["expires_at"],
            unique=False
        )

    # 3. Add composite index on refresh_tokens(user_id, expires_at)
    if "idx_refresh_tokens_user_expires" not in rt_indexes:
        op.create_index(
            "idx_refresh_tokens_user_expires",
            "refresh_tokens",
            ["user_id", "expires_at"],
            unique=False
        )

    # 4. Safe table sanitization: Purge accumulated dead/expired refresh tokens
    # Note: Active sessions (expires_at >= NOW()) are preserved 100%
    op.execute(
        sa.text("DELETE FROM refresh_tokens WHERE expires_at < NOW()")
    )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    # 1. Drop indexes from refresh_tokens
    rt_indexes = [idx["name"] for idx in insp.get_indexes("refresh_tokens")]
    if "idx_refresh_tokens_user_expires" in rt_indexes:
        op.drop_index("idx_refresh_tokens_user_expires", table_name="refresh_tokens")
    if "idx_refresh_tokens_expires_at" in rt_indexes:
        op.drop_index("idx_refresh_tokens_expires_at", table_name="refresh_tokens")

    # 2. Drop token_version column from users
    user_columns = [col["name"] for col in insp.get_columns("users")]
    if "token_version" in user_columns:
        op.drop_column("users", "token_version")
