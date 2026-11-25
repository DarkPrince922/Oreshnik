"""«Агент»: a user's own server with OpenHands (Agent Canvas) on it — checking it and installing OpenHands there over SSH.

What the installation leaves on the server (verified against @openhands/agent-canvas 1.41.0 and its docs):

* Node.js 24 (from NodeSource) and `uv` — what Agent Canvas needs to run (it starts its agent server through `uvx`);
* the npm package `@openhands/agent-canvas`, pinned to `settings.openhands_version`;
* `/etc/oreshnik-openhands.env` (mode 600): LOCAL_BACKEND_API_KEY — the key the panel holds — and OH_SECRET_KEY, with which
  Agent Canvas encrypts its own settings (kept across reinstalls, or the stored settings could no longer be read);
* a systemd service `oreshnik-openhands` that runs `agent-canvas` as root. It listens on 127.0.0.1:8000 only: nothing is opened on
  the server's network, the panel reaches it through SSH. As root, the agent has the whole machine — that is the point of «Агент».

Everything here is best effort and never raises: a check or an installation ends in a status and a reason the user can act on.
"""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.crypto import decrypt, encrypt
from app.models.agent_server import AgentServer
from app.services.provision import LogBuffer, _stream  # noqa: F401  (LogBuffer is re-exported for the worker)
from app.services.ssh import SshConnection, SshError, SshErrorKind, SshTarget, fetch_host_key, fingerprint

SERVICE = "oreshnik-openhands"
ENV_FILE = "/etc/oreshnik-openhands.env"
PORT = 8000
NODE_MAJOR = 24
MIN_MEM_MB = 3500  # the docs: 2 vCPU and 4 GB are enough for a single user

PROBE_SCRIPT = f"""
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
echo "uid=$(id -u)"
echo "user=$(id -un)"
if [ "$(id -u)" = 0 ] || sudo -n true 2>/dev/null; then echo "root=1"; else echo "root=0"; fi
if [ -r /etc/os-release ]; then . /etc/os-release; echo "os_id=$ID"; echo "os_version=$VERSION_ID"; fi
command -v apt-get >/dev/null 2>&1 && echo "apt=1" || echo "apt=0"
command -v systemctl >/dev/null 2>&1 && echo "systemd=1" || echo "systemd=0"
echo "node=$(node --version 2>/dev/null)"
echo "uv=$(uv --version 2>/dev/null)"
echo "canvas=$(agent-canvas --version 2>/dev/null | tail -n 1)"
echo "service=$(systemctl is-active {SERVICE} 2>/dev/null)"
echo "http=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://127.0.0.1:{PORT}/ 2>/dev/null)"
echo "mem_mb=$(awk '/^MemTotal:/ {{print int($2/1024)}}' /proc/meminfo 2>/dev/null)"
echo "free_mb=$(df -Pm / 2>/dev/null | awk 'NR==2 {{print $4}}')"
"""


@dataclass
class ServerState:
    uid: str = ""
    user: str = ""
    root: bool = False
    os_id: str = ""
    os_version: str = ""
    apt: bool = False
    systemd: bool = False
    node: str = ""
    uv: str = ""
    canvas: str = ""
    service: str = ""
    http: str = ""
    mem_mb: int | None = None
    free_mb: int | None = None
    raw: dict = field(default_factory=dict)

    @property
    def node_major(self) -> int:
        m = re.match(r"v?(\d+)", self.node)
        return int(m.group(1)) if m else 0

    @property
    def canvas_version(self) -> str:
        m = re.search(r"\d+\.\d+\.\d+", self.canvas)
        return m.group(0) if m else ""

    @property
    def answers(self) -> bool:
        return self.http.startswith(("2", "3"))


def parse_probe(output: str) -> ServerState:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()

    def number(key: str) -> int | None:
        return int(values[key]) if values.get(key, "").isdigit() else None

    return ServerState(
        uid=values.get("uid", ""), user=values.get("user", ""), root=values.get("root") == "1",
        os_id=values.get("os_id", ""), os_version=values.get("os_version", ""), apt=values.get("apt") == "1",
        systemd=values.get("systemd") == "1", node=values.get("node", ""), uv=values.get("uv", ""), canvas=values.get("canvas", ""),
        service=values.get("service", ""), http=values.get("http", ""), mem_mb=number("mem_mb"), free_mb=number("free_mb"), raw=values,
    )


