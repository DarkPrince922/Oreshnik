from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.agent import AgentSessionStatus


class AgentSessionCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    llm_profile_id: int


class AgentMessage(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class AgentSessionOut(BaseModel):
    id: int
    title: str
    llm_profile_id: int | None
    llm_profile_name: str | None
    status: AgentSessionStatus
    turn_count: int
    last_error: str | None
    cancel_requested: bool
    event_count: int
    created_at: datetime
    updated_at: datetime


class AgentEventOut(BaseModel):
    seq: int
    kind: str
    payload: dict
    created_at: datetime
