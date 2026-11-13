"""A chat with a coding agent (Codex CLI) that runs in the isolated `agent` container (see agentd/main.py).

A session is long-lived: each user message is one *turn* (one `codex exec` / `codex exec resume`), and Codex's own
thread id ties the turns together. Everything the agent did is kept as an ordered list of events, so the page can be
reloaded or reopened and show the same thing.
"""

import enum
import secrets
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key() -> str:
    return "s" + secrets.token_hex(12)


class AgentSessionStatus(str, enum.Enum):
    idle = "idle"          # waiting for the next message
    running = "running"    # a turn is in progress (at most one per session)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Names the session's working directory inside the agent container; unguessable, never shown as an id elsewhere.
    key: Mapped[str] = mapped_column(String(32), unique=True, default=_key)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    llm_profile_id: Mapped[int | None] = mapped_column(ForeignKey("llm_profiles.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[AgentSessionStatus] = mapped_column(
        Enum(AgentSessionStatus, name="agent_session_status"), default=AgentSessionStatus.idle
    )
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # Codex's own id; set by the first turn
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    next_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # Bumped by the worker for every event of a running turn; a running session that stops beating lost its worker.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_agent_events_session_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # "user" (the operator's message), "turn.error" (the panel's own note about a failed turn), or what Codex emitted:
    # thread.started / turn.started / item.started / item.updated / item.completed / turn.completed / turn.failed / error.
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
