"""skills: Agent Skills (SKILL.md and its files), the user's own and shared ones

Revision ID: 0021_skills
Revises: 0020_agent_functions_only
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_skills"
down_revision: Union[str, None] = "0020_agent_functions_only"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("skill_md", sa.Text(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False, server_default="both"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skills_user_id", "skills", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_skills_user_id", table_name="skills")
    op.drop_table("skills")
