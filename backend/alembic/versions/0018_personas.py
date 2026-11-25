"""personas: who an AI agent is, picked per session

Revision ID: 0018_personas
Revises: 0017_agent_goal_context
Create Date: 2026-11-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_personas"
down_revision: Union[str, None] = "0017_agent_goal_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False, server_default="both"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_personas_user_id", "personas", ["user_id"])
    op.add_column("agent_sessions", sa.Column("persona_id", sa.Integer(), sa.ForeignKey("personas.id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_sessions", "persona_id")
    op.drop_index("ix_personas_user_id", table_name="personas")
    op.drop_table("personas")
