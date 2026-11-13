"""What the panel knows about an SSH runner: its connection details, and whether it is ready to run scans.

A host is *ready* when a non-interactive login can use Docker and has Strix on its PATH (plus python3 and setsid, which the
panel itself relies on to mirror a running scan and to stop it). Everything else is a reason why not, phrased for the person
who has to fix it.
"""

import re
from dataclasses import dataclass, field

from app.core.crypto import decrypt
from app.models.runner import Runner, RunnerStatus
from app.services.ssh import SshConnection, SshError, SshErrorKind, SshTarget, fetch_host_key, fingerprint

# Non-interactive SSH sessions do not read the profile, so tools installed for the user (uv tool, pipx) would not be found.
REMOTE_PATH = 'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"'

PROBE_SCRIPT = (
    REMOTE_PATH
    + r"""
os_id=$( (. /etc/os-release 2>/dev/null && echo "$ID") || true)
os_ver=$( (. /etc/os-release 2>/dev/null && echo "$VERSION_ID") || true)
echo "os=$os_id"
echo "os_version=$os_ver"
echo "user=$(id -un)"
echo "uid=$(id -u)"
if command -v docker >/dev/null 2>&1; then echo docker_cli=1; else echo docker_cli=0; fi
if docker info >/dev/null 2>&1; then echo docker_ok=1; else echo docker_ok=0; fi
s=$(command -v strix 2>/dev/null); echo "strix=$s"
if [ -n "$s" ]; then
  if command -v timeout >/dev/null 2>&1; then v=$(timeout 30 strix --version 2>&1 | head -n1); else v=$(strix --version 2>&1 | head -n1); fi
  echo "strix_version=$v"
fi
echo "python3=$(command -v python3 2>/dev/null)"
echo "setsid=$(command -v setsid 2>/dev/null)"
echo "curl=$(command -v curl 2>/dev/null)"
echo "uv=$(command -v uv 2>/dev/null)"
if [ "$(id -u)" = 0 ] || sudo -n true >/dev/null 2>&1; then echo can_root=1; else echo can_root=0; fi
echo "free_mb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print int($4/1024)}')"
"""
)

MIN_FREE_MB = 2048
PROVISION_STALE_MINUTES = 90  # an installation that has not ended by then was lost with its worker


@dataclass
class RemoteState:
    os_id: str = ""
    os_version: str = ""
    user: str = ""
    uid: int | None = None
    docker_cli: bool = False
    docker_ok: bool = False
    strix_path: str = ""
    strix_version: str = ""
    python3: bool = False
    setsid: bool = False
    curl: bool = False
    uv: bool = False
    can_root: bool = False
    free_mb: int | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def apt_based(self) -> bool:
        return self.os_id in ("ubuntu", "debian")

    def missing(self) -> list[str]:
        """What is needed to run scans but not there (or not usable)."""
        out = []
        if not self.docker_ok:
            out.append("доступ к Docker" if self.docker_cli else "Docker")
        if not self.strix_path:
            out.append("Strix")
        if not self.python3:
            out.append("python3")
        if not self.setsid:
            out.append("setsid")
        return out


def is_provisioning(runner: Runner) -> bool:
    """True while an installation is genuinely running (not a status left behind by a worker that died)."""
    from datetime import datetime, timedelta, timezone

    if runner.status != RunnerStatus.provisioning:
        return False
    started = runner.provision_started_at
    return started is not None and datetime.now(timezone.utc) - started < timedelta(minutes=PROVISION_STALE_MINUTES)


def parse_probe(output: str) -> RemoteState:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isidentifier():
            values[key] = value.strip()
    state = RemoteState(raw=values)
    state.os_id = values.get("os", "").lower()
    state.os_version = values.get("os_version", "")
    state.user = values.get("user", "")
    state.uid = int(values["uid"]) if values.get("uid", "").isdigit() else None
    state.docker_cli = values.get("docker_cli") == "1"
    state.docker_ok = values.get("docker_ok") == "1"
    state.strix_path = values.get("strix", "")
    state.strix_version = values.get("strix_version", "")
    state.python3 = bool(values.get("python3"))
    state.setsid = bool(values.get("setsid"))
    state.curl = bool(values.get("curl"))
    state.uv = bool(values.get("uv"))
    state.can_root = values.get("can_root") == "1"
    state.free_mb = int(values["free_mb"]) if values.get("free_mb", "").isdigit() else None
    return state


