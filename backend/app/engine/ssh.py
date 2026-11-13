"""SshRunner: runs the real `strix` CLI on a remote host over SSH.

Everything the panel builds on stays on the panel's side: the scan's directory under STRIX_RUNS_DIR is still the source of
truth, the log is still `oreshnik.log`, results are still read from `scan-<id>/strix_runs/<name>/`. This runner only moves
Strix itself to another machine:

    1. connect (host key pinned), clear any leftover of an earlier attempt, create ~/oreshnik/scan-<id>
    2. for a continuation, upload the run directory (the panel's copy is the master)
    3. start Strix in its own session (`setsid`), the script and the LLM environment arriving on *stdin* — so secrets
       are never part of a command line (visible to every user of the host) and never touch the remote disk
    4. stream its output; while it runs, mirror run.json / agents / findings to the panel every couple of seconds, so the
       live cost, the agent feed and the budget bar work exactly as for a local scan
    5. cancel / time-out the way LocalRunner does (SIGINT, then SIGTERM, then SIGKILL — to the whole process group)
    6. at the end fetch the run directory (size-capped, symlinks skipped), remove the containers of the scan and delete the
       remote scan directory. If fetching fails the remote directory is kept and the scan fails with a clear message.

If the connection drops, the run is lost: Strix is stopped on the host (best effort, over a fresh connection) and the scan
fails — the same outcome as a worker that dies under a local scan.
"""

import logging
import os
import threading
import time
from collections.abc import Callable

from app.core.config import settings
from app.engine.base import RunResult, ScanSpec, strix_privacy_env, strix_reasoning_env
from app.engine.local import strix_argv
from app.services.resume import RUN_NAME_RE
from app.services.scan_files import INSTRUCTION_NAME
from app.services.ssh import SshConnection, SshError, SshErrorKind, SshTarget, q
from app.services.ssh_runner import REMOTE_PATH

log = logging.getLogger("oreshnik.ssh_runner")

REMOTE_ROOT = "oreshnik"
SIGNAL_NUMBERS = {"INT": 2, "TERM": 15, "KILL": 9, "HUP": 1, "QUIT": 3, "ABRT": 6, "SEGV": 11, "PIPE": 13}

# agents.db is a live SQLite database in WAL mode: copying the file alone would give a stale or torn picture. The host's own
# python3 takes a consistent snapshot with the backup API, and only that snapshot is downloaded.
SNAPSHOT_PY = (
    "import sqlite3,sys\n"
    "src,dst=sys.argv[1],sys.argv[2]\n"
    "try:\n"
    "    a=sqlite3.connect('file:'+src+'?mode=ro',uri=True,timeout=2)\n"
    "except sqlite3.Error:\n"
    "    a=sqlite3.connect(src,timeout=2)\n"
    "b=sqlite3.connect(dst)\n"
    "a.backup(b)\n"
    "b.close()\n"
    "a.close()\n"
)
SNAPSHOT_NAME = "agents.snapshot.db"

# What the panel mirrors while a scan runs (small files, rewritten by Strix as it goes).
LIVE_FILES = ("run.json", "vulnerabilities.json", ".state/agents.json")


def remote_dir(scan_id: int) -> str:
    return f"{REMOTE_ROOT}/scan-{int(scan_id)}"


def build_script(spec: ScanSpec, argv: list[str], scan_remote_dir: str) -> str:
    """The shell script fed to `sh -s` on the host. Every dynamic word is quoted; the working directory and the run name
    are built from an integer id and a validated name."""
    lines = [
        REMOTE_PATH,
        f'cd "$HOME/{scan_remote_dir}" || exit 97',
        "mkdir -p home",
        'export HOME="$PWD/home"',  # a private HOME per scan, as for a local scan
        f"export STRIX_RUN_ID={q(spec.run_label)}",
        "export TERM=dumb NO_COLOR=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8",
    ]
    for key, value in spec.env.items():
        if not key.isidentifier():
            raise SshError(SshErrorKind.other, "Недопустимое имя переменной окружения")
        lines.append(f"export {key}={q(value)}")
    # after the scan's own variables: they cannot switch these back on, or override the panel's own tuning
    for key, value in (strix_privacy_env() | strix_reasoning_env()).items():
        lines.append(f"export {key}={q(value)}")
    lines += [
        "echo $$ > .pid",  # this shell becomes Strix (exec) and is the session leader: .pid is the process group to signal
        "exec " + " ".join(q(a) for a in argv) + " </dev/null",
    ]
    return "\n".join(lines) + "\n"


def _signal_group(conn: SshConnection, scan_remote_dir: str, name: str) -> None:
    """Send a signal to Strix's whole process group on the host. Best effort."""
    try:
        conn.run(
            f'p=$(cat "$HOME/{scan_remote_dir}/.pid" 2>/dev/null) && [ -n "$p" ] && kill -s {name} -- "-$p" 2>/dev/null; true',
            timeout=20,
        )
    except SshError as exc:
        log.warning("could not send %s to the remote scan: %s", name, exc.message)


