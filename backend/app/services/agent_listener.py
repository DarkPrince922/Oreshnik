"""A listener for the agent: a fixed range of ports the panel (this process) has published to the host, proxied — on
explicit, per-session request — to the same port inside the sandboxed `agent` container. See docs/ARCHITECTURE.md §27 for
why this exists and what it does not do.

The container itself never gets a published port or anything beyond the isolated `agentnet` network (see §20); this module
is what makes a port *in* that network briefly reachable from outside Docker, and only the one port, for the one session
that asked for it, only while it is open. The agent binds the listening socket itself (e.g. `nc -lp 4444`, a reverse-shell
catcher, a webhook receiver for a target it is testing) — this module is the dumb TCP pipe in front of it, not a listener
of its own content.

State lives in this process only (an `asyncio.Server` cannot be handed to another process): a listener does not survive a
backend restart, and with more than one backend process sharing the same host ports this would double-bind — the panel
runs a single `uvicorn` process (see docker-compose.yml), so that does not apply here, but it is why this must stay true if
that ever changes.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import settings
from app.services.agent import AgentError

log = logging.getLogger("oreshnik.agent_listener")

_RANGE_RE = re.compile(r"^(\d{1,5})-(\d{1,5})$")
AGENT_HOST = "agent"  # the container's name on `agentnet` (docker-compose.yml) — the one host this may ever proxy to
BIND_HOST = "0.0.0.0"  # the host-facing side of the pipe; a test overrides this (and AGENT_HOST) to tell the two apart on one machine
PIPE_CHUNK = 65536


def port_range() -> tuple[int, int] | None:
    """The configured (min, max), or None when the listener is off (`AGENT_LISTENER_PORTS` empty or malformed)."""
    match = _RANGE_RE.match((settings.agent_listener_ports or "").strip())
    if not match:
        return None
    lo, hi = int(match.group(1)), int(match.group(2))
    if not (0 < lo <= hi <= 65535):
        return None
    return lo, hi


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Listener:
    port: int
    session_key: str
    session_id: int
    user_id: int
    server: asyncio.base_events.Server
    opened_at: datetime = field(default_factory=_now)
    last_active: datetime = field(default_factory=_now)


_listeners: dict[int, Listener] = {}
_lock = asyncio.Lock()


def status_for(session_key: str) -> Listener | None:
    return next((l for l in _listeners.values() if l.session_key == session_key), None)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, listener: Listener) -> None:
    try:
        while True:
            chunk = await reader.read(PIPE_CHUNK)
            if not chunk:
                break
            listener.last_active = _now()
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        writer.close()


async def _accept(port: int, caller_reader: asyncio.StreamReader, caller_writer: asyncio.StreamWriter) -> None:
    listener = _listeners.get(port)
    if listener is None:  # closed between accept() and here
        caller_writer.close()
        return
    try:
        agent_reader, agent_writer = await asyncio.wait_for(asyncio.open_connection(AGENT_HOST, port), timeout=10.0)
    except (OSError, asyncio.TimeoutError):
        log.info("listener on %s: nothing is listening inside the agent container yet", port)
        caller_writer.close()
        return
    listener.last_active = _now()
    await asyncio.gather(
        _pipe(caller_reader, agent_writer, listener), _pipe(agent_reader, caller_writer, listener), return_exceptions=True,
    )


async def open_listener(*, port: int, session_key: str, session_id: int, user_id: int) -> Listener:
    rng = port_range()
    if rng is None:
        raise AgentError("Листенер выключен: AGENT_LISTENER_PORTS не задан на сервере", 503)
    if not (rng[0] <= port <= rng[1]):
        raise AgentError(f"Порт должен быть в диапазоне {rng[0]}–{rng[1]}", 422)
    async with _lock:
        existing = _listeners.get(port)
        if existing is not None:
            if existing.session_key == session_key:
                return existing
            raise AgentError(f"Порт {port} уже занят другой сессией", 409)
        for other_port, other in list(_listeners.items()):
            if other.session_key == session_key:  # one listener per session at a time
                await _close(other_port)
        try:
            server = await asyncio.start_server(lambda r, w: _accept(port, r, w), host=BIND_HOST, port=port)
        except OSError as exc:
            raise AgentError(f"Не удалось занять порт {port}: {exc}", 409)
        listener = Listener(port=port, session_key=session_key, session_id=session_id, user_id=user_id, server=server)
        _listeners[port] = listener
        log.info("listener opened: port=%s session=%s user=%s", port, session_key, user_id)
        return listener


async def _close(port: int) -> None:
    listener = _listeners.pop(port, None)
    if listener is not None:
        listener.server.close()
        log.info("listener closed: port=%s session=%s", port, listener.session_key)


async def close_listener(port: int, *, session_key: str | None = None) -> bool:
    """True when something was closed. `session_key`, if given, must own the port (a user closes only their own)."""
    async with _lock:
        listener = _listeners.get(port)
        if listener is None:
            return False
        if session_key is not None and listener.session_key != session_key:
            return False
        await _close(port)
        return True


async def close_for_session(session_key: str) -> None:
    async with _lock:
        for port, listener in list(_listeners.items()):
            if listener.session_key == session_key:
                await _close(port)


async def close_all() -> None:
    async with _lock:
        for port in list(_listeners):
            await _close(port)


async def sweep_idle() -> None:
    """Close whatever nobody has sent a byte through for `agent_listener_idle_seconds` — an operator who forgets to close
    a listener must not leave a port open on the host indefinitely."""
    async with _lock:
        cutoff = settings.agent_listener_idle_seconds
        now = _now()
        for port, listener in list(_listeners.items()):
            if (now - listener.last_active).total_seconds() > cutoff:
                log.info("listener on %s closed: idle for more than %ss", port, cutoff)
                await _close(port)


async def sweep_loop(interval: float = 30.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_idle()
        except Exception:
            log.exception("listener idle sweep failed")
