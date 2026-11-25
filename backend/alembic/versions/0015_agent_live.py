"""agent: per-session settings (instructions, effort, approvals) and the live snapshots of what is being written

Revision ID: 0015_agent_live
Revises: 0014_agent_ops_model
Create Date: 2026-11-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_agent_live"
down_revision: Union[str, None] = "0014_agent_ops_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("instructions", sa.Text(), nullable=True))
    op.add_column("agent_sessions", sa.Column("reasoning_effort", sa.String(16), nullable=True))
    op.add_column("agent_sessions", sa.Column("approval_mode", sa.String(8), nullable=False, server_default="auto"))
    op.create_table(
        "agent_live",
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_live")
    op.drop_column("agent_sessions", "approval_mode")
    op.drop_column("agent_sessions", "reasoning_effort")
    op.drop_column("agent_sessions", "instructions")
