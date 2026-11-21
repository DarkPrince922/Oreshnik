import re
import shlex
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.skill import SKILL_SCOPES

NAME_RE = r"^[a-z][a-z0-9_-]{0,31}$"
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
# What an MCP command's environment may not set: it would change how the agent itself, or every program it starts, behaves.
RESERVED_ENV = {"PATH", "HOME", "CODEX_HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "NODE_OPTIONS", "SHELL", "USER", "TERM",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}


class Secret(BaseModel):
    """A variable or a header. `value` None keeps the stored value (the panel never shows it back)."""
    name: str = Field(min_length=1, max_length=100)
    value: str | None = Field(default=None, max_length=8000)


def _check_env(items: list[Secret] | None) -> list[Secret] | None:
    for item in items or []:
        if not ENV_RE.match(item.name) or item.name in RESERVED_ENV or item.name.startswith("ORESHNIK_"):
            raise ValueError(f"переменная {item.name!r}: латиница, цифры и _, не с цифры; PATH, HOME и подобные задавать нельзя")
    if items and len({i.name for i in items}) != len(items):
        raise ValueError("переменные повторяются")
    return items


def _check_headers(items: list[Secret] | None) -> list[Secret] | None:
    for item in items or []:
        if not HEADER_RE.match(item.name):
            raise ValueError(f"заголовок {item.name!r}: латиница, цифры и дефисы")
    if items and len({i.name.lower() for i in items}) != len(items):
        raise ValueError("заголовки повторяются")
    return items


def check_command(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    try:
        parts = shlex.split(v)
    except ValueError as exc:
        raise ValueError(f"команду не разобрать: {exc}")
    if not parts:
        return None
    return v.strip()


class McpCreate(BaseModel):
    name: str = Field(pattern=NAME_RE)
    description: str = Field(default="", max_length=200)
    transport: Literal["command", "url"]
    command: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2000, pattern=r"^https?://\S+$")
    env: list[Secret] = Field(default_factory=list, max_length=50)
    headers: list[Secret] = Field(default_factory=list, max_length=50)
    bearer_token: str | None = Field(default=None, max_length=8000)
    scope: str = "both"
    enabled: bool = True
    shared: bool = False
    startup_timeout_sec: int = Field(default=30, ge=1, le=600)
    tool_timeout_sec: int = Field(default=120, ge=1, le=3600)

    _env = field_validator("env")(classmethod(lambda cls, v: _check_env(v)))
    _headers = field_validator("headers")(classmethod(lambda cls, v: _check_headers(v)))
    _command = field_validator("command")(classmethod(lambda cls, v: check_command(v)))

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v not in SKILL_SCOPES:
            raise ValueError(f"допустимо: {', '.join(SKILL_SCOPES)}")
        return v

    @model_validator(mode="after")
    def _transport(self) -> "McpCreate":
        if self.transport == "command" and not self.command:
            raise ValueError("укажите команду запуска")
        if self.transport == "url" and not self.url:
            raise ValueError("укажите адрес сервера")
        return self


class McpUpdate(BaseModel):
    name: str | None = Field(default=None, pattern=NAME_RE)
    description: str | None = Field(default=None, max_length=200)
    transport: Literal["command", "url"] | None = None
    command: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2000, pattern=r"^https?://\S+$")
    env: list[Secret] | None = Field(default=None, max_length=50)          # the full list: a variable left out is removed
    headers: list[Secret] | None = Field(default=None, max_length=50)
    bearer_token: str | None = Field(default=None, max_length=8000)        # "" removes it, null (or left out) keeps it
    scope: str | None = None
    enabled: bool | None = None
    startup_timeout_sec: int | None = Field(default=None, ge=1, le=600)
    tool_timeout_sec: int | None = Field(default=None, ge=1, le=3600)

    _env = field_validator("env")(classmethod(lambda cls, v: _check_env(v)))
    _headers = field_validator("headers")(classmethod(lambda cls, v: _check_headers(v)))
    _command = field_validator("command")(classmethod(lambda cls, v: check_command(v)))

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str | None) -> str | None:
        if v is not None and v not in SKILL_SCOPES:
            raise ValueError(f"допустимо: {', '.join(SKILL_SCOPES)}")
        return v


class McpTool(BaseModel):
    name: str
    description: str = ""


class McpCheck(BaseModel):
    ok: bool
    tools: list[McpTool] = []
    error: str | None = None
    at: datetime | None = None


class McpOut(BaseModel):
    id: int
    name: str
    description: str
    transport: Literal["command", "url"]
    command: str | None
    url: str | None
    env_names: list[str]
    header_names: list[str]
    has_token: bool
    scope: str
    enabled: bool
    shared: bool
    mine: bool
    editable: bool
    overridden: bool
    startup_timeout_sec: int
    tool_timeout_sec: int
    last_check: McpCheck | None
    created_at: datetime
    updated_at: datetime
