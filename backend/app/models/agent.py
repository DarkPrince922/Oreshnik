"""A chat with a coding agent (Codex CLI) that runs in the isolated `agent` container (see agentd/main.py).

A session is long-lived: each user message is one *turn* (one `codex exec` / `codex exec resume`), and Codex's own
thread id ties the turns together. Everything the agent did is kept as an ordered list of events, so the page can be
reloaded or reopened and show the same thing.
"""

import enum
import secrets
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_session_key() -> str:
    return "s" + secrets.token_hex(12)


_key = new_session_key


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
    llm_model: Mapped[str | None] = mapped_column(String(200), nullable=True)  # None = the profile's own default
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # Codex's own id; set by the first turn
    # Settings of the session itself, editable between messages and applied to the next one:
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)           # its own system prompt (Codex «developer instructions»)
    # What the conversation's thread actually has as instructions (Codex ignores new ones on an existing thread; a change is sent inside
    # the next message and recorded here). None = none.
    thread_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)  # how hard the model thinks; None = the model's default
    approval_mode: Mapped[str] = mapped_column(String(8), default="auto")             # auto: no questions · ask: approve commands and file changes
    web_search: Mapped[str] = mapped_column(String(8), default="auto")               # Codex's own web search: auto | on | off
    # The goal of the session as Codex last reported it ({objective, status, token_budget, tokens_used, seconds_used}); None = no goal.
    goal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # How full the model's context was after the last turn, and what the session has spent in total (kept across restarts of the thread).
    context_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window_known: Mapped[bool] = mapped_column(default=True)                 # False: Codex does not know the model, the window is its default
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
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


class AgentLive(Base):
    """What the agent is writing right now and has not finished: a message so far, a command's output so far, the turn's diff, the plan.

    One row per such item, replaced as it grows and deleted when the item is finished (the finished item goes into `agent_events`). The page
    shows these rows next to the history; they are not history, so they cost nothing once the turn is over.
    """

    __tablename__ = "agent_live"

    session_id: Mapped[int] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True)   # the item's id, or "__diff__" / "__plan__"
    payload: Mapped[dict] = mapped_column(JSON)
    rev: Mapped[int] = mapped_column(Integer, default=0)               # +1 at every change: a reader sends only what changed
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
