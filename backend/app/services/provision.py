"""Installing what an SSH runner needs: Docker, Strix, and the little the panel itself relies on.

The panel does not guess: it first looks at the host (`ssh_runner.probe`), then builds a **plan** — only the steps that are
missing — and runs them one by one, streaming everything they print into a log the administrator can read. A step that fails
stops the installation; nothing is retried behind the user's back.

What it will run, in the worst case (a bare Debian/Ubuntu host):

    1. Docker via the official convenience script (https://get.docker.com), the service enabled, the user added to `docker`
    2. python3 / setsid from the distribution (apt) if missing
    3. uv (https://astral.sh/uv) and Strix pinned to the version the panel was verified against, installed for the user
    4. the Strix sandbox image pulled in advance (it is large; the first scan would otherwise wait for it)

Steps 1–2 need root (root login or `sudo` without a password); 3–4 only need the user. On anything other than Debian/Ubuntu the
panel does not install system packages: it says what is missing and leaves it to the administrator.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import settings
from app.models.runner import Runner, RunnerStatus
from app.services.scan_files import clean_line
from app.services.ssh import SshConnection, SshError, SshTarget, q
from app.services.ssh_runner import REMOTE_PATH, RemoteState, describe_state, probe, target_for

log = logging.getLogger("oreshnik.provision")

STEP_TIMEOUT_SECONDS = 25 * 60  # pulling the sandbox image on a slow link is the long one
LOG_LIMIT = 200_000

PRELUDE = (
    REMOTE_PATH
    + """
export DEBIAN_FRONTEND=noninteractive
SUDO=""
if [ "$(id -u)" != 0 ]; then SUDO="sudo -n"; fi
set -e
"""
)

ENSURE_CURL = """
if ! command -v curl >/dev/null 2>&1; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y --no-install-recommends curl ca-certificates
fi
"""


@dataclass(frozen=True)
class InstallCommands:
    """The shell snippets that really install things. The defaults are the real installers; tests substitute harmless ones."""

    docker: str
    start_docker: str
    docker_access: str
    python3: str
    setsid: str
    uv: str
    strix: str
    pull_image: str


def default_commands(version: str | None = None, image: str | None = None) -> InstallCommands:
    version = version or settings.strix_version
    image = image or settings.strix_sandbox_image
    return InstallCommands(
        docker=ENSURE_CURL
        + """