def _remove_containers(conn: SshConnection, label: str) -> None:
    try:
        conn.run(
            f'{REMOTE_PATH}\nids=$(docker ps -aq --filter {q("label=strix-run-id=" + label)} 2>/dev/null); '
            '[ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1; true',
            timeout=60,
        )
    except SshError as exc:
        log.warning("could not remove the sandbox containers on the host: %s", exc.message)


def cleanup_remote(target: SshTarget, scan_id: int, label: str) -> bool:
    """Stop whatever is left of a scan on its host (the worker that ran it is gone, or lost the connection). Never raises."""
    try:
        with SshConnection(target) as conn:
            _signal_group(conn, remote_dir(scan_id), "TERM")
            time.sleep(1)
            _signal_group(conn, remote_dir(scan_id), "KILL")
            _remove_containers(conn, label)
        return True
    except SshError as exc:
        log.warning("remote cleanup of scan %s failed: %s", scan_id, exc.message)
        return False
    except Exception:
        log.exception("remote cleanup of scan %s crashed", scan_id)
        return False


class _Mirror(threading.Thread):
    """Copies the files of a running scan from the host to the panel, so everything that reads the local run directory
    (live cost, the agent feed) sees the scan as it goes. It must never break a scan: errors are logged and dropped."""

    def __init__(self, conn: SshConnection, spec: ScanSpec, scan_remote_dir: str):
        super().__init__(name=f"oreshnik-mirror-{spec.scan_id}", daemon=True)
        self.conn, self.spec, self.remote = conn, spec, scan_remote_dir
        self._stop_event = threading.Event()
        self.run_name: str | None = spec.resume_run

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=30)

    def run(self) -> None:
        while not self._stop_event.wait(settings.ssh_sync_interval_seconds):
            try:
                sync_run_files(self.conn, self.spec, self.remote, self)
            except Exception as exc:
                log.debug("mirror of scan %s skipped a beat: %s", self.spec.scan_id, exc)


def _run_name(conn: SshConnection, scan_remote_dir: str, hint: str | None) -> str | None:
    if hint and RUN_NAME_RE.match(hint):
        return hint
    names = [n for n in conn.list_dir(f"{scan_remote_dir}/strix_runs") if RUN_NAME_RE.match(n)]
    return names[0] if names else None


def _snapshot_agents_db(conn: SshConnection, remote_run: str) -> bool:
    """Ask the host's python3 for a consistent copy of the agents' database. Paths are relative to $HOME and made only of
    an integer id and a validated run name, so quoting them is enough."""
    src = f"{remote_run}/.state/agents.db"
    dst = f"{remote_run}/.state/{SNAPSHOT_NAME}"
    done = conn.run(f'{REMOTE_PATH}\ncd "$HOME" && python3 -c {q(SNAPSHOT_PY)} {q(src)} {q(dst)}', timeout=60)
    return done.status == 0


def sync_run_files(conn: SshConnection, spec: ScanSpec, scan_remote_dir: str, state: "_Mirror | None" = None) -> None:
    """One mirroring pass: the small live files plus a consistent snapshot of the agents' database."""
    name = _run_name(conn, scan_remote_dir, state.run_name if state else spec.resume_run)
    if name is None:
        return
    if state is not None:
        state.run_name = name
    remote_run = f"{scan_remote_dir}/strix_runs/{name}"
    local_run = spec.work_dir / "strix_runs" / name
    # run.json first: it is what makes the run directory "exist" for the panel
    for rel in LIVE_FILES:
        conn.get_file(f"{remote_run}/{rel}", local_run / rel)
    if conn.exists(f"{remote_run}/.state/agents.db") and _snapshot_agents_db(conn, remote_run):
        db = local_run / ".state" / "agents.db"
        if conn.get_file(f"{remote_run}/.state/{SNAPSHOT_NAME}", db.with_name(SNAPSHOT_NAME)):
            for stale in ("agents.db-wal", "agents.db-shm"):  # a leftover journal would be applied to the new snapshot
                (db.parent / stale).unlink(missing_ok=True)
            os.replace(db.with_name(SNAPSHOT_NAME), db)


def fetch_final(conn: SshConnection, spec: ScanSpec, scan_remote_dir: str) -> None:
    """Bring the whole run directory home. Raises SshError if it cannot be done (the caller keeps the remote copy)."""
    for name in [n for n in conn.list_dir(f"{scan_remote_dir}/strix_runs") if RUN_NAME_RE.match(n)]:
        remote_run = f"{scan_remote_dir}/strix_runs/{name}"
        local_run = spec.work_dir / "strix_runs" / name
        snapshot = conn.exists(f"{remote_run}/.state/agents.db") and _snapshot_agents_db(conn, remote_run)

        def skip(rel: str) -> bool:
            if rel == f".state/{SNAPSHOT_NAME}":
                return False
            return snapshot and rel.startswith(".state/agents.db")  # the raw live database is replaced by the snapshot

        conn.get_tree(remote_run, local_run, skip=skip)
        if snapshot:
            snap = local_run / ".state" / SNAPSHOT_NAME
            if snap.is_file():
                for stale in ("agents.db-wal", "agents.db-shm"):
                    (local_run / ".state" / stale).unlink(missing_ok=True)
                os.replace(snap, local_run / ".state" / "agents.db")


