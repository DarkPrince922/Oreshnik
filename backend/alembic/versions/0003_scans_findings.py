"""scans and findings tables

Revision ID: 0003_scans_findings
Revises: 0002_llm_profiles_runners
Create Date: 2026-11-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_scans_findings"
down_revision: Union[str, None] = "0002_llm_profiles_runners"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    mode = postgresql.ENUM("quick", "standard", "deep", name="scan_mode", create_type=False)
    status = postgresql.ENUM(
        "queued",
        "running",
        "cancelling",
        "succeeded",
        "vulns_found",
        "failed",
        "cancelled",
        name="scan_status",
        create_type=False,
    )
    for enum in (mode, status):
        enum.create(bind, checkfirst=True)

    ts = sa.DateTime(timezone=True)
    op.create_table(
        "scans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("runner_id", sa.Integer(), sa.ForeignKey("runners.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "llm_profile_id",
            sa.Integer(),
            sa.ForeignKey("llm_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("targets", postgresql.JSONB(), nullable=False),
        sa.Column("scan_mode", mode, nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("max_budget", sa.Float(), nullable=True),
        sa.Column("status", status, nullable=False, server_default="queued"),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("run_name", sa.String(length=200), nullable=True),
        sa.Column("run_status", sa.String(length=32), nullable=True),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", ts, nullable=True),
        sa.Column("finished_at", ts, nullable=True),
        sa.Column("heartbeat_at", ts, nullable=True),
    )
    op.create_index("ix_scans_user_id", "scans", ["user_id"])
    op.create_index("ix_scans_status", "scans", ["status"])

    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_id", sa.Integer(), sa.ForeignKey("scans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vuln_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=True),
        sa.Column("cve", sa.String(length=64), nullable=True),
        sa.Column("cwe", sa.String(length=64), nullable=True),
        sa.Column("cvss", sa.Float(), nullable=True),
        sa.Column("found_at", ts, nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_findings_scan_id", "findings", ["scan_id"])
    op.create_index("ix_findings_severity", "findings", ["severity"])


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("findings")
    op.drop_table("scans")
    for name in ("scan_status", "scan_mode"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
