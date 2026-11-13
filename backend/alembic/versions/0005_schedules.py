"""schedules, and the link from a scan to the schedule that started it

Revision ID: 0005_schedules
Revises: 0004_scan_continuation
Create Date: 2026-11-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_schedules"
down_revision: Union[str, None] = "0004_scan_continuation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    mode = postgresql.ENUM("quick", "standard", "deep", name="scan_mode", create_type=False)
    ts = sa.DateTime(timezone=True)
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("cron", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("targets", postgresql.JSONB(), nullable=False),
        sa.Column("scan_mode", mode, nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("max_budget", sa.Float(), nullable=True),
        sa.Column("runner_id", sa.Integer(), sa.ForeignKey("runners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("llm_profile_id", sa.Integer(), sa.ForeignKey("llm_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("next_run_at", ts, nullable=True),
        sa.Column("last_run_at", ts, nullable=True),
        sa.Column("last_scan_id", sa.Integer(), sa.ForeignKey("scans.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_result", sa.String(length=16), nullable=True),
        sa.Column("last_detail", sa.Text(), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", ts, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "name", name="uq_schedules_user_name"),
    )
    op.create_index("ix_schedules_user_id", "schedules", ["user_id"])
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])

    op.add_column("scans", sa.Column("schedule_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_scans_schedule_id", "scans", "schedules", ["schedule_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_scans_schedule_id", "scans", ["schedule_id"])


def downgrade() -> None:
    op.drop_index("ix_scans_schedule_id", table_name="scans")
    op.drop_constraint("fk_scans_schedule_id", "scans", type_="foreignkey")
    op.drop_column("scans", "schedule_id")
    op.drop_index("ix_schedules_next_run_at", table_name="schedules")
    op.drop_index("ix_schedules_user_id", table_name="schedules")
    op.drop_table("schedules")
