"""personas: the skills an agent with the persona gets

Revision ID: 0023_persona_skills
Revises: 0022_mcp_servers
Create Date: 2026-11-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_persona_skills"
down_revision: Union[str, None] = "0022_mcp_servers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("personas", sa.Column("skills", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("personas", "skills")
