"""An "operational task": a natural-language goal, turned into a shell script by an LLM and run as root over
SSH on one or more existing runners — no human approval step, by the owner's explicit choice (see docs/ARCHITECTURE.md).
Everything the LLM wrote and everything each host printed is kept, so the one thing that *is* enforced (the script is
never silently invisible) still holds.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.llm_profile import LLMProfile
from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OpsRunStatus(str, enum.Enum):
    drafting = "drafting"    # asking the LLM to write the script
    running = "running"      # executing on the hosts, one by one
    done = "done"            # every host was attempted (their own outcomes can still differ)
    failed = "failed"        # never reached a host: the LLM call failed, or the safety guard refused the script


class OpsHostStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"             # connected, ran the script, non-zero exit
    blocked = "blocked"           # the safety guard refused the script before any host was touched
    unreachable = "unreachable"   # could not connect at all (not the script's fault)
    cancelled = "cancelled"       # the run was cancelled before this host's turn


class OpsRun(Base):
    __tablename__ = "ops_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)  # the operator's own words; sent to the LLM verbatim
    llm_profile_id: Mapped[int | None] = mapped_column(ForeignKey("llm_profiles.id", ondelete="SET NULL"), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(200), nullable=True)  # None = the profile's own default
    status: Mapped[OpsRunStatus] = mapped_column(Enum(OpsRunStatus, name="ops_run_status"), default=OpsRunStatus.drafting)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)  # exactly what was sent to every host, verbatim
    script_error: Mapped[str | None] = mapped_column(Text, nullable=True)  # why drafting failed
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why the safety guard refused the script
    cancel_requested: Mapped[bool] = mapped_column(default=False)  # checked between hosts, not mid-script
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")
    llm_profile: Mapped[LLMProfile | None] = relationship(lazy="joined")
    hosts: Mapped[list["OpsRunHost"]] = relationship(
        order_by="OpsRunHost.id", cascade="all, delete-orphan", passive_deletes=True
    )


class OpsRunHost(Base):
    """One row per target runner, created up front (status=queued) so the whole fleet is visible immediately —
    the worker only ever updates a row that already exists, it never adds or removes one."""

    __tablename__ = "ops_run_hosts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ops_run_id: Mapped[int] = mapped_column(ForeignKey("ops_runs.id", ondelete="CASCADE"), index=True)
    runner_id: Mapped[int | None] = mapped_column(ForeignKey("runners.id", ondelete="SET NULL"), nullable=True)
    # Snapshotted at creation: the audit trail must still name the host even if the runner is later renamed or deleted.
    runner_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[OpsHostStatus] = mapped_column(Enum(OpsHostStatus, name="ops_host_status"), default=OpsHostStatus.queued)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)  # stdout + stderr, capped; never a secret (see ops.py)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
