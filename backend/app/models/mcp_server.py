"""«MCP-серверы»: external tools for the agents — a command run inside the agent container, or a streamable-HTTP server by URL. Codex
offers their tools to the model next to its own. Secrets (env values, header values, a bearer token) are stored encrypted.

A server belongs to the user who added it; an administrator can also add shared ones, which every user's agents get — with the
administrator's secrets, which those agents can see (a tool's process has them).
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    shared: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str] = mapped_column(String(32))                       # Codex's server name: the model sees mcp__<name>__<tool>
    description: Mapped[str] = mapped_column(String(200), default="")
    command: Mapped[str | None] = mapped_column(Text, nullable=True)    # a command line run in the agent container, e.g. npx -y …
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    env_enc: Mapped[str | None] = mapped_column(Text, nullable=True)    # encrypted JSON {NAME: value} for the command's environment
    headers_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted JSON {Header: value} for a URL server
    token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted bearer token for a URL server
    scope: Mapped[str] = mapped_column(String(8), default="both")
    enabled: Mapped[bool] = mapped_column(default=True)
    startup_timeout_sec: Mapped[int] = mapped_column(Integer, default=30)
    tool_timeout_sec: Mapped[int] = mapped_column(Integer, default=120)
    last_check: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {ok, tools: [{name, description}], error, at}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
