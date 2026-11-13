"""Remember which Langfuse trace wrote each draft, so HR's review can be scored against it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drafts") as batch:
        batch.add_column(sa.Column("trace_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("drafts") as batch:
        batch.drop_column("trace_id")
