"""«Личности»: who an AI agent is and how it works — a role, a tone, a language, rules — given to it as its instructions.

A persona belongs to the user who made it. An administrator can also make shared ones, which every user sees and picks but only an
administrator changes. A session of «Код» (and, later, of «Агент») names the persona it runs with.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

PERSONA_SCOPES = ("both", "code", "agent")  # where it can be picked: everywhere, only in «Код», only in «Агент»


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)  # who made it
    shared: Mapped[bool] = mapped_column(default=False)        # made by an administrator for everybody
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200), default="")
    instructions: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(8), default="both")
    is_default: Mapped[bool] = mapped_column(default=False)    # the owner's own default for new sessions (one per user)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
