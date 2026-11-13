from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.ops import OpsHostStatus, OpsRunStatus

MAX_RUNNERS_PER_TASK = 50


class OpsRunCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    goal: str = Field(min_length=1, max_length=10000)
    llm_profile_id: int
    runner_ids: list[int] = Field(min_length=1, max_length=MAX_RUNNERS_PER_TASK)

    @field_validator("goal")
    @classmethod
    def _goal(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("runner_ids")
    @classmethod
    def _unique(cls, v: list[int]) -> list[int]:
        seen, out = set(), []
        for i in v:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out


class OpsRunHostOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    runner_id: int | None
    runner_name: str
    status: OpsHostStatus
    exit_code: int | None
    output: str | None
    started_at: datetime | None
    finished_at: datetime | None


class OpsRunOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    user_id: int
    owner_email: str
    name: str
    goal: str
    llm_profile_id: int | None
    llm_profile_name: str | None
    status: OpsRunStatus
    script: str | None
    script_error: str | None
    blocked_reason: str | None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    hosts: list[OpsRunHostOut]
