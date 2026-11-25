"""«Навыки»: Agent Skills — a folder with SKILL.md (a name, a description, instructions) and any files it needs (scripts, references,
templates). The same open format Codex, OpenHands, Claude Code and others read, so a skill made elsewhere is imported as it is.

A skill belongs to the user who made it; an administrator can also make shared ones, which every user's agents get. An agent sees the
list of names and descriptions and reads a skill's instructions when the task calls for it (or when the user names it: `$name`).
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SKILL_SCOPES = ("both", "code", "agent")  # which agents get it: all, only «Код», only «Агент»


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        Index("uq_skills_shared_name", "name", unique=True, postgresql_where=text("shared")),
        Index("uq_skills_private_name", "user_id", "name", unique=True, postgresql_where=text("NOT shared")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)  # who made it
    shared: Mapped[bool] = mapped_column(default=False)        # made by an administrator for everybody
    name: Mapped[str] = mapped_column(String(64))              # the folder name too: lower-case letters, digits, hyphens
    description: Mapped[str] = mapped_column(Text, default="")
    skill_md: Mapped[str] = mapped_column(Text)                # SKILL.md as it is, front matter included
    files: Mapped[dict] = mapped_column(JSON, default=dict)    # the other files of the folder: relative path -> base64 content
    scope: Mapped[str] = mapped_column(String(8), default="both")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
