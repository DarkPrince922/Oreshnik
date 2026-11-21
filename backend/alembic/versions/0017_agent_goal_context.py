"""agent: the session's goal, web search choice, context fill and total token spend

Revision ID: 0017_agent_goal_context
Revises: 0016_agent_thread_instructions
Create Date: 2026-11-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_agent_goal_context"
down_revision: Union[str, None] = "0016_agent_thread_instructions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("web_search", sa.String(8), nullable=False, server_default="auto"))
    op.add_column("agent_sessions", sa.Column("goal", sa.JSON(), nullable=True))
    op.add_column("agent_sessions", sa.Column("context_used", sa.Integer(), nullable=True))
    op.add_column("agent_sessions", sa.Column("context_window", sa.Integer(), nullable=True))
    op.add_column("agent_sessions", sa.Column("context_window_known", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("agent_sessions", sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("agent_sessions", sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    for column in ("tokens_out", "tokens_in", "context_window_known", "context_window", "context_used", "goal", "web_search"):
        op.drop_column("agent_sessions", column)
