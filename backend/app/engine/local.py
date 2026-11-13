"""LocalRunner: runs the real `strix` CLI as a subprocess on the worker's host.

Facts this relies on (verified against strix-agent 1.7.0):
  * headless mode is `-n`; scan mode is `-m quick|standard|deep`
  * results go to ./strix_runs/<name>/ relative to the working directory
  * exit codes: 0 finished, 1 error/preflight failure, 2 findings (argparse errors also exit 2!)
  * SIGINT triggers a graceful shutdown (run.json status "interrupted", sandbox cleanup)
  * sandbox containers carry the label strix-run-id=$STRIX_RUN_ID
  * Strix keeps config in ~/.strix, so every scan gets its own HOME
"""

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from app.core.config import settings
from app.engine.base import RunResult, ScanSpec
from app.engine.docker_cleanup import cleanup_containers
from app.services.scan_files import INSTRUCTION_NAME

log = logging.getLogger("oreshnik.local_runner")

# The only worker environment variables the Strix process may inherit. In particular the
# panel's own secrets (JWT_SECRET, SECRET_ENCRYPTION_KEY, DB password, ...) must not leak.
_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)


def strix_argv(spec: ScanSpec, strix_bin: str) -> list[str]:
    """The Strix command line for a scan (shared by the local and the SSH runner, so both always agree)."""
    # `--opt=value` form: a value starting with "-" can never be parsed as an option.
    argv = [strix_bin, "-n", "-m", spec.scan_mode]
    if spec.resume_run:
        # Strix refuses --resume together with --target: the targets come from the run being continued.
        argv.append(f"--resume={spec.resume_run}")
    else:
        argv += [f"--target={t}" for t in spec.targets]
    if spec.instruction:
        argv.append(f"--instruction-file={INSTRUCTION_NAME}")
    if spec.max_budget:
        argv.append(f"--max-budget={spec.max_budget:g}")
    return argv


class LocalRunner:
    def __init__(self, strix_bin: str | None = None):
        self.strix_bin = strix_bin or settings.strix_bin

    # ── command construction (pure; unit-tested) ─────────────────────────

    def build_argv(self, spec: ScanSpec) -> list[str]:
        return strix_argv(spec, self.strix_bin)

    def build_env(self, spec: ScanSpec) -> dict[str, str]:
        env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
        env.update(
            {
                "HOME": str(spec.work_dir / "home"),
                "TERM": "dumb",
                "NO_COLOR": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "STRIX_RUN_ID": spec.run_label,
            }
        )
        env.update(spec.env)
        return env

    # ── execution ────────────────────────────────────────────────────────

    def run(
        self,
        spec: ScanSpec,
        on_line: Callable[[str], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        spec.work_dir.mkdir(parents=True, exist_ok=True)
        (spec.work_dir / "home").mkdir(exist_ok=True)
        if spec.instruction:
            (spec.work_dir / INSTRUCTION_NAME).write_text(spec.instruction, encoding="utf-8")

        argv = self.build_argv(spec)
        on_line(f"$ {' '.join(argv)}")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=spec.work_dir,
                env=self.build_env(spec),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group: signals reach every child
            )
        except FileNotFoundError:
            return RunResult(error=f"Не найден исполняемый файл «{self.strix_bin}» (установлен ли Strix?)")
        except OSError as exc:
            return RunResult(error=f"Не удалось запустить Strix: {exc}")

        reader = threading.Thread(target=self._pump, args=(proc, on_line), daemon=True)
        reader.start()

        result = RunResult()
        deadline = time.monotonic() + spec.timeout_seconds if spec.timeout_seconds else None
        stop_sent_at: float | None = None
        escalation = 0  # 0 = nothing sent, 1 = SIGINT, 2 = SIGTERM, 3 = SIGKILL
        grace = max(1, settings.strix_cancel_grace_seconds)

        try:
            while proc.poll() is None:
                now = time.monotonic()
                if stop_sent_at is None:
                    if should_cancel():
                        result.cancelled = True
                    elif deadline is not None and now > deadline:
                        result.timed_out = True
                    if result.cancelled or result.timed_out:
                        self._signal(proc, signal.SIGINT)
                        escalation, stop_sent_at = 1, now
                else:
                    waited = now - stop_sent_at
                    if escalation == 1 and waited > grace:
                        self._signal(proc, signal.SIGTERM)
                        escalation = 2
                    elif escalation == 2 and waited > grace * 2:
                        self._signal(proc, signal.SIGKILL)
                        escalation = 3
                time.sleep(0.2)
        finally:
            if proc.poll() is None:  # we are being unwound (e.g. worker shutdown): don't orphan Strix
                self._signal(proc, signal.SIGKILL)
            reader.join(timeout=5)
            cleanup_containers(spec.run_label)

        result.exit_code = proc.returncode
        return result

    @staticmethod
    def _pump(proc: subprocess.Popen, on_line: Callable[[str], None]) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        proc.stdout.close()

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            log.warning("cannot signal process group %s: %s", proc.pid, exc)
