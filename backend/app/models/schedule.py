from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.llm_profile import LLMProfile
from app.models.runner import Runner
from app.models.scan import ScanMode
from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Schedule(Base):
    """A scan that is started automatically by a cron expression.

    The scan template mirrors `POST /api/scans`; every firing creates an ordinary scan owned by the schedule's owner
    (so it uses the owner's LLM profile and counts towards the owner's limits).
    """

    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_schedules_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    cron: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # the scan template
    llm_model: Mapped[str | None] = mapped_column(String(200), nullable=True)  # None = the profile's default model

    targets: Mapped[list[str]] = mapped_column(JSONB)
    scan_mode: Mapped[ScanMode] = mapped_column(Enum(ScanMode, name="scan_mode", create_type=False))
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    runner_id: Mapped[int | None] = mapped_column(ForeignKey("runners.id", ondelete="SET NULL"), nullable=True)
    llm_profile_id: Mapped[int | None] = mapped_column(ForeignKey("llm_profiles.id", ondelete="SET NULL"), nullable=True)

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_id: Mapped[int | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"), nullable=True)
    # "started" | "skipped" | "error", with a human-readable explanation
    last_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the scheduler had to switch the schedule off by itself (owner disabled, profile or runner deleted, ...).
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    user: Mapped[User] = relationship(lazy="joined")
    runner: Mapped[Runner | None] = relationship(lazy="joined")
    llm_profile: Mapped[LLMProfile | None] = relationship(lazy="joined")
