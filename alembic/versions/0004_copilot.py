"""HR Copilot: delegated token audit, conversations, messages, action proposals.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_grants",
        sa.Column("jti", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("audience", sa.String(length=80), nullable=False),
        sa.Column("actors", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_token_grants_user_id", "token_grants", ["user_id"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_table(
        "action_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_action_proposals_conversation_id", "action_proposals", ["conversation_id"])
    op.create_index("ix_action_proposals_user_id", "action_proposals", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("action_proposals.id"), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("agent_jobs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    with op.batch_alter_table("agent_jobs") as batch:
        batch.add_column(sa.Column("steps", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("delegation", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("delegation")
    with op.batch_alter_table("agent_jobs") as batch:
        batch.drop_column("steps")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_action_proposals_user_id", table_name="action_proposals")
    op.drop_index("ix_action_proposals_conversation_id", table_name="action_proposals")
    op.drop_table("action_proposals")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_token_grants_user_id", table_name="token_grants")
    op.drop_table("token_grants")
