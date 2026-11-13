"""SSH access to remote runners.

asyncssh does the protocol work; this module gives the (synchronous) Celery worker a plain blocking interface on top of it.
Each connection owns a small event-loop thread, so several threads of one scan (the run itself, the mirroring of its files,
a cancel request) can share one connection safely.

Security rules that live here, not in the callers:

* **The host key is always pinned.** A connection is only opened against a key an administrator has confirmed; a different key
  is an error ("host key changed"), never a prompt and never an automatic update. The only thing allowed without a pinned key
  is *fetching* the key to show its fingerprint (`fetch_host_key`), which authenticates nothing and sends no credentials.
* Key authentication only: no passwords, no SSH agent, no ~/.ssh/config of the machine the panel runs on.
* Secrets (the private key, anything passed as `input`) are never part of an error message.
* What is downloaded from a remote host is untrusted: names are validated, symlinks are skipped, and the total size and the
  number of files are capped.
"""

import asyncio
import concurrent.futures
import logging
import os
import posixpath
import queue
import shlex
import socket
import stat as statmod
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import asyncssh

from app.core.config import settings

log = logging.getLogger("oreshnik.ssh")


class SshErrorKind(str, Enum):
    unreachable = "unreachable"          # no route, refused, timeout
    auth = "auth"                        # the host rejected the key / the key is unusable
    host_key_untrusted = "host_key_untrusted"  # no pinned key yet
    host_key_changed = "host_key_changed"      # the host shows a different key than the pinned one
    protocol = "protocol"                # the connection broke or the server misbehaved
    other = "other"


