"""«MCP-серверы»: secrets stored encrypted and never shown back, own and shared servers, the check, and what a session of «Код» gets."""
from app.core.db import SessionLocal
from app.models import McpServer, Role
from app.services import agent as agent_service
from tests.test_agent import new_session, op_user, openai_profile, report, wired  # noqa: F401  (`wired`: the agent stand-in)
from tests.test_agentd import agentd  # noqa: F401  (needed by `wired`)


def make(client, headers, **body):
    body = {"name": "shodan", "transport": "command", "command": "npx -y shodan-mcp --mode 'read only'",
            "env": [{"name": "SHODAN_API_KEY", "value": "sk-very-secret"}], **body}
    r = client.post("/api/mcp", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_secrets_are_encrypted_never_shown_and_kept_unless_replaced(client):
    _, h = op_user(client)
    server = make(client, h)
    assert server["env_names"] == ["SHODAN_API_KEY"] and "sk-very-secret" not in str(client.get("/api/mcp", headers=h).json())
    with SessionLocal() as db:
        assert "sk-very-secret" not in (db.get(McpServer, server["id"]).env_enc or "")
    # the list sent back without a value keeps the stored one; a new name needs a value
    r = client.patch(f"/api/mcp/{server['id']}", json={"env": [{"name": "SHODAN_API_KEY"}, {"name": "EXTRA", "value": "x"}]}, headers=h)
    assert r.json()["env_names"] == ["EXTRA", "SHODAN_API_KEY"]
    with SessionLocal() as db:
        from app.api.mcp import agent_payload
        assert agent_payload(db.get(McpServer, server["id"]))["env"] == {"SHODAN_API_KEY": "sk-very-secret", "EXTRA": "x"}
    assert client.patch(f"/api/mcp/{server['id']}", json={"env": [{"name": "NEW"}]}, headers=h).status_code == 422
    assert client.post("/api/mcp", json={"name": "x", "transport": "command", "command": "a", "env": [{"name": "PATH", "value": "/e"}]}, headers=h).status_code == 422
    url = client.post("/api/mcp", json={"name": "burp", "transport": "url", "url": "http://10.0.0.5:9876/mcp", "bearer_token": "t1",
                                        "headers": [{"name": "X-Api-Key", "value": "h1"}]}, headers=h).json()
    assert url["has_token"] and url["header_names"] == ["X-Api-Key"] and url["command"] is None
    assert client.patch(f"/api/mcp/{url['id']}", json={"bearer_token": ""}, headers=h).json()["has_token"] is False


def test_own_servers_are_private_and_shared_ones_are_for_everybody(client):
    _, admin = op_user(client, "admin@example.com", Role.admin)
    _, op1 = op_user(client, "op1@example.com")
    _, op2 = op_user(client, "op2@example.com")
    mine = make(client, op1)
    make(client, admin, name="burp", transport="url", url="http://burp/mcp", env=[], shared=True)
    assert sorted(s["name"] for s in client.get("/api/mcp", headers=op1).json()) == ["burp", "shodan"]
    assert [s["name"] for s in client.get("/api/mcp", headers=op2).json()] == ["burp"]
    assert client.post("/api/mcp", json={"name": "z", "transport": "url", "url": "http://z/", "shared": True}, headers=op1).status_code == 403
    assert client.patch(f"/api/mcp/{mine['id']}", json={"enabled": False}, headers=op2).status_code == 404
    assert client.post("/api/mcp", json={"name": "shodan", "transport": "url", "url": "http://z/"}, headers=op1).status_code == 409


def test_the_check_lists_the_tools_and_is_remembered(client, monkeypatch):
    _, h = op_user(client)
    server = make(client, h)
    sent = []
    monkeypatch.setattr(agent_service, "check_mcp", lambda payload, timeout: sent.append(payload) or {"ok": True, "tools": [{"name": "search", "description": "d"}], "error": None})
    out = client.post(f"/api/mcp/{server['id']}/check", headers=h).json()
    assert out["last_check"]["ok"] and out["last_check"]["tools"] == [{"name": "search", "description": "d"}]
    assert sent[0] == {"name": "shodan", "command": "npx", "args": ["-y", "shodan-mcp", "--mode", "read only"],
                       "env": {"SHODAN_API_KEY": "sk-very-secret"}, "startup_timeout_sec": 30, "tool_timeout_sec": 120}
    assert client.get("/api/mcp", headers=h).json()[0]["last_check"]["ok"]
    assert client.patch(f"/api/mcp/{server['id']}", json={"command": "npx other"}, headers=h).json()["last_check"] is None   # a new setup is unchecked


def test_a_session_of_kod_gets_the_enabled_servers_and_the_users_own_wins(client, wired, monkeypatch):
    sent: list[dict] = []
    real = agent_service.stream_turn
    monkeypatch.setattr(agent_service, "stream_turn", lambda **kw: sent.append(kw) or real(**kw))
    _, admin = op_user(client, "admin@example.com", Role.admin)
    user, h = op_user(client)
    make(client, admin, shared=True, env=[{"name": "SHODAN_API_KEY", "value": "admins"}])
    make(client, admin, name="agentonly", shared=True, scope="agent")
    make(client, h)
    off = make(client, h, name="off", env=[])
    client.patch(f"/api/mcp/{off['id']}", json={"enabled": False}, headers=h)
    report(client, h, new_session(client, h, openai_profile(user.id))["id"])
    assert [(s["name"], s["env"]) for s in sent[-1]["mcp_servers"]] == [("shodan", {"SHODAN_API_KEY": "sk-very-secret"})]


def test_two_servers_that_set_one_variable_differently_stop_the_turn_with_a_reason(client, wired):
    user, h = op_user(client)
    make(client, h)
    make(client, h, name="other", env=[{"name": "SHODAN_API_KEY", "value": "different"}])
    s = new_session(client, h, openai_profile(user.id))
    from tests.test_agent import send, wait_idle
    send(client, h, s["id"], "hi")
    wait_idle(s["id"])
    assert "по-разному" in client.get(f"/api/agent/sessions/{s['id']}", headers=h).json()["last_error"]
