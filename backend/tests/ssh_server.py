"""A real SSH server for the tests (asyncssh), so the SSH code is exercised over the actual protocol.

It accepts one user with one key, runs `exec` requests through /bin/sh with HOME set to a temporary directory, and serves
SFTP from that same directory (so a relative SFTP path and `$HOME/…` in a command are the same place). It records every
command line it was asked to run — the tests use that to prove that secrets never travel in a command line.
"""
import asyncio
import os
import shutil
import stat
from pathlib import Path

import asyncssh

from app.services.ssh import SshTarget, _LoopThread

FAKE_STRIX = Path(__file__).parent / "fake_strix.py"

DOCKER_STUB = """#!/bin/sh
echo "docker $*" >> "$HOME/docker-calls.log"
case "$1" in
  info) [ -f "$HOME/docker-broken" ] && { echo "permission denied" >&2; exit 1; }; exit 0 ;;
  ps) cat "$HOME/containers.txt" 2>/dev/null; exit 0 ;;
  *) exit 0 ;;
esac
"""


# What a bare machine offers. docker, strix, uv and curl are deliberately NOT here: the tests "install" them, so what the
# panel finds on the host does not depend on what happens to be installed on the machine running the tests.
SYSTEM_TOOLS = (
    "sh", "cat", "id", "df", "awk", "sleep", "mkdir", "rm", "touch", "head", "tr", "fold", "base64", "chmod", "env", "printenv",
    "setsid", "timeout", "python3", "ln", "mkfifo", "sed", "grep", "dirname", "basename", "wc", "sort", "ls", "cut", "date",
    "uname", "xargs", "tee", "mv", "cp", "kill", "ps", "readlink", "true", "false", "test", "expr",
)


class FakeHost:
    def __init__(self, home: Path, *, user: str = "scan", password: str | None = None):
        self.home = home
        self.user = user
        self.password = password      # None: this host does not allow password login at all
        self.allow_key = True
        self.client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        # exported once: every export of an OpenSSH key carries a fresh random check value, so two exports differ as text
        self._client_pem = self.client_key.export_private_key().decode()
        self.commands: list[str] = []
        self.inputs: list[str] = []
        self.connections: list[asyncssh.SSHServerConnection] = []
        self.logins = 0       # successful key authentications
        self.attempts = 0     # every public-key authentication attempt, successful or not
        self.password_attempts = 0
        self.password_logins = 0
        self._loop: _LoopThread | None = None
        self._server: asyncssh.SSHAcceptor | None = None
        self.port = 0
        home.mkdir(parents=True, exist_ok=True)
        self.sysbin = home / "sysbin"
        self.sysbin.mkdir(exist_ok=True)
        for tool in SYSTEM_TOOLS:
            found = shutil.which(tool)
            if found and not (self.sysbin / tool).exists():
                (self.sysbin / tool).symlink_to(found)

    # -- what the panel needs to know
    @property
    def host_key_line(self) -> str:
        return self.host_key.export_public_key().decode().strip()

    @property
    def client_key_pem(self) -> str:
        return self._client_pem

    def target(self, *, trusted: bool = True) -> SshTarget:
        return SshTarget("127.0.0.1", self.port, self.user, self.client_key_pem, self.host_key_line if trusted else None)

    def password_target(self, password: str | None = None, *, trusted: bool = True) -> SshTarget:
        """Log in with a password only (no key)."""
        return SshTarget("127.0.0.1", self.port, self.user, None, self.host_key_line if trusted else None, password or self.password)

    # -- the fake machine
    def install_tools(self, *, strix: bool = True, docker: bool = True) -> None:
        bindir = self.home / ".local" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        if strix:
            shutil.copy(FAKE_STRIX, bindir / "strix")
            (bindir / "strix").chmod(0o755)
        if docker:
            (bindir / "docker").write_text(DOCKER_STUB)
            (bindir / "docker").chmod(0o755)

    def docker_calls(self) -> list[str]:
        path = self.home / "docker-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    # -- lifecycle
    def _server_class(self):
        host = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                host.connections.append(conn)

            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return host.allow_key

            def password_auth_supported(self):
                return host.password is not None

            def validate_password(self, username, password):
                host.password_attempts += 1
                ok = host.password is not None and username == host.user and password == host.password
                host.password_logins += 1 if ok else 0
                return ok

            def validate_public_key(self, username, key):
                ok = username == host.user and key.public_data == host.client_key.public_data
                host.attempts += 1
                host.logins += 1 if ok else 0
                return ok

        return Server

    async def _listen(self, port: int):
        return await asyncssh.listen(
            "127.0.0.1",
            port,
            server_factory=self._server_class(),
            server_host_keys=[self.host_key],
            process_factory=self._handle,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(self.home).encode()),
            allow_scp=False,
            encoding=None,
            reuse_address=True,
        )

    def start(self) -> "FakeHost":
        self._loop = _LoopThread()
        self._server = self._loop.call(self._listen(0), 10)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        if self._loop is None:
            return
        server = self._server
        if server is not None:
            self._loop.loop.call_soon_threadsafe(server.close)
        self.kick()
        self._loop.close()
        self._loop = None

    def kick(self) -> None:
        """Drop every connection, as a network failure would."""
        loop = self._loop
        if loop is None:
            return
        for conn in list(self.connections):
            loop.loop.call_soon_threadsafe(conn.abort)
        self.connections.clear()

    def rotate_host_key(self) -> None:
        """The machine presents a different host key from now on (for new connections)."""
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        assert self._server is not None and self._loop is not None
        self._loop.loop.call_soon_threadsafe(self._server.close)
        self._server = self._loop.call(self._listen(self.port), 10)

    # -- exec
    async def _handle(self, process: asyncssh.SSHServerProcess) -> None:
        command = process.command or ""
        self.commands.append(command)
        env = {
            "HOME": str(self.home),
            "PATH": str(self.sysbin),
            "LANG": "C.UTF-8",
        }
        child = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self.home), env=env,
        )
        received = bytearray()

        async def feed() -> None:
            try:
                while True:
                    data = await process.stdin.read(65536)
                    if not data:
                        break
                    received.extend(data)
                    child.stdin.write(data)
                    await child.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    child.stdin.close()
                except Exception:
                    pass
                self.inputs.append(bytes(received).decode("utf-8", "replace"))

        async def pump(source, sink) -> None:
            try:
                while True:
                    data = await source.read(65536)
                    if not data:
                        break
                    sink.write(data)
            except Exception:
                transport = getattr(source, "_transport", None)
                if transport is not None:
                    transport.close()  # the client is gone: the command gets EPIPE, like under a real sshd without a tty

        feeder = asyncio.ensure_future(feed())
        pumpers = [asyncio.ensure_future(pump(child.stdout, process.stdout)), asyncio.ensure_future(pump(child.stderr, process.stderr))]
        code = await child.wait()
        await asyncio.wait(pumpers, timeout=5)
        feeder.cancel()
        try:
            process.exit(code if code >= 0 else 128 - code)
        except Exception:
            pass


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
