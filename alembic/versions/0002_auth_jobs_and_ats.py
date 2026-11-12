"""Auth hardening, agent job queue, and the free public ATS.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False))
        batch.add_column(sa.Column("failed_logins", sa.Integer(), server_default=sa.text("0"), nullable=False))
        batch.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("mfa_secret", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("mfa_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
        batch.add_column(sa.Column("mfa_recovery", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch.add_column(sa.Column("oidc_subject", sa.String(length=255), nullable=True))
        batch.create_index("ix_users_oidc_subject", ["oidc_subject"], unique=False)

    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("location", sa.String(length=120), server_default=sa.text("''"), nullable=False))
        batch.add_column(sa.Column("published", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    with op.batch_alter_table("candidates") as batch:
        batch.add_column(sa.Column("source", sa.String(length=20), server_default=sa.text("'manual'"), nullable=False))

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("replaced_by", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.create_index("ix_refresh_tokens_token_hash", ["token_hash"], unique=True)
        batch.create_index("ix_refresh_tokens_user_id", ["user_id"], unique=False)

    op.create_table(
        "password_resets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("password_resets") as batch:
        batch.create_index("ix_password_resets_token_hash", ["token_hash"], unique=True)
        batch.create_index("ix_password_resets_user_id", ["user_id"], unique=False)

    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_jobs") as batch:
        batch.create_index("ix_agent_jobs_agent", ["agent"], unique=False)
        batch.create_index("ix_agent_jobs_status", ["status"], unique=False)
        batch.create_index("ix_agent_jobs_user_id", ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_jobs") as batch:
        batch.drop_index("ix_agent_jobs_user_id")
        batch.drop_index("ix_agent_jobs_status")
        batch.drop_index("ix_agent_jobs_agent")
    op.drop_table("agent_jobs")

    with op.batch_alter_table("password_resets") as batch:
        batch.drop_index("ix_password_resets_user_id")
        batch.drop_index("ix_password_resets_token_hash")
    op.drop_table("password_resets")

    with op.batch_alter_table("refresh_tokens") as batch:
        batch.drop_index("ix_refresh_tokens_user_id")
        batch.drop_index("ix_refresh_tokens_token_hash")
    op.drop_table("refresh_tokens")

    with op.batch_alter_table("candidates") as batch:
        batch.drop_column("source")

    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("published")
        batch.drop_column("location")

    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_oidc_subject")
        batch.drop_column("oidc_subject")
        batch.drop_column("mfa_recovery")
        batch.drop_column("mfa_enabled")
        batch.drop_column("mfa_secret")
        batch.drop_column("last_login_at")
        batch.drop_column("locked_until")
        batch.drop_column("failed_logins")
        batch.drop_column("token_version")
