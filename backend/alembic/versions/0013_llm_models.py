"""saved model lists on LLM profiles, and the model chosen for a scan / schedule

Revision ID: 0013_llm_models
Revises: 0012_app_settings
Create Date: 2026-11-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_llm_models"
down_revision: Union[str, None] = "0012_app_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_profiles", sa.Column("models", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("scans", sa.Column("llm_model", sa.String(200), nullable=True))
    op.add_column("schedules", sa.Column("llm_model", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("schedules", "llm_model")
    op.drop_column("scans", "llm_model")
    op.drop_column("llm_profiles", "models")
