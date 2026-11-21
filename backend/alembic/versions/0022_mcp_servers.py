"""mcp servers: external tools for the agents, the user's own and shared ones

Revision ID: 0022_mcp_servers
Revises: 0021_skills
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_mcp_servers"
down_revision: Union[str, None] = "0021_skills"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("url", sa.String(2000), nullable=True),
        sa.Column("env_enc", sa.Text(), nullable=True),
        sa.Column("headers_enc", sa.Text(), nullable=True),
        sa.Column("token_enc", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(8), nullable=False, server_default="both"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("startup_timeout_sec", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("tool_timeout_sec", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("last_check", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_servers_user_id", "mcp_servers", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_user_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
