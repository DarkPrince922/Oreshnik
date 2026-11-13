from app.core.db import SessionLocal
from app.models import Runner

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"


def ssh_body(**over):
    body = {"name": "box", "kind": "ssh", "ssh_host": "10.0.0.5", "ssh_user": "root", "ssh_private_key": KEY}
    body.update(over)
    return body


def test_only_admin_mutates_runners(client, admin, operator, viewer):
    for h in (operator, viewer):
        assert client.post("/api/runners", headers=h, json={"name": "m", "kind": "mock"}).status_code == 403
        assert client.delete("/api/runners/1", headers=h).status_code == 403
        assert client.post("/api/runners/1/check", headers=h).status_code == 403
    assert client.post("/api/runners", headers=admin, json={"name": "m", "kind": "mock"}).status_code == 201
    assert client.get("/api/runners").status_code == 401


def test_ssh_runner_requires_connection_details(client, admin):
    assert client.post("/api/runners", headers=admin, json={"name": "x", "kind": "ssh"}).status_code == 422
    assert client.post("/api/runners", headers=admin, json=ssh_body(ssh_private_key="not a key")).status_code == 422
    assert client.post("/api/runners", headers=admin, json=ssh_body(ssh_host=None)).status_code == 422


def test_ssh_key_is_encrypted_and_never_returned(client, admin):
    r = client.post("/api/runners", headers=admin, json=ssh_body())
    assert r.status_code == 201
    assert "PRIVATE KEY" not in r.text and r.json()["has_ssh_key"] is True
    assert "PRIVATE KEY" not in client.get("/api/runners", headers=admin).text
    with SessionLocal() as db:
        assert "PRIVATE KEY" not in db.query(Runner).one().ssh_key_enc


def test_non_admins_do_not_see_connection_details(client, admin, operator):
    client.post("/api/runners", headers=admin, json=ssh_body())
    as_admin = client.get("/api/runners", headers=admin).json()[0]
    as_operator = client.get("/api/runners", headers=operator).json()[0]
    assert as_admin["ssh_host"] == "10.0.0.5" and as_admin["ssh_user"] == "root"
    assert as_operator["ssh_host"] is None and as_operator["ssh_user"] is None and as_operator["has_ssh_key"] is False


def test_ssh_fields_are_dropped_for_non_ssh_kinds(client, admin):
    r = client.post("/api/runners", headers=admin, json={"name": "m", "kind": "mock", "ssh_host": "h", "ssh_user": "u", "ssh_private_key": KEY})
    assert r.status_code == 201 and r.json()["ssh_host"] is None and r.json()["has_ssh_key"] is False


def test_duplicate_runner_name_conflicts(client, admin):
    client.post("/api/runners", headers=admin, json={"name": "m", "kind": "mock"})
    assert client.post("/api/runners", headers=admin, json={"name": "m", "kind": "local"}).status_code == 409


def test_update_keeps_ssh_key_unless_replaced(client, admin):
    rid = client.post("/api/runners", headers=admin, json=ssh_body()).json()["id"]
    r = client.patch(f"/api/runners/{rid}", headers=admin, json={"name": "renamed", "ssh_host": "10.0.0.9"})
    assert r.status_code == 200 and r.json()["name"] == "renamed" and r.json()["ssh_host"] == "10.0.0.9"
    assert r.json()["has_ssh_key"] is True


def test_check_mock_runner_goes_online(client, admin):
    rid = client.post("/api/runners", headers=admin, json={"name": "m", "kind": "mock"}).json()["id"]
    assert client.get("/api/runners", headers=admin).json()[0]["status"] == "offline"
    r = client.post(f"/api/runners/{rid}/check", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "online" and r.json()["last_seen_at"]


def test_check_local_runner_reports_what_is_missing(client, admin, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    rid = client.post("/api/runners", headers=admin, json={"name": "l", "kind": "local"}).json()["id"]
    r = client.post(f"/api/runners/{rid}/check", headers=admin).json()
    assert r["status"] == "offline" and "strix CLI" in r["status_detail"] and "docker CLI" in r["status_detail"]


def test_check_when_worker_is_unreachable_marks_offline(client, admin, monkeypatch):
    from app.api import runners as runners_api

    class Boom:
        def delay(self, *_):
            raise ConnectionError("broker down")

    monkeypatch.setattr(runners_api, "runner_check", Boom())
    rid = client.post("/api/runners", headers=admin, json={"name": "m", "kind": "mock"}).json()["id"]
    r = client.post(f"/api/runners/{rid}/check", headers=admin).json()
    assert r["status"] == "offline" and "Воркер" in r["status_detail"]


def test_missing_runner_is_404(client, admin):
    assert client.post("/api/runners/999/check", headers=admin).status_code == 404
    assert client.delete("/api/runners/999", headers=admin).status_code == 404
    assert client.patch("/api/runners/999", headers=admin, json={"name": "x"}).status_code == 404
