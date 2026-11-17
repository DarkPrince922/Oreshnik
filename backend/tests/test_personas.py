"""«Личности»: own and shared personas, who may change them, and how a session of «Код» runs with one."""
from app.models import Role
from app.services import agent as agent_service
from tests.test_agent import new_session, op_user, openai_profile, report, wired  # noqa: F401  (`wired`: the agent stand-in)
from tests.test_agentd import agentd  # noqa: F401  (needed by `wired`)


def make(client, headers, **body):
    body = {"name": "Пентестер", "instructions": "Ты — пентестер. Отвечай по-русски.", **body}
    r = client.post("/api/personas", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def names(client, headers):
    return sorted(p["name"] for p in client.get("/api/personas", headers=headers).json())


def test_own_personas_are_private_and_shared_ones_are_for_everybody(client):
    _, admin = op_user(client, "admin@example.com", Role.admin)
    _, op1 = op_user(client, "op1@example.com")
    _, op2 = op_user(client, "op2@example.com")
    make(client, op1, name="Моя")
    make(client, admin, name="Админская")
    shared = make(client, admin, name="Общая", shared=True)
    assert names(client, op1) == ["Моя", "Общая"]
    assert names(client, op2) == ["Общая"]
    assert names(client, admin) == ["Админская", "Общая"]  # an admin does not see others' private ones either
    seen = next(p for p in client.get("/api/personas", headers=op2).json() if p["id"] == shared["id"])
    assert seen["shared"] and not seen["editable"] and not seen["mine"]


def test_who_may_change_what(client):
    _, admin = op_user(client, "admin@example.com", Role.admin)
    _, op1 = op_user(client, "op1@example.com")
    _, op2 = op_user(client, "op2@example.com")
    assert client.post("/api/personas", json={"name": "x", "instructions": "y", "shared": True}, headers=op1).status_code == 403
    shared = make(client, admin, name="Общая", shared=True)
    mine = make(client, op1, name="Моя")
    assert client.patch(f"/api/personas/{shared['id']}", json={"name": "z"}, headers=op1).status_code == 403
    assert client.delete(f"/api/personas/{shared['id']}", headers=op1).status_code == 403
    assert client.patch(f"/api/personas/{mine['id']}", json={"name": "z"}, headers=op2).status_code == 404  # not even seen
    assert client.patch(f"/api/personas/{shared['id']}", json={"instructions": " Новое. "}, headers=admin).json()["instructions"] == "Новое."
    assert client.delete(f"/api/personas/{mine['id']}", headers=op1).status_code == 204


def test_one_default_per_user_and_validation(client):
    _, op1 = op_user(client, "op1@example.com")
    a = make(client, op1, name="A", is_default=True)
    b = make(client, op1, name="B", is_default=True)
    listed = {p["name"]: p["is_default"] for p in client.get("/api/personas", headers=op1).json()}
    assert listed == {"A": False, "B": True}
    assert client.patch(f"/api/personas/{a['id']}", json={"is_default": True}, headers=op1).json()["is_default"] is True
    assert {p["name"]: p["is_default"] for p in client.get("/api/personas", headers=op1).json()} == {"A": True, "B": False}
    for bad in ({"name": " ", "instructions": "x"}, {"name": "x", "instructions": ""}, {"name": "x", "instructions": "y", "scope": "everywhere"}):
        assert client.post("/api/personas", json=bad, headers=op1).status_code == 422
    assert b["scope"] == "both"
    _, viewer = op_user(client, "v@example.com", Role.viewer)
    assert client.get("/api/personas", headers=viewer).status_code == 403


def test_a_session_runs_with_its_persona_first(client, wired, monkeypatch):
    monkeypatch.setattr(agent_service, "listener_note", lambda: "")
    user, h = op_user(client)
    persona = make(client, h, name="Аудитор", instructions="Ты — аудитор. Ничего не меняй.")
    s = new_session(client, h, openai_profile(user.id), instructions="Кратко.", persona_id=persona["id"])
    assert s["persona_id"] == persona["id"] and s["persona_name"] == "Аудитор"
    assert report(client, h, s["id"])["seen"]["developerInstructions"] == "Ты — аудитор. Ничего не меняй.\n\nКратко."
    # a change of the persona reaches the conversation inside its next message
    client.patch(f"/api/personas/{persona['id']}", json={"instructions": "Ты — аудитор. Пиши отчёт."}, headers=h)
    told = report(client, h, s["id"])["seen"]["lastPrompt"]
    assert "Пиши отчёт." in told and told.endswith("report")
    # taken off the session, and a persona deleted under a session leaves it running without one
    assert client.patch(f"/api/agent/sessions/{s['id']}", json={"persona_id": None}, headers=h).json()["persona_id"] is None
    other = make(client, h, name="Другая")
    client.patch(f"/api/agent/sessions/{s['id']}", json={"persona_id": other["id"]}, headers=h)
    client.delete(f"/api/personas/{other['id']}", headers=h)
    assert client.get(f"/api/agent/sessions/{s['id']}", headers=h).json()["persona_id"] is None


def test_the_default_persona_is_taken_when_none_is_named_and_scope_is_respected(client, wired, monkeypatch):
    monkeypatch.setattr(agent_service, "listener_note", lambda: "")
    user, h = op_user(client)
    profile = openai_profile(user.id)
    default = make(client, h, name="По умолчанию", is_default=True)
    assert new_session(client, h, profile)["persona_id"] == default["id"]
    agent_only = make(client, h, name="Только для Агента", scope="agent")
    r = client.post("/api/agent/sessions", json={"llm_profile_id": profile.id, "persona_id": agent_only["id"]}, headers=h)
    assert r.status_code == 422
    _, other = op_user(client, "other@example.com")
    foreign = make(client, other, name="Чужая")
    assert client.post("/api/agent/sessions", json={"llm_profile_id": profile.id, "persona_id": foreign["id"]}, headers=h).status_code == 404