def describe(state: ServerState) -> tuple[str, str]:
    """(status, a sentence for the user) for what a probe found."""
    where = f"{state.user}@{state.os_id or 'host'} {state.os_version}".strip()
    if not state.root:
        return "error", f"Нужны права root: войдите как root или дайте пользователю sudo без пароля ({where})"
    if state.answers and state.canvas_version:
        note = f" · памяти {state.mem_mb} МБ — мало, нужно от 4 ГБ" if state.mem_mb and state.mem_mb < MIN_MEM_MB else ""
        return "online", f"OpenHands {state.canvas_version} работает · {where}{note}"
    if state.canvas_version:
        return "offline", f"OpenHands {state.canvas_version} установлен, но не отвечает (служба: {state.service or 'нет'}) — нажмите «Установить», чтобы перезапустить"
    return "offline", f"OpenHands не установлен · {where} — нажмите «Установить»"


def is_configured(server: AgentServer) -> bool:
    return bool(server.ssh_host and server.ssh_user and (server.ssh_key_enc or server.ssh_password_enc))


def target_for(server: AgentServer) -> SshTarget:
    """Connection details with the secrets decrypted. Worker/API only; never log or serialise the result."""
    if not is_configured(server):
        raise SshError(SshErrorKind.other, "Не заданы адрес, пользователь или ключ/пароль сервера")
    return SshTarget(
        host=server.ssh_host, port=server.ssh_port or 22, user=server.ssh_user,
        private_key=decrypt(server.ssh_key_enc) if server.ssh_key_enc else None, host_key=server.ssh_host_key,
        password=decrypt(server.ssh_password_enc) if server.ssh_password_enc else None,
    )


@dataclass
class CheckResult:
    status: str
    detail: str
    pending_host_key: str | None = None
    state: ServerState | None = None


def check(server: AgentServer) -> CheckResult:
    """Look at the server: its host key on first contact, then what is installed and whether OpenHands answers. Never raises."""
    if not is_configured(server):
        return CheckResult("new", "Укажите адрес сервера, пользователя и ключ или пароль")
    port = server.ssh_port or 22
    if not server.ssh_host_key:
        try:
            line = fetch_host_key(server.ssh_host, port)
            shown = fingerprint(line)
        except SshError as exc:
            return CheckResult("offline", exc.message)
        return CheckResult(
            "new",
            f"Подтвердите ключ сервера: {shown}. Сравните с выводом `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` на сервере.",
            pending_host_key=line,
        )
    try:
        with SshConnection(target_for(server)) as conn:
            done = conn.run("sh -s", input=PROBE_SCRIPT, timeout=60)
    except SshError as exc:
        pending = None
        message = exc.message
        if exc.kind == SshErrorKind.host_key_changed:
            try:
                pending = fetch_host_key(server.ssh_host, port)
                message += f" Сейчас сервер показывает {fingerprint(pending)}, подтверждён {fingerprint(server.ssh_host_key)}."
            except SshError:
                pass
        status = "error" if exc.kind in (SshErrorKind.auth, SshErrorKind.host_key_changed) else "offline"
        return CheckResult(status, message, pending_host_key=pending)
    state = parse_probe(done.stdout)
    status, detail = describe(state)
    return CheckResult(status, detail, state=state)


# ── installation ────────────────────────────────────────────────────────────

PRELUDE = """
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$PATH"
export DEBIAN_FRONTEND=noninteractive
SUDO=""
if [ "$(id -u)" != 0 ]; then SUDO="sudo -n"; fi
set -e
"""


@dataclass(frozen=True)
class Step:
    title: str
    script: str


