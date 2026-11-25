from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.skill import SKILL_SCOPES


def _scope(v: str | None) -> str | None:
    if v is not None and v not in SKILL_SCOPES:
        raise ValueError(f"допустимо: {', '.join(SKILL_SCOPES)}")
    return v


class SkillCreate(BaseModel):
    skill_md: str = Field(min_length=1, max_length=600_000)
    scope: str = "both"
    shared: bool = False       # administrators only
    enabled: bool = True

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: str) -> str:
        return _scope(v)  # type: ignore[return-value]


class SkillUpdate(BaseModel):
    expected_revision: str | None = None
    skill_md: str | None = Field(default=None, min_length=1, max_length=600_000)
    scope: str | None = None
    enabled: bool | None = None

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: str | None) -> str | None:
        return _scope(v)


class SkillFile(BaseModel):
    path: str
    size: int
    revision: str


class SkillOut(BaseModel):
    id: int
    name: str
    description: str
    skill_md: str
    files: list[SkillFile]
    edit_revision: str
    scope: str
    shared: bool
    enabled: bool
    mine: bool                 # made by the user asking
    editable: bool             # the user asking may change and delete it
    overridden: bool           # a shared skill the user's own skill of the same name replaces
    notes: list[str] = []      # what the panel repaired in SKILL.md on this save (front matter, name, description)
    created_at: datetime
    updated_at: datetime


class SkillFilePreview(BaseModel):
    path: str
    size: int
    text: str | None           # None: not text


class SkillImport(BaseModel):
    skills: list[SkillOut]
    skipped: list[str] = []   # "<folder>: why" for skills in the archive that could not be taken
