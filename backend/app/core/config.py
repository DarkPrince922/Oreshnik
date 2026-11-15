from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER = "CHANGE_ME"

# What the panel's STRIX_API_TYPE may be: "auto" lets the panel decide per profile (see services/llm.py), the others are
# Strix's own `STRIX_API_TYPE` values and apply to every profile.
STRIX_API_TYPES = ("auto", "responses", "chat_completions")

# Which tools the agent (Codex) offers its model: "auto" = plain function tools only for a gateway, everything Codex has for OpenAI itself.
AGENT_TOOLS = ("auto", "functions", "full")

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
    # How many scans RUN at once. A scan beyond this waits as `queued` and starts the moment one finishes. The panel itself
    # holds this limit (so it can be changed in «Настройки» without restarting anything); the worker is started with
    # SCAN_WORKER_SLOTS processes as a ceiling it can never exceed.
    scan_concurrency: int = Field(default=15, ge=1, le=64)
    scan_worker_slots: int = Field(default=16, ge=1, le=256)
    # Guard against accidentally burning tokens with many parallel scans: active (queued + running) scans one account may
    # have on ONE runner. A runner can set its own number (`runners.max_scans_per_user`); this is the default for the rest.
    max_active_scans_per_user: int = 5
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
    # Which OpenAI API Strix's agent loop talks to. "auto" (default): a `custom` profile gets chat_completions — an
    # OpenAI-compatible gateway usually implements only that, and Strix's Responses route sends native "custom" tools
    # such as the file tools, which such gateways refuse with 400 "This endpoint supports function tools only" — and every other
    # profile is left to Strix (which picks Responses for models its LiteLLM catalog lists under /v1/responses).
    # "responses" / "chat_completions" force that route for every profile.
    strix_api_type: str = "auto"
    # How long Strix waits for the model to answer its one tiny test request before the scan starts (Strix's own
    # LLM_PREFLIGHT_TIMEOUT, 30 s by default). Raise it for a slow gateway or a model that thinks long before its first token;
    # it does not help when the runner cannot reach the endpoint at all.
    llm_preflight_timeout: int = Field(default=30, ge=1, le=600)
    # During a scan: how long Strix waits for one answer of the model (LLM_TIMEOUT), and how long a streamed answer may stay
    # silent before it is given up on (LLM_STREAM_IDLE_TIMEOUT, 0 = no such limit). Both are Strix's own defaults (300 s), so
    # the defaults here change nothing. Raise them for a slow gateway that cuts requests short (499, timeouts mid-scan).
    llm_timeout: int = Field(default=300, ge=10, le=3600)
    llm_stream_idle_timeout: int = Field(default=300, ge=0, le=3600)

    # ── More of Strix's own tuning, passed to it as environment (app/engine/base.py: STRIX_TUNING). Every default here is Strix's
    # own default or "not set", so nothing changes until one is touched. All of them are editable in the panel.
    # Sandbox container limits (Docker): memory ("4g"), CPUs ("2", "1.5"), processes (0 = no limit), /dev/shm ("1g"), its log files.
    strix_sandbox_mem_limit: str = Field(default="", pattern=r"^(\d{1,6}[bkmgBKMG]?)?$")
    strix_sandbox_cpus: str = Field(default="", pattern=r"^(\d{1,3}(\.\d{1,2})?)?$")
    strix_sandbox_pids_limit: int = Field(default=0, ge=0, le=1_000_000)
    strix_sandbox_shm_size: str = Field(default="", pattern=r"^(\d{1,6}[bkmgBKMG]?)?$")
    strix_sandbox_log_max_size: str = Field(default="50m", pattern=r"^(\d{1,6}[bkmgBKMG]?|off)$")
    strix_sandbox_log_max_file: int = Field(default=3, ge=1, le=100)
    # The model: prompt caching, streaming, forcing a tool call, actions per turn, extra HTTP headers (a JSON object of strings).
    strix_prompt_cache: bool = True
    llm_disable_streaming: bool = False
    strix_force_required_tool_choice: bool = False
    llm_max_tool_calls_per_turn: int = Field(default=32, ge=0, le=1000)
    llm_extra_headers: str = Field(default="", max_length=4000)
    # The agents' context: compaction, the window assumed for a model Strix does not know, how much tool output and how many
    # screenshots are kept.
    strix_context_auto_compact: bool = True
    strix_context_fallback_tokens: int = Field(default=200_000, ge=1_000, le=10_000_000)
    strix_tool_output_max_tokens: int = Field(default=8_000, ge=100, le=1_000_000)
    strix_max_context_images: int = Field(default=3, ge=0, le=100)
    # A cheaper model for merging duplicate findings (empty = the scan's own), web search for the agents, Strix's verbose log.
    strix_dedupe_model: str = Field(default="", pattern=r"^[A-Za-z0-9_./:@-]{0,200}$")
    strix_web_search_provider: str = Field(default="auto", pattern=r"^(auto|perplexity|exa)$")
    strix_debug: bool = False
    # Keys for that web search: secrets, so .env only — never shown or edited in the panel.
    perplexity_api_key: str = ""
    exa_api_key: str = ""
    # Docker network for Strix's sandbox containers (Strix's own STRIX_DOCKER_SANDBOX_NETWORK). Empty = Strix's default: it
    # publishes the sandbox's Caido port on a random host port chosen by dockerd, which can collide ("failed to bind host
    # port ... address already in use") when scans start together. With a network name nothing is published on the host at all
    # and Strix talks to the container by its IP — works on a Linux Docker host, not on Docker Desktop. The panel creates
    # the network if it does not exist; a worker that itself runs in a container must be on that network (docker-compose.yml does it).
    strix_sandbox_network: str = Field(default="", pattern=r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,62})?$")
    # How many times a scan is started again when the sandbox could not bind its host port (the clash above), before it fails.
    sandbox_start_retries: int = Field(default=2, ge=0, le=10)
    # The model did not answer Strix's pre-scan request for a reason that passes by itself (502/503/504, overload, a rate limit, a
    # timeout, a dropped connection): how many times the scan is started again, and how long it waits before each try. A wrong
    # key, an unknown model or a spent quota is never retried. 0 attempts = off.
    llm_retry_attempts: int = Field(default=2, ge=0, le=10)
    llm_retry_delay_seconds: int = Field(default=90, ge=5, le=3600)
    # Which HTTP codes from the model's gateway count as "passes by itself" (comma-separated). 499 = the gateway gave up waiting
    # for the model (nginx's "client closed request"). 400/401/402/403/404/422 are never retried, listed or not.
    llm_retry_codes: str = Field(default="408, 409, 425, 429, 499, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529", pattern=r"^\s*(\d{3}\s*(,\s*\d{3}\s*)*)?$", max_length=400)

    # Операции: how long one host's script may run before it is given up on and marked failed.
    ops_host_timeout_seconds: int = Field(default=600, ge=1, le=7200)

    @field_validator("agent_tools", mode="before")
    @classmethod
    def _normalize_agent_tools(cls, value: object) -> object:
        return value.strip().lower() or "auto" if isinstance(value, str) else value

    @field_validator("agent_tools")
    @classmethod
    def _check_agent_tools(cls, value: str) -> str:
        if value not in AGENT_TOOLS:
            raise ValueError(f"AGENT_TOOLS must be one of {', '.join(AGENT_TOOLS)}, got {value!r}")
        return value

    @field_validator("strix_api_type", mode="before")
    @classmethod
    def _normalize_api_type(cls, value: object) -> object:
        return value.strip().lower() or "auto" if isinstance(value, str) else value

    @field_validator("strix_api_type")
    @classmethod
    def _check_api_type(cls, value: str) -> str:
        if value not in STRIX_API_TYPES:
            raise ValueError(f"STRIX_API_TYPE must be one of {', '.join(STRIX_API_TYPES)}, got {value!r}")
        return value

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
    agent_tools: str = "auto"
    # Complete the usage numbers in a gateway's final stream event (see agentd's compat proxy). Never applied to OpenAI itself.
    agent_response_fix: bool = True
    # A goal makes the agent work by itself, turn after turn: the token budget one goal may get at most (the user picks a number up to this).
    agent_goal_max_tokens: int = Field(default=5_000_000, ge=10_000, le=50_000_000)
    # A turn whose worker has not stored an event for this long is given up on.
    agent_stale_seconds: int = Field(default=300, ge=30)
    # Listener for the agent: a fixed range of ports the panel (this process) has published to the host and proxies, on
    # explicit request, to a single port inside the sandboxed agent container (which itself has no published port and sits on
    # an isolated network — see docs/ARCHITECTURE.md §27). Empty = feature off. Fixed at deploy time: docker-compose must
    # publish the same range, so changing it needs a restart, unlike the rest of «Настройки».
    agent_listener_ports: str = ""
    # A listener nobody has sent a byte through for this long is closed on its own.
    agent_listener_idle_seconds: int = Field(default=1800, ge=30)

    # CORS
    # Written as a plain list — CORS_ORIGINS=http://203.0.113.5:5173,http://localhost:5173 — or as JSON. Docker Compose strips
    # the quotes of an unquoted JSON value in .env, so whatever is left of one (["http://a","http://b"] -> [http://a,http://b])
    # is read too. The union type is what lets a non-JSON value reach the validator at all.
    cors_origins: str | list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _read_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            import json

            text = value.strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":  # quotes around the whole value, kept by some loaders
                text = text[1:-1].strip()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except ValueError:
                pass
            return [part.strip().strip("\"'") for part in text.strip("[]").split(",") if part.strip().strip("\"'")]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _check_cors_origins(cls, value: object) -> object:
        for origin in value if isinstance(value, list) else []:
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS_ORIGINS: «{origin}» is not an address. Example: CORS_ORIGINS=http://203.0.113.5:5173,http://localhost:5173")
        return value

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_dev(self) -> bool:
        return self.oreshnik_env == "dev"

    def encryption_key_problem(self) -> str | None:
        """Why SECRET_ENCRYPTION_KEY cannot be used, or None. Checked at startup, not at the first secret that is saved."""
        from cryptography.fernet import Fernet

        try:
            Fernet(self.secret_encryption_key.encode())
        except Exception:
            return (
                "SECRET_ENCRYPTION_KEY в .env — не ключ Fernet (нужны 32 байта в url-safe base64, 44 символа, в конце «=»). "
                "Без него нельзя сохранить ни один LLM-профиль или SSH-ключ. Создайте ключ: "
                "python3 -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\" "
                "— впишите его в .env и выполните: docker compose up -d --force-recreate. Или запустите ./install.sh: он починит .env сам."
            )
        return None

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
