"""«Агент»: each user's own server with OpenHands (Agent Canvas) on it, run as root, reached by the panel over SSH.

One server per user. The panel installs OpenHands there (Node.js 24, uv, the `@openhands/agent-canvas` package, a systemd service
bound to 127.0.0.1:8000 on the server only) and later talks to it through an SSH tunnel. The host key is pinned like a runner's.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# new: the host key is not confirmed yet · offline: not reachable, or OpenHands missing / not answering · installing ·
# online: OpenHands answers on the server · error: login refused, host key changed, the installation failed
AGENT_SERVER_STATUSES = ("new", "offline", "installing", "online", "error")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentServer(Base):
    __tablename__ = "agent_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    ssh_host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(100), default="root")
    ssh_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)          # pinned OpenSSH public key line
    ssh_host_key_pending: Mapped[str | None] = mapped_column(Text, nullable=True)  # seen, waiting for the user's confirmation
    status: Mapped[str] = mapped_column(String(16), default="new")
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    openhands_version: Mapped[str | None] = mapped_column(String(40), nullable=True)  # what answers there now
    # The key Agent Canvas on the server is started with (LOCAL_BACKEND_API_KEY): the panel's way in. Encrypted; never shown.
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    install_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    install_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
