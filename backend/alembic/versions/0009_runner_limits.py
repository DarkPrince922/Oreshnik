"""runner limits: scans per account and targets at once, per runner

Revision ID: 0009_runner_limits
Revises: 0008_ssh_password
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_runner_limits"
down_revision: Union[str, None] = "0008_ssh_password"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL = "the panel's default" (MAX_ACTIVE_SCANS_PER_USER): an existing runner keeps behaving as before.
    op.add_column("runners", sa.Column("max_scans_per_user", sa.Integer(), nullable=True))
    # NULL = no limit on how many distinct targets (domains / hosts) are being scanned on the runner at once.
    op.add_column("runners", sa.Column("max_targets", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("runners", "max_targets")
    op.drop_column("runners", "max_scans_per_user")
