from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.agent import AgentSessionStatus
from app.services.agent import APPROVAL_MODES, EFFORTS


class AgentSessionCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    llm_profile_id: int
    llm_model: str | None = Field(default=None, max_length=200)  # one of the profile's models; empty = its default
    # settings of the session (all can be changed later, between messages)
    instructions: str | None = Field(default=None, max_length=20_000)
    reasoning_effort: str | None = None
    approval_mode: str = "auto"

    @field_validator("reasoning_effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        return _check_effort(v)

    @field_validator("approval_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        return _check_mode(v)


def _check_effort(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    if v not in EFFORTS:
        raise ValueError(f"допустимо: {', '.join(EFFORTS)}")
    return v


def _check_mode(v: str) -> str:
    if v not in APPROVAL_MODES:
        raise ValueError(f"допустимо: {', '.join(APPROVAL_MODES)}")
    return v


class AgentSessionUpdate(BaseModel):
    """Partial update of a session's settings; they apply from the next message."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    instructions: str | None = Field(default=None, max_length=20_000)   # empty string clears it
    reasoning_effort: str | None = None                                 # empty string = the model's default
    approval_mode: str | None = None

    @field_validator("reasoning_effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        return _check_effort(v)

    @field_validator("approval_mode")
    @classmethod
    def _mode(cls, v: str | None) -> str | None:
        return None if v is None else _check_mode(v)


class AgentMessage(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class AgentAction(BaseModel):
    """Something to do in the session other than a message: compress the conversation, review the changes, run a command yourself, undo the last turn."""

    action: Literal["compact", "review", "shell", "revert"]
    command: str | None = Field(default=None, max_length=10_000)   # shell
    target: dict[str, Any] | None = None                          # review: {"type": "uncommittedChanges"} | baseBranch+branch | commit+sha | custom+instructions

    @field_validator("command")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class AgentApproval(BaseModel):
    decision: Literal["accept", "accept_session", "decline", "cancel", "answer"]
    answers: dict[str, Any] | None = None   # for a question: {question id: answer or [answers]}


class AgentClone(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class AgentFromScan(BaseModel):
    llm_profile_id: int
    llm_model: str | None = Field(default=None, max_length=200)


class AgentSessionOut(BaseModel):
    id: int
    title: str
    llm_profile_id: int | None
    llm_profile_name: str | None
    llm_model: str | None  # the model the session talks to: the one chosen, else the profile's default
    instructions: str | None
    reasoning_effort: str | None
    approval_mode: str
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


class AgentFromScanOut(BaseModel):
    session: AgentSessionOut
    draft: str     # the task proposed for the first message (the user reads and sends it)
    files: int
