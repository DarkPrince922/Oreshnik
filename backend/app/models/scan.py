import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.llm_profile import LLMProfile
from app.models.runner import Runner
from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ScanStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    cancelling = "cancelling"
    succeeded = "succeeded"      # finished, nothing found
    vulns_found = "vulns_found"  # finished, findings recorded
    failed = "failed"
    cancelled = "cancelled"


ACTIVE_STATUSES = (ScanStatus.queued, ScanStatus.running, ScanStatus.cancelling)
FINAL_STATUSES = (
    ScanStatus.succeeded,
    ScanStatus.vulns_found,
    ScanStatus.failed,
    ScanStatus.cancelled,
)


class ScanMode(str, enum.Enum):
    """Strix's `-m/--scan-mode`. `deep` is Strix's own default and the most expensive."""

    quick = "quick"
    standard = "standard"
    deep = "deep"


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    runner_id: Mapped[int | None] = mapped_column(
        ForeignKey("runners.id", ondelete="SET NULL"), nullable=True
    )
    llm_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_profiles.id", ondelete="SET NULL"), nullable=True
    )

    targets: Mapped[list[str]] = mapped_column(JSONB)
    scan_mode: Mapped[ScanMode] = mapped_column(Enum(ScanMode, name="scan_mode"))
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_budget: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[ScanStatus] = mapped_column(
        Enum(ScanStatus, name="scan_status"), default=ScanStatus.queued, index=True
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # `status` field of Strix's run.json ("completed", "interrupted", "failed", ...).
    run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Continuation of an earlier scan (`strix --resume`): the parent, the Strix run it continues, and what the
    # parent had already cost. Strix's cost is cumulative across a resume, so this scan's own spend is
    # `cost - cost_base`.
    parent_scan_id: Mapped[int | None] = mapped_column(
        ForeignKey("scans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resume_run_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cost_base: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")
    runner: Mapped[Runner | None] = relationship(lazy="joined")
    llm_profile: Mapped[LLMProfile | None] = relationship(lazy="joined")


class Finding(Base):
    """One entry of Strix's vulnerabilities.json; `data` keeps the full original record."""

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    vuln_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cve: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cwe: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cvss: Mapped[float | None] = mapped_column(Float, nullable=True)
    found_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data: Mapped[dict] = mapped_column(JSONB)
