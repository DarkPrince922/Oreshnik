"""ssh runners: login by password as an alternative (or addition) to a key

Revision ID: 0008_ssh_password
Revises: 0007_ssh_runners
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_ssh_password"
down_revision: Union[str, None] = "0007_ssh_runners"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fernet-encrypted, like ssh_key_enc. A runner needs at least one of the two.
    op.add_column("runners", sa.Column("ssh_password_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runners", "ssh_password_enc")
