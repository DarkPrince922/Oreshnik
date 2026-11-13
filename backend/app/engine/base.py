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

    return {"LLM_PREFLIGHT_TIMEOUT": str(settings.llm_preflight_timeout)}


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
