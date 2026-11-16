"""Configurable Resume Processing Engine: extraction modes and candidate parsed data.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    with op.batch_alter_table("candidates") as batch:
        batch.add_column(
            sa.Column(
                "extraction_method",
                sa.String(length=40),
                server_default=sa.text("'standard'"),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "extraction_status",
                sa.String(length=40),
                server_default=sa.text("'completed'"),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("extracted_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidates") as batch:
        batch.drop_column("extracted_data")
        batch.drop_column("extraction_status")
        batch.drop_column("extraction_method")
    op.drop_table("system_settings")
