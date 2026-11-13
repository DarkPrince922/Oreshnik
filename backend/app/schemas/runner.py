from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.runner import RunnerKind, RunnerStatus

_MAX_KEY_LEN = 16384


def _check_private_key(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if "PRIVATE KEY" not in value or len(value) > _MAX_KEY_LEN:
        raise ValueError("ssh_private_key must be a PEM/OpenSSH private key")
    return value + "\n"


class RunnerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: RunnerKind
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=100)
    ssh_private_key: str | None = None

    _key = field_validator("ssh_private_key")(_check_private_key)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _ssh_requirements(self) -> "RunnerCreate":
        if self.kind == RunnerKind.ssh:
            if not (self.ssh_host and self.ssh_user and self.ssh_private_key):
                raise ValueError("ssh runners require ssh_host, ssh_user and ssh_private_key")
        else:
            # SSH details make no sense for other kinds — drop them.
            self.ssh_host = self.ssh_user = self.ssh_private_key = None
            self.ssh_port = 22
        return self


class RunnerUpdate(BaseModel):
    """Kind is immutable. `ssh_private_key` is only replaced when provided."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=100)
    ssh_private_key: str | None = None

    _key = field_validator("ssh_private_key")(_check_private_key)


class RunnerOut(BaseModel):
    id: int
    name: str
    kind: RunnerKind
    status: RunnerStatus
    status_detail: str | None
    last_seen_at: datetime | None
    created_at: datetime
    # Visible to admins only:
    ssh_host: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    has_ssh_key: bool = False
    ssh_host_key_fingerprint: str | None = None          # the pinned host key
    ssh_host_key_pending_fingerprint: str | None = None  # a key waiting for confirmation
    ssh_host_key_algorithm: str | None = None
    has_provision_log: bool = False


class TrustHostKey(BaseModel):
    """The fingerprint the administrator compared — it must be the one the panel is currently holding as pending."""

    fingerprint: str = Field(min_length=10, max_length=120)


class ProvisionLog(BaseModel):
    running: bool
    log: str
