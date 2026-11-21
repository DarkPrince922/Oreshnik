"""agent servers: each user's own OpenHands server; who may use «Агент»

Revision ID: 0019_agent_servers
Revises: 0018_personas
Create Date: 2026-11-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_agent_servers"
down_revision: Union[str, None] = "0018_personas"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("agent_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "agent_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("ssh_host", sa.String(255), nullable=False),
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("ssh_user", sa.String(100), nullable=False, server_default="root"),
        sa.Column("ssh_key_enc", sa.Text(), nullable=True),
        sa.Column("ssh_password_enc", sa.Text(), nullable=True),
        sa.Column("ssh_host_key", sa.Text(), nullable=True),
        sa.Column("ssh_host_key_pending", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("openhands_version", sa.String(40), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("install_log", sa.Text(), nullable=True),
        sa.Column("install_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("agent_servers")
    op.drop_column("users", "agent_access")
