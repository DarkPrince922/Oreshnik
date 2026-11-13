"""ssh runners: the pinned host key, a key waiting for confirmation, and the log of the last installation

Revision ID: 0007_ssh_runners
Revises: 0006_scan_resume_in_place
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_ssh_runners"
down_revision: Union[str, None] = "0006_scan_resume_in_place"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # OpenSSH public key lines ("ssh-ed25519 AAAA…"). The trusted one is what every connection is checked against;
    # the pending one is what the host showed last time and still has to be confirmed by an administrator.
    op.add_column("runners", sa.Column("ssh_host_key", sa.Text(), nullable=True))
    op.add_column("runners", sa.Column("ssh_host_key_pending", sa.Text(), nullable=True))
    op.add_column("runners", sa.Column("provision_log", sa.Text(), nullable=True))
    # When the installation started: a worker that died mid-way must not leave the runner "installing" forever.
    op.add_column("runners", sa.Column("provision_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runners", "provision_started_at")
    op.drop_column("runners", "provision_log")
    op.drop_column("runners", "ssh_host_key_pending")
    op.drop_column("runners", "ssh_host_key")
