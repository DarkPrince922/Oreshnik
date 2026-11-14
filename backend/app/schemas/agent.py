from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.agent import AgentSessionStatus
from app.services.agent import APPROVAL_MODES, EFFORTS, WEB_SEARCH_MODES


class AgentSessionCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    llm_profile_id: int
    llm_model: str | None = Field(default=None, max_length=200)  # one of the profile's models; empty = its default
    # settings of the session (all can be changed later, between messages)
    instructions: str | None = Field(default=None, max_length=20_000)
    reasoning_effort: str | None = None
    approval_mode: str = "auto"
    web_search: str = "auto"

    @field_validator("reasoning_effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        return _check_effort(v)

    @field_validator("approval_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        return _check_mode(v)

    @field_validator("web_search")
    @classmethod
    def _web(cls, v: str) -> str:
        return _check_web(v)


def _check_effort(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    if v not in EFFORTS:
        raise ValueError(f"допустимо: {', '.join(EFFORTS)}")
    return v


def _check_web(v: str) -> str:
    if v not in WEB_SEARCH_MODES:
        raise ValueError(f"допустимо: {', '.join(WEB_SEARCH_MODES)}")
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
    web_search: str | None = None

    @field_validator("reasoning_effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        return _check_effort(v)

    @field_validator("approval_mode")
    @classmethod
    def _mode(cls, v: str | None) -> str | None:
        return None if v is None else _check_mode(v)

    @field_validator("web_search")
    @classmethod
    def _web(cls, v: str | None) -> str | None:
        return None if v is None else _check_web(v)


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

    action: Literal["compact", "review", "shell", "revert", "goal", "goal_clear"]
    command: str | None = Field(default=None, max_length=10_000)   # shell
    # goal: a new objective with its token budget — or neither, to set the session's paused goal going again
    objective: str | None = Field(default=None, max_length=4000)
    token_budget: int | None = Field(default=None, ge=10_000)
    target: dict[str, Any] | None = None                          # review: {"type": "uncommittedChanges"} | baseBranch+branch | commit+sha | custom+instructions

    @field_validator("command", "objective")
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
    web_search: str
    goal: dict | None            # {objective, status, token_budget, tokens_used, seconds_used} or None
    context_used: int | None     # tokens in the model's context after the last turn
    context_window: int | None   # the window Codex assumes for the model
    context_window_known: bool   # False: Codex does not know the model; the window is its default, not a fact
    tokens_in: int               # spent by the session in total
    tokens_out: int
    status: AgentSessionStatus
    turn_count: int
    last_error: str | None
    cancel_requested: bool
    event_count: int
    listener_port: int | None   # a listener open for this session right now, or None
    created_at: datetime
    updated_at: datetime


class AgentListenerOpen(BaseModel):
    port: int = Field(ge=1, le=65535)


class AgentListenerOut(BaseModel):
    port: int | None
    opened_at: datetime | None = None
    last_active: datetime | None = None


class AgentListenerRange(BaseModel):
    available: bool
    min: int | None = None
    max: int | None = None


class AgentEventOut(BaseModel):
    seq: int
    kind: str
    payload: dict
    created_at: datetime


class AgentFromScanOut(BaseModel):
    session: AgentSessionOut
    draft: str     # the task proposed for the first message (the user reads and sends it)
    files: int
