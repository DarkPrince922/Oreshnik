"""continuing a scan inside the same scan: how many times, the time spent before, and what was asked

Revision ID: 0006_scan_resume_in_place
Revises: 0005_schedules
Create Date: 2026-11-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_scan_resume_in_place"
down_revision: Union[str, None] = "0005_schedules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scans", sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"))
    # Seconds the scan was active in its earlier runs (started_at is reset on every resume).
    op.add_column("scans", sa.Column("elapsed_base", sa.Float(), nullable=False, server_default="0"))
    # [{"at": iso, "instruction": str | null, "max_budget": float | null}, ...] — one entry per resume.
    op.add_column("scans", sa.Column("resume_history", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("scans", "resume_history")
    op.drop_column("scans", "elapsed_base")
    op.drop_column("scans", "resume_count")