class SshError(Exception):
    """A failure with a user-facing (Russian) message. Never contains secrets."""

    def __init__(self, kind: SshErrorKind, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    user: str
    private_key: str = field(repr=False)
    host_key: str | None = None  # the pinned OpenSSH public key line; required to connect


@dataclass
class CommandResult:
    status: int | None
    stdout: str
    stderr: str


def fingerprint(key_line: str) -> str:
    """`SHA256:…` of an OpenSSH public key line, the way `ssh-keygen -lf` prints it."""
    try:
        return asyncssh.import_public_key(key_line).get_fingerprint("sha256")
    except Exception as exc:
        raise SshError(SshErrorKind.other, "Некорректный публичный ключ хоста") from exc


def key_algorithm(key_line: str) -> str:
    return key_line.split(None, 1)[0] if key_line.strip() else ""


def _kind_of(exc: BaseException) -> SshError:
    """Translate whatever the library raised into one of our errors."""
    if isinstance(exc, SshError):
        return exc
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return SshError(
            SshErrorKind.host_key_changed,
            "Ключ хоста не совпадает с подтверждённым. Если сервер не переустанавливали — это может быть подмена: "
            "не подтверждайте новый ключ, пока не выясните причину.",
        )
    if isinstance(exc, asyncssh.PermissionDenied):
        return SshError(SshErrorKind.auth, "Хост не принял ключ: проверьте пользователя и то, что публичный ключ есть в authorized_keys")
    if isinstance(exc, (asyncssh.KeyImportError, asyncssh.KeyEncryptionError)):
        return SshError(SshErrorKind.auth, "Не удалось прочитать приватный ключ (нужен ключ без пароля в формате PEM/OpenSSH)")
    if isinstance(exc, (asyncio.TimeoutError, concurrent.futures.TimeoutError, TimeoutError)):
        return SshError(SshErrorKind.unreachable, "Хост не ответил вовремя")
    if isinstance(exc, socket.gaierror):
        return SshError(SshErrorKind.unreachable, "Не удалось найти хост (проверьте адрес)")
    if isinstance(exc, ConnectionRefusedError):
        return SshError(SshErrorKind.unreachable, "Соединение отклонено: на этом порту нет SSH-сервера")
    if isinstance(exc, (asyncssh.ConnectionLost, asyncssh.DisconnectError, BrokenPipeError, ConnectionResetError, EOFError)):
        return SshError(SshErrorKind.protocol, "Соединение с хостом оборвано")
    if isinstance(exc, OSError):
        return SshError(SshErrorKind.unreachable, f"Хост недоступен ({exc.__class__.__name__})")
    if isinstance(exc, asyncssh.Error):
        return SshError(SshErrorKind.protocol, f"Ошибка SSH ({exc.__class__.__name__})")
    return SshError(SshErrorKind.other, f"Ошибка при работе по SSH ({exc.__class__.__name__})")


class _LoopThread:
    """An asyncio loop on its own thread, driven from synchronous code."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="oreshnik-ssh", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except BaseException as exc:
            future.cancel()
            raise _kind_of(exc) from exc

    def close(self) -> None:
        async def _cancel_all() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            if self.loop.is_running():
                asyncio.run_coroutine_threadsafe(_cancel_all(), self.loop).result(5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self.loop.is_running():
            self.loop.close()


async def _fetch_host_key(host: str, port: int, timeout: float) -> str:
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port, config=None), timeout)
    if key is None:
        raise SshError(SshErrorKind.protocol, "Хост не показал ключ")
    return key.export_public_key().decode().strip()


def fetch_host_key(host: str, port: int, timeout: float | None = None) -> str:
    """The host's public key as an OpenSSH line. Authenticates nothing and sends no credentials: it only lets the
    administrator compare the fingerprint before trusting the host."""
    timeout = timeout or settings.ssh_connect_timeout_seconds
    loop = _LoopThread()
    try:
        return loop.call(_fetch_host_key(host, port, timeout), timeout + 5)
    finally:
        loop.close()


class RemoteProcess:
    """A command running on the host, with its output (stderr merged in) delivered line by line."""

    def __init__(self, loop: _LoopThread, process: "asyncssh.SSHClientProcess[str]"):
        self._loop = loop
        self._process = process
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._done = threading.Event()
        self.error: SshError | None = None
        self.exit_status: int | None = None
        self.exit_signal: str | None = None
        self._pump = asyncio.run_coroutine_threadsafe(self._read(), loop.loop)

    async def _read(self) -> None:
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                self._lines.put(line.rstrip("\r\n"))
            await self._process.wait(check=False)
            self.exit_status = self._process.exit_status
            self.exit_signal = self._process.exit_signal[0] if self._process.exit_signal else None
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # connection lost mid-run
            self.error = _kind_of(exc)
        finally:
            self._done.set()

    def lines(self, timeout: float = 0.2) -> list[str]:
        """Whatever output is available now; waits up to `timeout` for the first line."""
        out: list[str] = []
        try:
            out.append(self._lines.get(timeout=timeout))
            while True:
                out.append(self._lines.get_nowait())
        except queue.Empty:
            pass
        return out

    @property
    def finished(self) -> bool:
        """True once the command ended AND everything it printed has been handed out."""
        return self._done.is_set() and self._lines.empty()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def close(self) -> None:
        self._pump.cancel()
        try:
            self._loop.call(self._close_process(), 5)
        except SshError:
            pass

    async def _close_process(self) -> None:
        self._process.close()


class SshConnection:
    """One authenticated, host-key-verified connection. Use as a context manager."""

    def __init__(self, target: SshTarget, connect_timeout: float | None = None):
        self.target = target
        self.connect_timeout = connect_timeout or settings.ssh_connect_timeout_seconds
        self._loop: _LoopThread | None = None
        self._conn: asyncssh.SSHClientConnection | None = None
        self._sftp: asyncssh.SFTPClient | None = None

    # -- lifecycle
    def open(self) -> "SshConnection":
        if self._conn is not None:  # already open: opening twice must not leak a second connection
            return self
        if not self.target.host_key:
            raise SshError(SshErrorKind.host_key_untrusted, "Ключ хоста не подтверждён: подтвердите отпечаток на странице «Раннеры»")
        try:
            trusted = asyncssh.import_public_key(self.target.host_key)
            client_key = asyncssh.import_private_key(self.target.private_key)
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise SshError(SshErrorKind.auth, "Не удалось прочитать ключ (приватный или ключ хоста)") from exc
        self._loop = _LoopThread()
        try:
            self._conn = self._loop.call(self._connect(trusted, client_key), self.connect_timeout + 10)
        except BaseException:
            self.close()
            raise
        return self

    async def _connect(self, trusted: "asyncssh.SSHKey", client_key: "asyncssh.SSHKey") -> asyncssh.SSHClientConnection:
        return await asyncio.wait_for(
            asyncssh.connect(
                self.target.host,
                self.target.port,
                username=self.target.user,
                client_keys=[client_key],
                known_hosts=([trusted], [], []),  # only this key is acceptable
                agent_path=None,
                config=None,
                password=None,
                preferred_auth="publickey",
                keepalive_interval=15,
                keepalive_count_max=3,
                connect_timeout=self.connect_timeout,
            ),
            self.connect_timeout + 5,
        )

    def close(self) -> None:
        loop, conn = self._loop, self._conn
        self._conn = self._sftp = None
        if loop is None:
            return
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        loop.close()
        self._loop = None

    def __enter__(self) -> "SshConnection":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        if self._loop is None or self._conn is None:
            coro.close()
            raise SshError(SshErrorKind.protocol, "Соединение закрыто")
        return self._loop.call(coro, timeout)

    # -- commands
    def run(self, command: str, *, input: str | None = None, timeout: float = 60) -> CommandResult:
        """Run a command to completion. The command line is visible to every user of the host: never put secrets in it
        — pass them as `input` instead."""

        async def go() -> CommandResult:
            assert self._conn is not None
            done = await asyncio.wait_for(
                self._conn.run(command, input=input, check=False, encoding="utf-8", errors="replace"), timeout
            )
            return CommandResult(done.exit_status, str(done.stdout or ""), str(done.stderr or ""))

        return self._call(go(), timeout + 5)

    def start(self, command: str, *, input: str | None = None) -> RemoteProcess:
        """Start a long-running command; `input` is written to its stdin, which is then closed."""

        async def go() -> "asyncssh.SSHClientProcess[str]":
            assert self._conn is not None
            process = await self._conn.create_process(
                command, stderr=asyncssh.STDOUT, encoding="utf-8", errors="replace", term_type=None
            )
            if input is not None:
                process.stdin.write(input)
            process.stdin.write_eof()
            return process

        process = self._call(go(), 30)
        assert self._loop is not None
        return RemoteProcess(self._loop, process)

    # -- files
    async def _sftp_client(self) -> asyncssh.SFTPClient:
        assert self._conn is not None
        if self._sftp is None:
            self._sftp = await self._conn.start_sftp_client()
        return self._sftp

    def exists(self, path: str) -> bool:
        async def go() -> bool:
            return bool(await (await self._sftp_client()).exists(path))

        return self._call(go(), 30)

    def makedirs(self, path: str) -> None:
        async def go() -> None:
            await (await self._sftp_client()).makedirs(path, exist_ok=True)

        self._call(go(), 30)

    def write_file(self, path: str, data: bytes, mode: int = 0o600) -> None:
        async def go() -> None:
            sftp = await self._sftp_client()
            parent = posixpath.dirname(path)
            if parent:
                await sftp.makedirs(parent, exist_ok=True)
            async with sftp.open(path, "wb") as fh:
                await fh.write(data)
            await sftp.chmod(path, mode)

        self._call(go(), 60)

    def read_file(self, path: str, max_bytes: int = 1_000_000) -> bytes:
        async def go() -> bytes:
            sftp = await self._sftp_client()
            async with sftp.open(path, "rb") as fh:
                return await fh.read(max_bytes)

        return self._call(go(), 60)

    def remove_tree(self, path: str) -> None:
        """Delete a directory on the host. Refuses anything that is not under a relative `oreshnik/…` path."""
        if not path or path.startswith("/") or ".." in path.split("/") or not path.startswith("oreshnik/"):
            raise SshError(SshErrorKind.other, "Отказ: удалять можно только внутри каталога oreshnik")

        async def go() -> None:
            sftp = await self._sftp_client()
            if await sftp.exists(path):
                await sftp.rmtree(path, ignore_errors=True)

        self._call(go(), 120)

    def put_tree(self, local: Path, remote: str) -> int:
        """Copy a local directory to the host (regular files only; symlinks are skipped). Returns the number of files."""

        async def go() -> int:
            sftp = await self._sftp_client()
            count = 0
            await sftp.makedirs(remote, exist_ok=True)
            for root, dirs, files in os.walk(local, followlinks=False):
                rel = os.path.relpath(root, local)
                target = remote if rel == "." else posixpath.join(remote, *Path(rel).parts)
                for d in list(dirs):
                    if os.path.islink(os.path.join(root, d)):
                        dirs.remove(d)
                        continue
                    await sftp.makedirs(posixpath.join(target, d), exist_ok=True)
                for name in files:
                    src = os.path.join(root, name)
                    if os.path.islink(src) or not os.path.isfile(src):
                        continue
                    await sftp.put(src, posixpath.join(target, name))
                    count += 1
            return count

        return self._call(go(), 600)

    def get_tree(
        self,
        remote: str,
        local: Path,
        *,
        max_bytes: int | None = None,
        max_files: int | None = None,
        skip: Callable[[str], bool] | None = None,
    ) -> tuple[int, int]:
        """Mirror a remote directory into `local` and return (files, bytes). The remote is untrusted: entries with
        suspicious names and symlinks are skipped, and going over `max_bytes` / `max_files` aborts with an error."""
        max_bytes = max_bytes if max_bytes is not None else settings.ssh_fetch_max_mb * 1024 * 1024
        max_files = max_files if max_files is not None else settings.ssh_fetch_max_files

        async def go() -> tuple[int, int]:
            sftp = await self._sftp_client()
            total_files = total_bytes = 0
            local.mkdir(parents=True, exist_ok=True)
            stack: list[tuple[str, Path, str]] = [(remote, local, "")]
            while stack:
                rdir, ldir, rel = stack.pop()
                for entry in await sftp.readdir(rdir):
                    name = entry.filename if isinstance(entry.filename, str) else entry.filename.decode("utf-8", "replace")
                    if name in (".", "..") or not name or "/" in name or "\x00" in name or "\\" in name:
                        continue
                    attrs = entry.attrs
                    relpath = f"{rel}/{name}" if rel else name
                    if skip is not None and skip(relpath):
                        continue
                    kind = attrs.type
                    if kind == 5 or (attrs.permissions is not None and statmod.S_ISLNK(attrs.permissions)):
                        continue  # symlink: never followed
                    if kind == 2 or (attrs.permissions is not None and statmod.S_ISDIR(attrs.permissions)):
                        (ldir / name).mkdir(exist_ok=True)
                        stack.append((posixpath.join(rdir, name), ldir / name, relpath))
                        continue
                    if kind != 1 and not (attrs.permissions is not None and statmod.S_ISREG(attrs.permissions)):
                        continue  # devices, sockets, …
                    size = int(attrs.size or 0)
                    total_files += 1
                    total_bytes += size
                    if total_files > max_files or total_bytes > max_bytes:
                        raise SshError(
                            SshErrorKind.other,
                            "Каталог прогона на хосте слишком велик — скачивание прервано "
                            f"(лимит {max_files} файлов / {max_bytes // (1024 * 1024)} МБ)",
                        )
                    tmp = ldir / f".{name}.part"
                    await sftp.get(posixpath.join(rdir, name), str(tmp))
                    os.replace(tmp, ldir / name)
            return total_files, total_bytes

        return self._call(go(), 900)

    def get_file(self, remote: str, local: Path, max_bytes: int = 50_000_000) -> bool:
        """Download one regular file atomically; False if it does not exist."""

        async def go() -> bool:
            sftp = await self._sftp_client()
            try:
                attrs = await sftp.stat(remote, follow_symlinks=False)
            except asyncssh.SFTPNoSuchFile:
                return False
            if attrs.permissions is not None and not statmod.S_ISREG(attrs.permissions):
                return False
            if int(attrs.size or 0) > max_bytes:
                raise SshError(SshErrorKind.other, "Файл на хосте слишком велик")
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(f".{local.name}.part")
            await sftp.get(remote, str(tmp))
            os.replace(tmp, local)
            return True

        return self._call(go(), 120)

    def list_dir(self, remote: str) -> list[str]:
        async def go() -> list[str]:
            sftp = await self._sftp_client()
            try:
                names = await sftp.listdir(remote)
            except asyncssh.SFTPNoSuchFile:
                return []
            return sorted(n for n in names if n not in (".", "..") and "/" not in n)

        return self._call(go(), 30)


def q(value: str) -> str:
    """Shell-quote one word for a POSIX shell on the host."""
    return shlex.quote(value)
