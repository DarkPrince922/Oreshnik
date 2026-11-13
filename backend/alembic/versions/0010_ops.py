"""ops runs: a natural-language goal turned into a script by an LLM and run on existing SSH runners

Revision ID: 0010_ops
Revises: 0009_runner_limits
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_ops"
down_revision: Union[str, None] = "0009_runner_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ops_run_status = sa.Enum("drafting", "running", "done", "failed", name="ops_run_status")
ops_host_status = sa.Enum(
    "queued", "running", "succeeded", "failed", "blocked", "unreachable", "cancelled", name="ops_host_status"
)


def upgrade() -> None:
    op.create_table(
        "ops_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("llm_profile_id", sa.Integer(), sa.ForeignKey("llm_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", ops_run_status, nullable=False, server_default="drafting"),
        sa.Column("script", sa.Text(), nullable=True),
        sa.Column("script_error", sa.Text(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "ops_run_hosts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ops_run_id", sa.Integer(), sa.ForeignKey("ops_runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("runner_id", sa.Integer(), sa.ForeignKey("runners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("runner_name", sa.String(100), nullable=False),
        sa.Column("status", ops_host_status, nullable=False, server_default="queued"),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ops_run_hosts")
    op.drop_table("ops_runs")
    ops_host_status.drop(op.get_bind(), checkfirst=True)
    ops_run_status.drop(op.get_bind(), checkfirst=True)
