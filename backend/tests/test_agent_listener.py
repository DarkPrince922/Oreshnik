"""The agent's listener (docs/ARCHITECTURE.md §27): a fixed port range the panel proxies, on request, to the same port
inside the isolated agent container. `app/services/agent_listener.py` on its own (port_range parsing, the actual TCP pipe),
and the HTTP surface in `app/api/agent.py` (ownership, range checks, conflicts)."""

import asyncio
import socket

import pytest

from app.core.config import settings
from app.services import agent_listener
from app.services.agent import AgentError
from tests.conftest import auth_headers, make_user
from tests.test_agent import op_user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every test gets its own narrow range, proxies to 127.0.0.1 instead of the real `agent` host, and leaves no
    listener bound on a shared port once it is done."""
    monkeypatch.setattr(settings, "agent_listener_ports", "33001-33010")
    monkeypatch.setattr(settings, "agent_listener_idle_seconds", 1800)
    monkeypatch.setattr(settings, "agent_token", "test-token")  # the listener endpoints gate on the agent feature being on at all
    # Two different loopback addresses stand in for two different machines: the panel's own bind (normally 0.0.0.0, reachable
    # from outside Docker) and the agent container's host name on `agentnet` — binding both to the SAME port number on 127.0.0.1
    # alone would collide, which is exactly what two different machines never do.
    monkeypatch.setattr(agent_listener, "BIND_HOST", "127.0.0.3")
    monkeypatch.setattr(agent_listener, "AGENT_HOST", "127.0.0.1")
    yield
    asyncio.run(agent_listener.close_all())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── port_range() ──────────────────────────────────────────────────────────

def test_port_range_is_none_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "agent_listener_ports", "")
    assert agent_listener.port_range() is None


@pytest.mark.parametrize("raw", ["", "4444", "4444-", "-4450", "4450-4444", "abc-def", "4444-999999"])
def test_port_range_rejects_malformed_or_backwards_values(monkeypatch, raw):
    monkeypatch.setattr(settings, "agent_listener_ports", raw)
    assert agent_listener.port_range() is None


def test_port_range_parses_a_valid_range():
    assert agent_listener.port_range() == (33001, 33010)


# ── the actual TCP pipe ──────────────────────────────────────────────────

async def _echo_server(port: int) -> asyncio.base_events.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(4096):
            writer.write(chunk)
            await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", port)


async def _roundtrip(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.3", port)  # the proxy's own (BIND_HOST) side
    writer.write(b"hello agent")
    await writer.drain()
    data = await reader.readexactly(len(b"hello agent"))
    writer.close()
    return data


def test_a_byte_sent_to_the_proxied_port_reaches_the_agent_and_comes_back():
    async def go():
        port = 33002
        inside = await _echo_server(port)  # stands in for the agent container's own listener on the same port
        try:
            await agent_listener.open_listener(port=port, session_key="k1", session_id=1, user_id=1)
            assert await _roundtrip(port) == b"hello agent"
        finally:
            await agent_listener.close_all()
            inside.close()
    asyncio.run(go())


def test_nothing_listening_inside_the_container_closes_the_connection_cleanly():
    async def go():
        port = 33003
        await agent_listener.open_listener(port=port, session_key="k1", session_id=1, user_id=1)
        reader, writer = await asyncio.open_connection("127.0.0.3", port)
        assert await reader.read() == b""  # the proxy accepted, found nobody inside, and closed
        writer.close()
    asyncio.run(go())


def test_a_port_outside_the_range_is_refused():
    async def go():
        with pytest.raises(AgentError) as exc:
            await agent_listener.open_listener(port=1, session_key="k1", session_id=1, user_id=1)
        assert exc.value.status == 422
    asyncio.run(go())


def test_the_listener_is_off_when_the_range_is_unset(monkeypatch):
    monkeypatch.setattr(settings, "agent_listener_ports", "")

    async def go():
        with pytest.raises(AgentError) as exc:
            await agent_listener.open_listener(port=33001, session_key="k1", session_id=1, user_id=1)
        assert exc.value.status == 503
    asyncio.run(go())


def test_a_port_already_taken_by_another_session_is_refused():
    async def go():
        await agent_listener.open_listener(port=33004, session_key="k1", session_id=1, user_id=1)
        with pytest.raises(AgentError) as exc:
            await agent_listener.open_listener(port=33004, session_key="k2", session_id=2, user_id=1)
        assert exc.value.status == 409
    asyncio.run(go())


def test_reopening_the_same_port_for_the_same_session_is_a_no_op():
    async def go():
        first = await agent_listener.open_listener(port=33005, session_key="k1", session_id=1, user_id=1)
        second = await agent_listener.open_listener(port=33005, session_key="k1", session_id=1, user_id=1)
        assert first.server is second.server
    asyncio.run(go())


def test_a_session_opening_a_new_port_closes_its_old_one_first():
    async def go():
        await agent_listener.open_listener(port=33006, session_key="k1", session_id=1, user_id=1)
        await agent_listener.open_listener(port=33007, session_key="k1", session_id=1, user_id=1)
        assert agent_listener.status_for("k1").port == 33007
        assert 33006 not in agent_listener._listeners
    asyncio.run(go())


def test_closing_by_session_key_only_touches_that_sessions_port():
    async def go():
        await agent_listener.open_listener(port=33008, session_key="k1", session_id=1, user_id=1)
        await agent_listener.open_listener(port=33009, session_key="k2", session_id=2, user_id=1)
        await agent_listener.close_for_session("k1")
        assert agent_listener.status_for("k1") is None
        assert agent_listener.status_for("k2") is not None
    asyncio.run(go())


def test_idle_sweep_closes_a_listener_nobody_used(monkeypatch):
    monkeypatch.setattr(settings, "agent_listener_idle_seconds", 30)

    async def go():
        listener = await agent_listener.open_listener(port=33010, session_key="k1", session_id=1, user_id=1)
        from datetime import datetime, timedelta, timezone
        listener.last_active = datetime.now(timezone.utc) - timedelta(seconds=31)
        await agent_listener.sweep_idle()
        assert agent_listener.status_for("k1") is None
    asyncio.run(go())


# ── the HTTP surface ──────────────────────────────────────────────────────

def test_listener_range_endpoint_reflects_settings(client):
    user, headers = op_user(client)
    assert client.get("/api/agent/listener-range", headers=headers).json() == {"available": True, "min": 33001, "max": 33010}


def test_listener_range_endpoint_says_unavailable_when_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_listener_ports", "")
    user, headers = op_user(client)
    assert client.get("/api/agent/listener-range", headers=headers).json() == {"available": False, "min": None, "max": None}


def test_opening_and_closing_a_listener_through_the_session_api(client, monkeypatch):
    from app.core.db import SessionLocal
    from app.models.agent import AgentSession

    user, headers = op_user(client)
    with SessionLocal() as db:
        session = AgentSession(user_id=user.id, title="t", key="sess-a")
        db.add(session)
        db.commit()
        db.refresh(session)

    assert client.get(f"/api/agent/sessions/{session.id}/listener", headers=headers).json() == {"port": None, "opened_at": None, "last_active": None}

    opened = client.post(f"/api/agent/sessions/{session.id}/listener", json={"port": 33001}, headers=headers)
    assert opened.status_code == 200 and opened.json()["port"] == 33001

    fetched = client.get(f"/api/agent/sessions/{session.id}/listener", headers=headers)
    assert fetched.json()["port"] == 33001

    assert client.delete(f"/api/agent/sessions/{session.id}/listener", headers=headers).status_code == 204
    assert client.get(f"/api/agent/sessions/{session.id}/listener", headers=headers).json()["port"] is None


def test_a_port_outside_the_range_is_refused_over_http(client):
    from app.core.db import SessionLocal
    from app.models.agent import AgentSession

    user, headers = op_user(client)
    with SessionLocal() as db:
        session = AgentSession(user_id=user.id, title="t", key="sess-b")
        db.add(session)
        db.commit()
        db.refresh(session)
    r = client.post(f"/api/agent/sessions/{session.id}/listener", json={"port": 1}, headers=headers)
    assert r.status_code == 422


def test_a_listener_cannot_be_opened_or_seen_for_someone_elses_session(client):
    from app.core.db import SessionLocal
    from app.models.agent import AgentSession

    owner, _ = op_user(client, "owner@example.com")
    _, other_headers = op_user(client, "other@example.com")
    with SessionLocal() as db:
        session = AgentSession(user_id=owner.id, title="t", key="sess-c")
        db.add(session)
        db.commit()
        db.refresh(session)
    assert client.get(f"/api/agent/sessions/{session.id}/listener", headers=other_headers).status_code == 404
    assert client.post(f"/api/agent/sessions/{session.id}/listener", json={"port": 33001}, headers=other_headers).status_code == 404


def test_deleting_a_session_closes_its_listener(client):
    from app.core.db import SessionLocal
    from app.models.agent import AgentSession

    user, headers = op_user(client)
    with SessionLocal() as db:
        session = AgentSession(user_id=user.id, title="t", key="sess-d")
        db.add(session)
        db.commit()
        db.refresh(session)
    client.post(f"/api/agent/sessions/{session.id}/listener", json={"port": 33001}, headers=headers)
    assert agent_listener.status_for("sess-d") is not None
    client.delete(f"/api/agent/sessions/{session.id}", headers=headers)
    assert agent_listener.status_for("sess-d") is None
