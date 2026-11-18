import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.persona import PERSONA_SCOPES


def _scope(v: str | None) -> str | None:
    if v is not None and v not in PERSONA_SCOPES:
        raise ValueError(f"допустимо: {', '.join(PERSONA_SCOPES)}")
    return v


def _skill_names(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    out: list[str] = []
    for name in v:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name or "") or len(name) > 64:
            raise ValueError(f"недопустимое имя навыка: {name!r}")
        if name not in out:
            out.append(name)
    return out


class PersonaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    instructions: str = Field(min_length=1, max_length=20_000)
    scope: str = "both"
    shared: bool = False       # administrators only
    is_default: bool = False   # own personas only
    skills: list[str] | None = Field(default=None, max_length=200)   # None: every enabled skill

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str] | None) -> list[str] | None:
        return _skill_names(v)

    @field_validator("name", "instructions")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("не может быть пустым")
        return v.strip()

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: str) -> str:
        return _scope(v)  # type: ignore[return-value]


class PersonaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=200)
    instructions: str | None = Field(default=None, min_length=1, max_length=20_000)
    scope: str | None = None
    is_default: bool | None = None
    skills: list[str] | None = Field(default=None, max_length=200)   # sent as null: back to every enabled skill

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str] | None) -> list[str] | None:
        return _skill_names(v)

    @field_validator("name", "instructions")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("не может быть пустым")
        return v.strip() if v is not None else None

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: str | None) -> str | None:
        return _scope(v)


class PersonaOut(BaseModel):
    id: int
    name: str
    description: str
    instructions: str
    scope: str
    shared: bool
    is_default: bool
    mine: bool         # made by the user asking
    editable: bool     # the user asking may change and delete it
    sessions: int      # sessions of the user asking that run with it
    skills: list[str] | None
    created_at: datetime
    updated_at: datetime