def probe(conn: SshConnection) -> RemoteState:
    result = conn.run("sh -s", input=PROBE_SCRIPT, timeout=90)
    return parse_probe(result.stdout)


def is_configured(runner: Runner) -> bool:
    """Host, user and at least one way to log in (a key and/or a password)."""
    return bool(runner.ssh_host and runner.ssh_user and (runner.ssh_key_enc or runner.ssh_password_enc))


def target_for(runner: Runner) -> SshTarget:
    """Connection details with the secrets decrypted. Only for use inside the worker; never log or serialise the result."""
    if not is_configured(runner):
        raise SshError(SshErrorKind.other, "У раннера не заданы хост, пользователь или ключ/пароль")
    return SshTarget(
        host=runner.ssh_host,
        port=runner.ssh_port or 22,
        user=runner.ssh_user,
        private_key=decrypt(runner.ssh_key_enc) if runner.ssh_key_enc else None,
        host_key=runner.ssh_host_key,
        password=decrypt(runner.ssh_password_enc) if runner.ssh_password_enc else None,
    )


@dataclass
class CheckResult:
    status: RunnerStatus
    detail: str
    # A host key that was seen and still needs to be confirmed (None: nothing to confirm / unchanged).
    pending_host_key: str | None = None
    state: RemoteState | None = None


def describe_state(state: RemoteState) -> tuple[RunnerStatus, str]:
    missing = state.missing()
    if missing:
        # Strix is installed for the user; everything else (Docker, python3, setsid) needs root.
        needs_root = [m for m in missing if m != "Strix"]
        if needs_root and not state.can_root:
            hint = "нужны права root или sudo без пароля — дайте их пользователю или установите вручную"
            if state.docker_cli and not state.docker_ok:
                hint = "добавьте пользователя в группу docker и переподключитесь, либо дайте ему root / sudo без пароля"
        else:
            hint = "нажмите «Установить»"
        return RunnerStatus.offline, f"Не готов: нет {', '.join(missing)} — {hint}"
    version = re.search(r"\d+\.\d+(?:\.\d+)*", state.strix_version)
    bits = [f"Strix {version.group(0)}" if version else "Strix", "Docker"]
    note = ""
    if state.free_mb is not None and state.free_mb < MIN_FREE_MB:
        note = f" · мало места: {state.free_mb} МБ свободно"
    return RunnerStatus.online, f"{' + '.join(bits)} доступны · {state.user}@{state.os_id or 'host'}{note}"


def check_ssh_runner(runner: Runner) -> CheckResult:
    """Check an SSH runner end to end. Never raises: every failure becomes a status and a reason."""
    if not is_configured(runner):
        return CheckResult(RunnerStatus.offline, "Не заданы хост, пользователь или ключ/пароль")
    port = runner.ssh_port or 22

    if not runner.ssh_host_key:
        # First contact: look at the host's key (no credentials are sent) and ask for confirmation.
        try:
            line = fetch_host_key(runner.ssh_host, port)
            fp = fingerprint(line)
        except SshError as exc:
            return CheckResult(RunnerStatus.offline, exc.message)
        return CheckResult(
            RunnerStatus.offline,
            f"Подтвердите ключ хоста: {fp}. Сравните с выводом `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` на сервере.",
            pending_host_key=line,
        )

    try:
        target = target_for(runner)
        with SshConnection(target) as conn:
            state = probe(conn)
    except SshError as exc:
        pending = None
        message = exc.message
        status = RunnerStatus.error if exc.kind in (SshErrorKind.auth, SshErrorKind.host_key_changed) else RunnerStatus.offline
        if exc.kind == SshErrorKind.host_key_changed:
            try:  # show what the host presents now, so the administrator can decide whether it is legitimate
                seen = fetch_host_key(runner.ssh_host, port)
                pending = seen
                message += f" Сейчас хост показывает {fingerprint(seen)}, подтверждён {fingerprint(runner.ssh_host_key)}."
            except SshError:
                pass
        return CheckResult(status, message, pending_host_key=pending)
    status, detail = describe_state(state)
    return CheckResult(status, detail, state=state)
