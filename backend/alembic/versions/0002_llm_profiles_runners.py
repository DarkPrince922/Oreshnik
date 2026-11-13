"""llm_profiles and runners tables

Revision ID: 0002_llm_profiles_runners
Revises: 0001_initial
Create Date: 2026-11-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_llm_profiles_runners"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    provider = postgresql.ENUM(
        "claude", "openai", "grok", "custom", name="llm_provider", create_type=False
    )
    kind = postgresql.ENUM("mock", "local", "ssh", name="runner_kind", create_type=False)
    status = postgresql.ENUM(
        "online", "offline", "provisioning", "error", name="runner_status", create_type=False
    )
    for enum in (provider, kind, status):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "llm_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider", provider, nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=False),
        sa.Column("api_key_hint", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_llm_profiles_user_name"),
    )
    op.create_index("ix_llm_profiles_user_id", "llm_profiles", ["user_id"])

    op.create_table(
        "runners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("kind", kind, nullable=False),
        sa.Column("status", status, nullable=False, server_default="offline"),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("ssh_host", sa.String(length=255), nullable=True),
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("ssh_user", sa.String(length=100), nullable=True),
        sa.Column("ssh_key_enc", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("runners")
    op.drop_index("ix_llm_profiles_user_id", table_name="llm_profiles")
    op.drop_table("llm_profiles")
    for name in ("runner_status", "runner_kind", "llm_provider"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
