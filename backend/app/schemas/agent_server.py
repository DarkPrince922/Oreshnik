from datetime import datetime

from pydantic import BaseModel, Field, field_validator

HOST_RE = r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$"


class AgentServerIn(BaseModel):
    """Where the user's own OpenHands server is and how to log in. A key or a password is kept encrypted and never shown again."""

    ssh_host: str = Field(min_length=1, max_length=255, pattern=HOST_RE)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", min_length=1, max_length=100, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
    ssh_private_key: str | None = Field(default=None, max_length=20_000)  # empty/None: keep the stored one
    ssh_password: str | None = Field(default=None, max_length=1000)
    clear_key: bool = False
    clear_password: bool = False

    @field_validator("ssh_host")
    @classmethod
    def _host(cls, v: str) -> str:
        return v.strip()


class AgentServerOut(BaseModel):
    ssh_host: str
    ssh_port: int
    ssh_user: str
    has_key: bool
    has_password: bool
    host_key_fingerprint: str | None      # confirmed
    pending_fingerprint: str | None       # seen, waiting for confirmation
    status: str
    status_detail: str | None
    openhands_version: str | None
    installing: bool
    checked_at: datetime | None


class InstallLog(BaseModel):
    running: bool
    log: str


class TrustKey(BaseModel):
    fingerprint: str = Field(min_length=10, max_length=120)
