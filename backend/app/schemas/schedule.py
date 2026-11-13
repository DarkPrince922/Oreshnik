from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.scan import ScanMode


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    cron: str = Field(max_length=100, description="5 fields: minute hour day-of-month month day-of-week")
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True

    # the scan template (same meaning as in POST /api/scans)
    targets: list[str] = Field(min_length=1, max_length=20)
    scan_mode: ScanMode = ScanMode.quick
    instruction: str | None = Field(default=None, max_length=10000)
    max_budget: float | None = Field(default=None, gt=0, le=10000)
    runner_id: int | None = None
    llm_profile_id: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("instruction")
    @classmethod
    def _blank_instruction(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class ScheduleUpdate(BaseModel):
    """Partial update. Nullable fields (`instruction`, `max_budget`, `llm_profile_id`) are cleared by sending null."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    cron: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    targets: list[str] | None = Field(default=None, min_length=1, max_length=20)
    scan_mode: ScanMode | None = None
    instruction: str | None = Field(default=None, max_length=10000)
    max_budget: float | None = Field(default=None, gt=0, le=10000)
    runner_id: int | None = None
    llm_profile_id: int | None = None

    @field_validator("instruction")
    @classmethod
    def _blank_instruction(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class ScheduleOut(BaseModel):
    id: int
    user_id: int
    owner_email: str
    name: str
    cron: str
    timezone: str
    enabled: bool
    targets: list[str]
    scan_mode: ScanMode
    instruction: str | None
    max_budget: float | None
    runner_id: int | None
    runner_name: str | None
    runner_kind: str | None
    llm_profile_id: int | None
    llm_profile_name: str | None
    next_run_at: datetime | None
    next_runs: list[datetime]  # the next few firing times, UTC (empty while disabled)
    last_run_at: datetime | None
    last_scan_id: int | None
    last_result: str | None  # started | skipped | error
    last_detail: str | None
    disabled_reason: str | None
    created_at: datetime


class SchedulePreview(BaseModel):
    valid: bool
    error: str | None = None
    next_runs: list[datetime] = []
    min_interval_minutes: float | None = None


class ScheduleRunOut(BaseModel):
    scan_id: int
