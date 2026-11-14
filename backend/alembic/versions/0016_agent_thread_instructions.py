"""agent: remember which instructions the conversation's thread actually has

Revision ID: 0016_agent_thread_instructions
Revises: 0015_agent_live
Create Date: 2026-11-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_agent_thread_instructions"
down_revision: Union[str, None] = "0015_agent_live"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("thread_instructions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_sessions", "thread_instructions")
