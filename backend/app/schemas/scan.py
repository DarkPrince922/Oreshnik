from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.scan import ScanMode, ScanStatus


class ScanCreate(BaseModel):
    targets: list[str] = Field(min_length=1, max_length=20)
    # Strix defaults to "deep" (slow and expensive); the panel defaults to the cheapest mode.
    scan_mode: ScanMode = ScanMode.quick
    instruction: str | None = Field(default=None, max_length=10000)
    max_budget: float | None = Field(default=None, gt=0, le=10000, description="USD, passed as --max-budget")
    runner_id: int | None = None
    llm_profile_id: int | None = None

    @field_validator("instruction")
    @classmethod
    def _blank_instruction(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class FindingBrief(BaseModel):
    id: int
    vuln_id: str
    title: str
    severity: str
    target: str | None
    endpoint: str | None
    method: str | None
    cve: str | None
    cwe: str | None
    cvss: float | None
    found_at: datetime | None


class FindingOut(FindingBrief):
    # The full original Strix record. Its text is attacker-influenced — render as plain text.
    data: dict[str, Any]


class ScanOut(BaseModel):
    id: int
    user_id: int
    owner_email: str
    runner_id: int | None
    runner_name: str | None
    llm_profile_id: int | None
    llm_profile_name: str | None
    targets: list[str]
    scan_mode: ScanMode
    instruction: str | None  # hidden from viewers: it may contain test credentials
    max_budget: float | None
    status: ScanStatus
    status_detail: str | None
    exit_code: int | None
    run_name: str | None
    run_status: str | None
    cost: float | None
    total_tokens: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    severity_counts: dict[str, int]
    findings_total: int


class ScanList(BaseModel):
    items: list[ScanOut]
    total: int
    limit: int
    offset: int


class LogPage(BaseModel):
    lines: list[str]
    next_offset: int
    finished: bool


class ReportOut(BaseModel):
    markdown: str | None


class AgentInfo(BaseModel):
    id: str
    name: str
    parent_id: str | None
    status: str  # running | waiting | budget_paused | completed | stopped | crashed | failed
    error: str | None = None


class ActivityEvent(BaseModel):
    """One step of the agents' work. All text is untrusted (it quotes the scanned target)."""

    seq: int
    ts: str
    agent: str
    kind: str  # task | message | agent_message | system | tool_call | tool_output
    role: str | None = None
    text: str | None = None
    tool: str | None = None
    call_id: str | None = None
    args: dict[str, Any] | None = None
    summary: str | None = None
    truncated: bool = False


class ActivityPage(BaseModel):
    events: list[ActivityEvent]
    next_cursor: int  # byte offset to pass as `cursor` on the next call
    agents: list[AgentInfo]
    finished: bool
