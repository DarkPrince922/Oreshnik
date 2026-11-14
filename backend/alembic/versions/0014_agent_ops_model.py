"""the model chosen for an agent session and for an ops run

Revision ID: 0014_agent_ops_model
Revises: 0013_llm_models
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_agent_ops_model"
down_revision: Union[str, None] = "0013_llm_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("llm_model", sa.String(200), nullable=True))
    op.add_column("ops_runs", sa.Column("llm_model", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("ops_runs", "llm_model")
    op.drop_column("agent_sessions", "llm_model")
