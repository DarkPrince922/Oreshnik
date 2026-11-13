from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER = "CHANGE_ME"

# Strix's own accepted values for STRIX_REASONING_EFFORT (strix.config.settings.ReasoningEffort).
STRIX_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    oreshnik_env: str = "dev"
    secret_encryption_key: str = "CHANGE_ME_generate_a_fernet_key"
    jwt_secret: str = "CHANGE_ME_random_hex"
    jwt_expire_minutes: int = 720
    jwt_algorithm: str = "HS256"

    # First-run admin. If the password is empty a random one is generated and
    # written to the log once (only when the users table is empty).
    admin_email: str = "admin@oreshnik.dev"
    admin_password: str = ""

    # Login throttling (failed attempts per email+IP within the window)
    login_max_failures: int = 10
    login_window_seconds: int = 900

    # Database
    postgres_user: str = "oreshnik"
    postgres_password: str = "oreshnik"
    postgres_db: str = "oreshnik"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"

    # Runner / Strix
    strix_runs_dir: str = "/data/strix_runs"
    default_runner: str = "mock"
    strix_bin: str = "strix"
    # Wall-clock safety net per scan (0 disables). Strix's own --max-budget is the
    # primary spend limit; this only stops a hung process.
    scan_timeout_minutes: int = 180
    # Seconds between SIGINT (graceful stop) and SIGTERM; SIGKILL follows after the same time.
    strix_cancel_grace_seconds: int = 20
    # A running scan whose worker has not heartbeated for this long is marked failed.
    scan_stale_seconds: int = 180
    # Guard against accidentally burning tokens with many parallel scans: active (queued + running) scans one account may
    # have on ONE runner. A runner can set its own number (`runners.max_scans_per_user`); this is the default for the rest.
    max_active_scans_per_user: int = 3
    scan_log_max_bytes: int = 20_000_000
    # Pace of the simulated runner (seconds between lines). Tests set 0.
    mock_delay_seconds: float = 0.35

    # Scheduling. Real (paid) runners cannot be scheduled more often than this; the simulated runner is exempt.
    schedule_min_interval_minutes: int = 15
    max_schedules_per_user: int = 20

    # What the Strix CLI is allowed to send out by itself. Both are OFF by default: a pentest tool should not report to anybody
    # about scans of your systems. Strix's own telemetry (PostHog and Scarf: model name, finding severities and CWEs, duration,
    # cost, …) and its daily "is there a newer version?" request to PyPI. The panel pins the Strix version on purpose, so the
    # update notice is of no use to it. Turn either one on only if you want it.
    strix_telemetry: bool = False
    strix_update_check: bool = False
    # How hard each model call thinks before answering: none|minimal|low|medium|high|xhigh|max. "high" is Strix's own
    # default (unset is the same as "high"), so the default here changes nothing. Lower trades thoroughness for speed
    # and cost; only matters for a model LiteLLM's own catalog marks as reasoning-capable — for a model it doesn't
    # recognise (most "custom" base_url profiles, and some providers) Strix silently ignores this.
    strix_reasoning_effort: str = "high"
    # How long Strix waits for the model to answer its one tiny test request before the scan starts (Strix's own
    # LLM_PREFLIGHT_TIMEOUT, 30 s by default). Raise it for a slow gateway or a model that thinks long before its first token;
    # it does not help when the runner cannot reach the endpoint at all.
    llm_preflight_timeout: int = Field(default=30, ge=1, le=600)

    # Операции: how long one host's script may run before it is given up on and marked failed.
    ops_host_timeout_seconds: int = Field(default=600, ge=1, le=7200)

    @field_validator("strix_reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("strix_reasoning_effort")
    @classmethod
    def _check_reasoning_effort(cls, value: str) -> str:
        if value not in STRIX_REASONING_EFFORTS:
            raise ValueError(
                f"STRIX_REASONING_EFFORT must be one of {', '.join(STRIX_REASONING_EFFORTS)}, got {value!r}"
            )
        return value

    # SSH runners
    ssh_connect_timeout_seconds: int = 15
    # How often the worker re-checks that SSH runners are reachable (0 disables the periodic check).
    ssh_check_interval_seconds: int = 120
    # While a remote scan runs, its run files are mirrored to the panel this often (live cost, agent feed).
    ssh_sync_interval_seconds: float = 2.0
    # Upper bounds for what is fetched from a remote run directory (a hostile or broken host must not fill the disk).
    ssh_fetch_max_mb: int = 500
    ssh_fetch_max_files: int = 20000
    # What the panel installs on a remote host. The panel was verified against this Strix version.
    strix_version: str = "1.7.0"
    strix_sandbox_image: str = "ghcr.io/usestrix/strix-sandbox:1.3.0"

    # Agent («Агент»): Codex CLI in its own container (agentd/). The token must be the same on both sides; empty = feature off.
    agent_url: str = "http://agent:8100"
    agent_token: str = ""
    # Wall-clock limit of ONE turn (one message to the agent).
    agent_turn_timeout_seconds: int = Field(default=1800, ge=10, le=24 * 3600)
    max_agent_sessions_per_user: int = 20
    # A turn whose worker has not stored an event for this long is given up on.
    agent_stale_seconds: int = Field(default=300, ge=30)

    # CORS
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_dev(self) -> bool:
        return self.oreshnik_env == "dev"

    def insecure_secrets(self) -> list[str]:
        """Names of secrets that still hold placeholder / too-weak values."""
        problems = []
        if PLACEHOLDER in self.secret_encryption_key:
            problems.append("SECRET_ENCRYPTION_KEY")
        if PLACEHOLDER in self.jwt_secret or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
