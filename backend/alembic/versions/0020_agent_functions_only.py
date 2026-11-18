"""agent sessions: remember that the model's gateway takes plain function tools only

Revision ID: 0020_agent_functions_only
Revises: 0019_agent_servers
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_agent_functions_only"
down_revision: Union[str, None] = "0019_agent_servers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("functions_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agent_sessions", "functions_only")
