"""scan continuation: parent link, resumed run name, cost baseline

Revision ID: 0004_scan_continuation
Revises: 0003_scans_findings
Create Date: 2026-11-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_scan_continuation"
down_revision: Union[str, None] = "0003_scans_findings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scans",
        sa.Column("parent_scan_id", sa.Integer(), sa.ForeignKey("scans.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("scans", sa.Column("resume_run_name", sa.String(length=200), nullable=True))
    op.add_column("scans", sa.Column("cost_base", sa.Float(), nullable=True))
    op.create_index("ix_scans_parent_scan_id", "scans", ["parent_scan_id"])


def downgrade() -> None:
    op.drop_index("ix_scans_parent_scan_id", table_name="scans")
    op.drop_column("scans", "cost_base")
    op.drop_column("scans", "resume_run_name")
    op.drop_column("scans", "parent_scan_id")