class SshRunner:
    # ── execution ────────────────────────────────────────────────────────

    def run(
        self,
        spec: ScanSpec,
        on_line: Callable[[str], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        target: SshTarget | None = spec.ssh
        if target is None:
            return RunResult(error="Нет данных для подключения к раннеру")
        try:
            conn = SshConnection(target).open()
        except SshError as exc:
            return RunResult(error=exc.message)
        try:
            return self._run(conn, target, spec, on_line, should_cancel)
        except SshError as exc:
            return RunResult(error=exc.message)
        finally:
            conn.close()

    def _run(
        self,
        conn: SshConnection,
        target: SshTarget,
        spec: ScanSpec,
        on_line: Callable[[str], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        scan_dir_remote = remote_dir(spec.scan_id)
        on_line(f"[oreshnik] Подключено к {target.user}@{target.host}:{target.port} (ключ хоста подтверждён)")
        if spec.resume_run and not RUN_NAME_RE.match(spec.resume_run):
            return RunResult(error="Недопустимое имя прогона")

        # Start clean: stop and remove what an earlier attempt of this scan may have left on the host.
        _signal_group(conn, scan_dir_remote, "KILL")
        conn.remove_tree(scan_dir_remote)
        conn.makedirs(scan_dir_remote)

        if spec.resume_run:
            local_run = spec.work_dir / "strix_runs" / spec.resume_run
            if not local_run.is_dir():
                return RunResult(error="Каталог продолжаемого прогона не найден на стороне панели")
            files = conn.put_tree(local_run, f"{scan_dir_remote}/strix_runs/{spec.resume_run}")
            on_line(f"[oreshnik] Прогон {spec.resume_run} передан на хост ({files} файл.)")
        if spec.instruction:
            conn.write_file(f"{scan_dir_remote}/{INSTRUCTION_NAME}", spec.instruction.encode("utf-8"), 0o600)

        argv = strix_argv(spec, "strix")
        script = build_script(spec, argv, scan_dir_remote)
        on_line("$ " + " ".join(q(a) for a in argv))  # the command, like for a local scan; the environment is not shown
        proc = conn.start("setsid -w sh -s", input=script)

        mirror = _Mirror(conn, spec, scan_dir_remote)
        mirror.start()
        result = RunResult()
        deadline = time.monotonic() + spec.timeout_seconds if spec.timeout_seconds else None
        stop_sent_at: float | None = None
        escalation = 0  # 0 = nothing sent, 1 = SIGINT, 2 = SIGTERM, 3 = SIGKILL
        grace = max(1, settings.strix_cancel_grace_seconds)

        try:
            while True:
                for line in proc.lines(0.2):
                    on_line(line)
                if proc.finished:
                    break
                if proc.error is not None and proc.exit_status is None:
                    break  # the connection is gone
                now = time.monotonic()
                if stop_sent_at is None:
                    if should_cancel():
                        result.cancelled = True
                    elif deadline is not None and now > deadline:
                        result.timed_out = True
                    if result.cancelled or result.timed_out:
                        _signal_group(conn, scan_dir_remote, "INT")
                        escalation, stop_sent_at = 1, now
                else:
                    waited = now - stop_sent_at
                    if escalation == 1 and waited > grace:
                        _signal_group(conn, scan_dir_remote, "TERM")
                        escalation = 2
                    elif escalation == 2 and waited > grace * 2:
                        _signal_group(conn, scan_dir_remote, "KILL")
                        escalation = 3
        finally:
            mirror.stop()

        if proc.exit_status is None and proc.error is not None:
            # The connection broke under a running scan. Stop Strix on the host over a fresh connection.
            result.error = "Потеряно соединение с раннером во время скана: " + proc.error.message
            conn.close()
            cleanup_remote(target, spec.scan_id, spec.run_label)
            return result

        if proc.exit_status is not None:
            result.exit_code = proc.exit_status
        elif proc.exit_signal:
            result.exit_code = 128 + SIGNAL_NUMBERS.get(proc.exit_signal, 0)

        # Collect the results, remove the sandbox containers, and — only if everything came home — the remote directory.
        _remove_containers(conn, spec.run_label)
        try:
            fetch_final(conn, spec, scan_dir_remote)
        except SshError as exc:
            keep = f"~/{scan_dir_remote}"
            on_line(f"[oreshnik] Не удалось забрать результаты с хоста: {exc.message}. Файлы остались в {keep}")
            if not (result.cancelled or result.timed_out) and result.error is None:
                result.error = f"Не удалось забрать результаты с хоста: {exc.message}"
            return result
        try:
            conn.remove_tree(scan_dir_remote)
        except SshError as exc:
            log.warning("could not remove %s on the host: %s", scan_dir_remote, exc.message)
        return result
