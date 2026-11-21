"""A JSON-RPC client for one `codex app-server` process, spoken over its stdin/stdout (one JSON object per line).

Message shapes, as seen from the real Codex 0.169 (there is no "jsonrpc" field):
  request        {"id": 1, "method": "thread/start", "params": {...}}      -> response {"id": 1, "result": {...}} or {"id": 1, "error": {...}}
  notification   {"method": "item/agentMessage/delta", "params": {...}}    (no id; both directions)
  server request {"id": 0, "method": "item/commandExecution/requestApproval", "params": {...}}  -> we answer {"id": 0, "result": {...}}
"""

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable

MAX_STDERR_TAIL = 4000


class RpcError(Exception):
    def __init__(self, method: str, error: object):
        self.method, self.error = method, error
        message = error.get("message") if isinstance(error, dict) else str(error)
        super().__init__(f"{method}: {message}")


class AppServer:
    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        on_notification: Callable[[dict], None],
        on_request: Callable[[dict], Awaitable[None]],
        on_exit: Callable[[], None],
    ):
        self.argv, self.env, self.cwd = argv, env, cwd
        self._on_notification, self._on_request, self._on_exit = on_notification, on_request, on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._tasks: list[asyncio.Task] = []
        self.stderr_tail = bytearray()
        self.closed = False

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and not self.closed

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=32 * 1024 * 1024,
        )
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._drain_stderr())]

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while chunk := await self.proc.stderr.read(4096):
            self.stderr_tail.extend(chunk)
            del self.stderr_tail[:-MAX_STDERR_TAIL]

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                try:
                    raw = await self.proc.stdout.readline()
                except ValueError:  # a line over the limit: nothing sensible to do with it
                    continue
                if not raw:
                    break
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif "method" in message and "id" in message:
                    asyncio.create_task(self._on_request(message))
                elif "method" in message:
                    self._on_notification(message)
        finally:
            self.closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("codex app-server exited"))
            self._pending.clear()
            self._on_exit()

    def _write(self, message: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())

    async def request(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        """The `result` of the call; `RpcError` when the server answers with an error, `ConnectionError` when it is gone."""
        if not self.alive:
            raise ConnectionError("codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write({"id": request_id, "method": method, **({"params": params} if params is not None else {})})
        await self.proc.stdin.drain()  # type: ignore[union-attr]
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in reply:
            raise RpcError(method, reply["error"])
        return reply.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        if self.alive:
            self._write({"method": method, **({"params": params} if params is not None else {})})

    def respond(self, request_id: object, result: dict | None = None, error: dict | None = None) -> None:
        if self.alive:
            self._write({"id": request_id, **({"error": error} if error is not None else {"result": result or {}})})

    async def stop(self, grace: float = 5.0) -> None:
        """End the process (and everything it started): politely first, then for good."""
        self.closed = True
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(proc.wait(), wait)
                    break
                except TimeoutError:
                    continue
        for task in self._tasks:
            task.cancel()
