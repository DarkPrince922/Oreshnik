from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import create_listener_token
from app.models import Role
from app.models.agent import AgentSession, AgentSessionStatus
from app.services import agent, agent_listener
from tests.conftest import auth_headers, make_user


def test_shared_runtime_denies_other_accounts_and_queued_work(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_token", "test-panel-token")
    owner = make_user("runtime-owner@example.com")
    other = make_user("other-account@example.com", role=Role.admin)
    headers = auth_headers(client, other.email)
    monkeypatch.setattr(settings, "agent_runtime_owner_id", 0)
    owner_headers = auth_headers(client, owner.email)
    assert not client.get("/api/agent/health", headers=owner_headers).json()["ok"]
    monkeypatch.setattr(settings, "agent_runtime_owner_id", owner.id)
    with SessionLocal() as db:
        session = AgentSession(user_id=other.id, title="Other", key="other-session", status=AgentSessionStatus.running)
        db.add(session)
        db.commit()
        session_id = session.id

    def unexpected_runtime_access(*args, **kwargs):
        raise AssertionError("The shared runtime was contacted")

    monkeypatch.setattr(agent, "agent_async_client", unexpected_runtime_access)
    monkeypatch.setattr(agent, "stream_turn", unexpected_runtime_access)
    monkeypatch.setattr(agent, "check_mcp", unexpected_runtime_access)
    mcp = client.post("/api/mcp", headers=headers, json={
        "name": "fixture", "transport": "command", "command": "true",
    })
    assert mcp.status_code == 201
    assert client.post(f"/api/mcp/{mcp.json()['id']}/check", headers=headers).status_code == 403
    assert client.get(f"/api/agent/sessions/{session_id}/files", headers=headers).status_code == 403
    assert client.post("/api/agent/sessions", headers=headers, json={"llm_profile_id": 1}).status_code == 403
    agent.run_turn(SessionLocal, session_id, "fixture")
    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.status == AgentSessionStatus.idle and session.last_error


def test_listener_rejects_token_of_deleted_session(client, monkeypatch):
    user = make_user("listener-owner@example.com")
    monkeypatch.setattr(settings, "agent_token", "test-panel-token")
    monkeypatch.setattr(settings, "agent_runtime_owner_id", user.id)
    monkeypatch.setattr(settings, "agent_listener_self", True)

    def unexpected_listener_access(*args, **kwargs):
        raise AssertionError("No port should be opened or closed by a revoked token")

    monkeypatch.setattr(agent_listener, "open_listener", unexpected_listener_access)
    monkeypatch.setattr(agent_listener, "close_listener", unexpected_listener_access)
    with SessionLocal() as db:
        session = AgentSession(user_id=user.id, title="Fixture", key="deleted-session")
        db.add(session)
        db.commit()
        token = create_listener_token(session.id, session.key, user.id, 300)
        db.delete(session)
        db.commit()

    headers = {"Authorization": f"Bearer {token}"}
    for method in ("POST", "DELETE"):
        response = client.request(method, "/api/agent/internal/listener", headers=headers, json={"port": 4444})
        assert response.status_code == 401
