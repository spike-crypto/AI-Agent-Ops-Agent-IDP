"""Integration keys: read-only credentials for outside systems such as SAP Integration Suite.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_integration_keys_key_hash", "integration_keys", ["key_hash"], unique=True)
    op.create_index("ix_integration_keys_user_id", "integration_keys", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_integration_keys_user_id", table_name="integration_keys")
    op.drop_index("ix_integration_keys_key_hash", table_name="integration_keys")
    op.drop_table("integration_keys")
