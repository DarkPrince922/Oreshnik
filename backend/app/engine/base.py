from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.services.ssh import SshTarget


def strix_privacy_env() -> dict[str, str]:
    """Environment that keeps the Strix CLI from phoning home, for every runner alike.

    Always passed explicitly (never inherited from the worker's environment) so the panel's settings are the single switch:
    `STRIX_TELEMETRY` is Strix's own opt-out, `STRIX_NO_UPDATE_CHECK` stops its once-a-day request to PyPI.
    """
    from app.core.config import settings

    env = {"STRIX_TELEMETRY": "1" if settings.strix_telemetry else "0"}
    if not settings.strix_update_check:
        env["STRIX_NO_UPDATE_CHECK"] = "1"
    return env


def strix_reasoning_env() -> dict[str, str]:
    """How hard Strix's model thinks per call (`STRIX_REASONING_EFFORT`), for every runner alike.

    Always passed explicitly, after the scan's own environment, so the admin setting is the one that actually applies
    regardless of what a scan's own `env` happens to carry.
    """
    from app.core.config import settings

    return {"STRIX_REASONING_EFFORT": settings.strix_reasoning_effort}


def strix_llm_timeout_env() -> dict[str, str]:
    """How long Strix waits for its pre-scan test request to the model (`LLM_PREFLIGHT_TIMEOUT`), for every runner alike."""
    from app.core.config import settings

    return {
        "LLM_PREFLIGHT_TIMEOUT": str(settings.llm_preflight_timeout),
        # the same for the scan itself: one answer of the model, and silence inside a streamed one
        "LLM_TIMEOUT": str(settings.llm_timeout),
        "LLM_STREAM_IDLE_TIMEOUT": str(settings.llm_stream_idle_timeout),
    }


def strix_sandbox_env() -> dict[str, str]:
    """`STRIX_DOCKER_SANDBOX_NETWORK` when the admin set a sandbox network (no host port is then published), else nothing."""
    from app.core.config import settings

    return {"STRIX_DOCKER_SANDBOX_NETWORK": settings.strix_sandbox_network} if settings.strix_sandbox_network else {}


# The rest of Strix's tuning: (our setting, Strix's variable). Text is passed only when set, a process limit only above zero,
# the rest always — every default equals Strix's own, so an untouched panel changes nothing.
STRIX_TUNING: tuple[tuple[str, str], ...] = (
    ("strix_sandbox_mem_limit", "STRIX_SANDBOX_MEM_LIMIT"),
    ("strix_sandbox_cpus", "STRIX_SANDBOX_CPUS"),
    ("strix_sandbox_pids_limit", "STRIX_SANDBOX_PIDS_LIMIT"),
    ("strix_sandbox_shm_size", "STRIX_SANDBOX_SHM_SIZE"),
    ("strix_sandbox_log_max_size", "STRIX_SANDBOX_LOG_MAX_SIZE"),
    ("strix_sandbox_log_max_file", "STRIX_SANDBOX_LOG_MAX_FILE"),
    ("strix_prompt_cache", "STRIX_PROMPT_CACHE"),
    ("llm_disable_streaming", "LLM_DISABLE_STREAMING"),
    ("strix_force_required_tool_choice", "STRIX_FORCE_REQUIRED_TOOL_CHOICE"),
    ("llm_max_tool_calls_per_turn", "LLM_MAX_TOOL_CALLS_PER_TURN"),
    ("llm_extra_headers", "LLM_EXTRA_HEADERS"),
    ("strix_context_auto_compact", "STRIX_CONTEXT_AUTO_COMPACT"),
    ("strix_context_fallback_tokens", "STRIX_CONTEXT_FALLBACK_TOKENS"),
    ("strix_tool_output_max_tokens", "STRIX_TOOL_OUTPUT_MAX_TOKENS"),
    ("strix_max_context_images", "STRIX_MAX_CONTEXT_IMAGES"),
    ("strix_dedupe_model", "STRIX_DEDUPE_MODEL"),
    ("strix_web_search_provider", "STRIX_WEB_SEARCH_PROVIDER"),
    ("strix_debug", "STRIX_DEBUG"),
    ("perplexity_api_key", "PERPLEXITY_API_KEY"),
    ("exa_api_key", "EXA_API_KEY"),
)


def strix_tuning_env() -> dict[str, str]:
    """Strix's own knobs the admin set (limits of the sandbox, the model, the context, web search), for every runner alike."""
    from app.core.config import settings

    env: dict[str, str] = {}
    for key, variable in STRIX_TUNING:
        value = getattr(settings, key)
        if isinstance(value, bool):
            env[variable] = "true" if value else "false"
        elif isinstance(value, int):
            if key == "strix_sandbox_pids_limit" and value <= 0:
                continue  # 0 = no limit: leave Docker's own
            env[variable] = str(value)
        elif value:
            env[variable] = str(value)
    return env


@dataclass
class ScanSpec:
    scan_id: int
    targets: list[str]
    scan_mode: str                      # quick | standard | deep
    instruction: str | None
    max_budget: float | None
    work_dir: Path                      # cwd of the run; Strix creates strix_runs/ inside it
    run_label: str                      # tags sandbox containers so they can be found and removed
    timeout_seconds: int | None = None
    # Name of the Strix run to continue (`--resume`); its directory is already in work_dir/strix_runs/.
    resume_run: str | None = None
    # Secret environment for the run (STRIX_LLM / LLM_API_KEY / LLM_API_BASE). Never logged.
    env: dict[str, str] = field(default_factory=dict, repr=False)
    # Where to run, for the SSH runner (decrypted key inside: never log or serialise it).
    ssh: "SshTarget | None" = field(default=None, repr=False)


@dataclass
class RunResult:
    exit_code: int | None = None
    cancelled: bool = False
    timed_out: bool = False
    error: str | None = None            # runner-level failure (binary missing, spawn error, ...)


class Runner(Protocol):
    """Executes one scan and reports progress line by line.

    `on_line` receives raw output lines (the caller cleans/redacts them).
    `should_cancel` is polled frequently; once it returns True the runner must stop
    the run gracefully and return with `cancelled=True`.
    """

    def run(
        self,
        spec: ScanSpec,
        on_line: Callable[[str], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult: ...