def plan(state: ServerState, version: str, api_key: str) -> list[Step]:
    """The steps that bring this server to "OpenHands answers on 127.0.0.1:8000". Raises ValueError when it cannot be done here."""
    if not state.root:
        raise ValueError("Нужны права root: войдите как root или дайте пользователю sudo без пароля")
    if not state.systemd:
        raise ValueError("На сервере нет systemd — поставьте Ubuntu или Debian")
    steps: list[Step] = []
    if not state.apt and (state.node_major < NODE_MAJOR or not state.uv):
        raise ValueError(f"Автоматическая установка умеет только Ubuntu/Debian (apt). Поставьте Node.js {NODE_MAJOR}+ и uv сами и нажмите «Установить» ещё раз")
    steps.append(Step("Базовые пакеты (curl, git, ca-certificates)", """
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y --no-install-recommends ca-certificates curl gnupg git
fi
"""))
    if state.node_major < NODE_MAJOR:
        steps.append(Step(f"Node.js {NODE_MAJOR}", f"""
curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | $SUDO bash -
$SUDO apt-get install -y nodejs
node --version
"""))
    if not state.uv:
        steps.append(Step("uv (запускает сервер агента)", """
curl -LsSf https://astral.sh/uv/install.sh | $SUDO env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
uv --version
"""))
    steps.append(Step(f"OpenHands (Agent Canvas {version})", f"""
$SUDO npm install -g --no-fund --no-audit @openhands/agent-canvas@{version}
agent-canvas --version
"""))
    # The key goes into the script's text, which travels on stdin — never on a command line other users of the host could see.
    steps.append(Step("Служба oreshnik-openhands (от root, только 127.0.0.1)", f"""
umask 077
old_secret=""
if $SUDO test -f {ENV_FILE}; then old_secret=$($SUDO sed -n 's/^OH_SECRET_KEY=//p' {ENV_FILE} | head -n 1); fi
if [ -z "$old_secret" ]; then old_secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \\n'); fi
printf 'LOCAL_BACKEND_API_KEY=%s\\nOH_SECRET_KEY=%s\\nOH_BIND_HOST=127.0.0.1\\nHOME=/root\\n' '{api_key}' "$old_secret" | $SUDO tee {ENV_FILE} >/dev/null
$SUDO chmod 600 {ENV_FILE}
canvas=$(command -v agent-canvas)
$SUDO tee /etc/systemd/system/{SERVICE}.service >/dev/null <<UNIT
[Unit]
Description=OpenHands Agent Canvas for Oreshnik
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/root
EnvironmentFile={ENV_FILE}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/root/.local/bin
ExecStart=$canvas --port {PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable {SERVICE} >/dev/null 2>&1
$SUDO systemctl restart {SERVICE}
"""))
    # The first start downloads the agent server with uvx: it can take minutes on a slow link.
    steps.append(Step("Запуск OpenHands (первый раз скачивает сервер агента — до 10 минут)", f"""
set +e
for i in $(seq 1 120); do
  code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://127.0.0.1:{PORT}/)
  case "$code" in 2*|3*) echo "OpenHands отвечает (HTTP $code)"; exit 0 ;; esac
  sleep 5
done
echo "OpenHands не ответил за 10 минут. Последние строки журнала службы:"
$SUDO journalctl -u {SERVICE} -n 40 --no-pager
exit 1
"""))
    return steps


@dataclass
class InstallOutcome:
    status: str
    detail: str
    api_key_enc: str | None = None
    version: str | None = None


def install(server: AgentServer, emit: Callable[[str], None], version: str | None = None) -> InstallOutcome:
    """Install (or repair, or upgrade) OpenHands on the server. Never raises: the outcome and the log say what happened."""
    version = version or settings.openhands_version
    api_key = secrets.token_urlsafe(32)
    try:
        target = target_for(server)
        with SshConnection(target) as conn:
            state = parse_probe(conn.run("sh -s", input=PROBE_SCRIPT, timeout=60).stdout)
    except SshError as exc:
        emit(f"✗ Не удалось подключиться: {exc.message}")
        return InstallOutcome("offline", exc.message)
    emit(f"Сервер: {state.user}@{state.os_id or 'unknown'} {state.os_version}, памяти {state.mem_mb or '?'} МБ, свободно на диске {state.free_mb or '?'} МБ".strip())
    try:
        steps = plan(state, version, api_key)
    except ValueError as exc:
        emit(f"✗ {exc}")
        return InstallOutcome("error", str(exc))
    for number, step in enumerate(steps, 1):
        emit(f"▶ Шаг {number}/{len(steps)}: {step.title}")
        try:
            with SshConnection(target) as conn:
                code = _stream(conn, PRELUDE + step.script, lambda line: emit(line.replace(api_key, "***")))
        except SshError as exc:
            emit(f"✗ {exc.message}")
            return InstallOutcome("offline", f"Установка остановилась на шаге «{step.title}»: {exc.message}")
        if code != 0:
            emit(f"✗ Шаг завершился с кодом {code}" if code is not None else "✗ Шаг не завершился")
            return InstallOutcome("error", f"Установка остановилась на шаге «{step.title}» — подробности в журнале")
        emit("✓ Готово")
    try:
        with SshConnection(target) as conn:
            after = parse_probe(conn.run("sh -s", input=PROBE_SCRIPT, timeout=60).stdout)
    except SshError as exc:
        emit(f"✗ Не удалось проверить результат: {exc.message}")
        return InstallOutcome("offline", exc.message, api_key_enc=encrypt(api_key))
    status, detail = describe(after)
    emit(("✓ " if status == "online" else "✗ ") + detail)
    return InstallOutcome(status, detail, api_key_enc=encrypt(api_key), version=after.canvas_version or None)
