import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunnerKind(str, enum.Enum):
    mock = "mock"    # simulated Strix run, no LLM calls
    local = "local"  # strix CLI + Docker on the worker's own host
    ssh = "ssh"      # remote host reached over SSH


class RunnerStatus(str, enum.Enum):
    online = "online"
    offline = "offline"
    provisioning = "provisioning"
    error = "error"


class Runner(Base):
    __tablename__ = "runners"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[RunnerKind] = mapped_column(Enum(RunnerKind, name="runner_kind"))
    status: Mapped[RunnerStatus] = mapped_column(
        Enum(RunnerStatus, name="runner_status"), default=RunnerStatus.offline
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ssh_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Login by password instead of (or in addition to) the key. Encrypted; never returned by the API.
    ssh_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # OpenSSH public key lines. `ssh_host_key` is pinned: every connection is checked against it. `ssh_host_key_pending` is
    # what the host presented and still has to be confirmed by an administrator (fingerprint comparison).
    ssh_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_host_key_pending: Mapped[str | None] = mapped_column(Text, nullable=True)
    provision_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    provision_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