curl -fsSL https://get.docker.com -o "$HOME/get-docker.sh"
$SUDO sh "$HOME/get-docker.sh"
rm -f "$HOME/get-docker.sh"
$SUDO systemctl enable --now docker || $SUDO service docker start || true
$SUDO usermod -aG docker "$(id -un)"
""",
        start_docker='$SUDO systemctl enable --now docker || $SUDO service docker start || true\n',
        docker_access='$SUDO usermod -aG docker "$(id -un)"\n',
        python3="$SUDO apt-get update -y\n$SUDO apt-get install -y --no-install-recommends python3\n",
        setsid="$SUDO apt-get update -y\n$SUDO apt-get install -y --no-install-recommends util-linux\n",
        uv=ENSURE_CURL + "curl -LsSf https://astral.sh/uv/install.sh | sh\n",
        strix=(
            'export PATH="$HOME/.local/bin:$PATH"\n'
            f"uv tool install --python 3.12 --force {q(f'strix-agent=={version}')}\n"
            "strix --version\n"
        ),
        pull_image=f"docker pull {q(image)}\n",
    )


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    script: str
    needs_root: bool


class ProvisionError(Exception):
    """Why the installation cannot start. Phrased for the administrator."""


def plan(state: RemoteState, commands: InstallCommands) -> list[Step]:
    """The steps still needed on this host, in order. Empty when everything is already there."""
    steps: list[Step] = []
    docker_touched = False
    if not state.docker_cli:
        steps.append(Step("docker", "Установка Docker (официальный скрипт get.docker.com)", commands.docker, True))
        docker_touched = True
    elif not state.docker_ok:
        steps.append(Step("docker-start", "Запуск службы Docker", commands.start_docker, True))
        steps.append(Step("docker-access", "Доступ пользователя к Docker (группа docker)", commands.docker_access, True))
        docker_touched = True
    if not state.python3:
        steps.append(Step("python3", "Установка python3", commands.python3, True))
    if not state.setsid:
        steps.append(Step("setsid", "Установка util-linux (setsid)", commands.setsid, True))
    if not state.strix_path:
        if not state.uv:
            steps.append(Step("uv", "Установка uv (менеджер Python-инструментов)", commands.uv, False))
        steps.append(Step("strix", f"Установка Strix {settings.strix_version}", commands.strix, False))
    if steps and (docker_touched or not state.strix_path):
        steps.append(Step("image", "Загрузка образа песочницы Strix", commands.pull_image, False))

    if not steps:
        return steps
    root_steps = [s.title for s in steps if s.needs_root]
    if root_steps:
        if not state.can_root:
            raise ProvisionError(
                "Для установки нужны права root или sudo без пароля у пользователя "
                f"«{state.user or '?'}»: {'; '.join(root_steps)}. Дайте права или установите вручную."
            )
        if not state.apt_based:
            raise ProvisionError(
                f"Автоматическая установка системных пакетов поддерживает только Debian и Ubuntu (на хосте: «{state.os_id or 'неизвестно'}»). "
                f"Установите вручную: {'; '.join(root_steps)}."
            )
    needs_curl = any(s.key in ("uv", "docker") for s in steps)
    if needs_curl and not state.curl and not (state.can_root and state.apt_based):
        raise ProvisionError("Нужен curl, а установить его без прав root и apt нельзя — установите curl вручную.")
    return steps


@dataclass
class ProvisionOutcome:
    status: RunnerStatus
    detail: str


def _stream(conn: SshConnection, script: str, emit: Callable[[str], None]) -> int | None:
    """Run a script on the host, passing its output to `emit` as it arrives. Returns the exit status (None if it did not end)."""
    proc = conn.start("sh -s", input=script)
    deadline = time.monotonic() + STEP_TIMEOUT_SECONDS
    try:
        while True:
            for line in proc.lines(0.5):
                emit(clean_line(line))
            if proc.finished:
                return proc.exit_status
            if proc.error is not None and proc.exit_status is None:
                emit(f"Соединение оборвано: {proc.error.message}")
                return None
            if time.monotonic() > deadline:
                emit(f"Превышено время шага ({STEP_TIMEOUT_SECONDS // 60} мин) — шаг прерван")
                return None
    finally:
        proc.close()


def run_provision(runner: Runner, emit: Callable[[str], None], commands: InstallCommands | None = None) -> ProvisionOutcome:
    """Install what is missing on the runner's host. Never raises: the outcome (and the log) say what happened."""
    commands = commands or default_commands()
    try:
        target: SshTarget = target_for(runner)
        with SshConnection(target) as conn:
            state = probe(conn)
    except SshError as exc:
        emit(f"✗ Не удалось подключиться: {exc.message}")
        return ProvisionOutcome(RunnerStatus.offline, exc.message)
    emit(f"Хост: {state.user}@{state.os_id or 'unknown'} {state.os_version}".strip())

    try:
        steps = plan(state, commands)
    except ProvisionError as exc:
        emit(f"✗ {exc}")
        return ProvisionOutcome(RunnerStatus.offline, str(exc))
    if not steps:
        emit("Всё необходимое уже установлено — делать нечего.")
    for number, step in enumerate(steps, 1):
        emit(f"▶ Шаг {number}/{len(steps)}: {step.title}")
        try:
            with SshConnection(target) as conn:  # a fresh login per step, so a new group membership applies to the next one
                status = _stream(conn, PRELUDE + step.script, emit)
        except SshError as exc:
            emit(f"✗ {exc.message}")
            return ProvisionOutcome(RunnerStatus.offline, f"Установка остановилась на шаге «{step.title}»: {exc.message}")
        if status != 0:
            emit(f"✗ Шаг завершился с кодом {status}" if status is not None else "✗ Шаг не завершился")
            return ProvisionOutcome(RunnerStatus.offline, f"Установка остановилась на шаге «{step.title}» — подробности в журнале")
        emit("✓ Готово")

    try:
        with SshConnection(target) as conn:
            conn.makedirs("oreshnik")
            after = probe(conn)
    except SshError as exc:
        emit(f"✗ Не удалось проверить результат: {exc.message}")
        return ProvisionOutcome(RunnerStatus.offline, exc.message)
    status, detail = describe_state(after)
    emit(("✓ " if status == RunnerStatus.online else "✗ ") + detail)
    return ProvisionOutcome(status, detail)


class LogBuffer:
    """Collects the installation log and hands it to `save` (a database write) at most every `interval` seconds."""

    def __init__(self, save: Callable[[str], None], interval: float = 1.5):
        self.text = ""
        self._save = save
        self._interval = interval
        self._last = 0.0
        self._truncated = False

    def __call__(self, line: str) -> None:
        if self._truncated:
            return
        self.text += line + "\n"
        if len(self.text) > LOG_LIMIT:
            self.text = self.text[:LOG_LIMIT] + "\n[журнал обрезан]\n"
            self._truncated = True
        now = time.monotonic()
        if now - self._last >= self._interval:
            self.flush()

    def flush(self) -> None:
        self._last = time.monotonic()
        try:
            self._save(self.text)
        except Exception:
            log.exception("could not save the provisioning log")
