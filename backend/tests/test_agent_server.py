"""«Агент»: who may have an OpenHands server, the user's own server (and nobody else's), checking it and installing OpenHands."""
import pytest
from cryptography.fernet import Fernet  # noqa: F401

from app.core.crypto import decrypt
from app.core.db import SessionLocal
from app.models import Role
from app.models.agent_server import AgentServer
from app.services import openhands
from app.worker import tasks
from tests.test_agent import op_user

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

PROBE_OK = """uid=0
user=root
root=1
os_id=ubuntu
os_version=24.04
apt=1
systemd=1
node=v24.11.0
uv=uv 0.9.5
canvas=1.41.0
service=active
http=200
mem_mb=3922
free_mb=51000
"""


def body(**over):
    return {"ssh_host": "203.0.113.9", "ssh_port": 22, "ssh_user": "root", "ssh_password": "s3cret-pass", **over}


# ── the probe and the plan ─────────────────────────────────────────────────

def test_a_ready_server_is_online():
    state = openhands.parse_probe(PROBE_OK)
    assert state.root and state.node_major == 24 and state.canvas_version == "1.41.0" and state.answers
    status, detail = openhands.describe(state)
    assert status == "online" and "OpenHands 1.41.0 работает" in detail


def test_what_a_probe_says_when_something_is_missing():
    bare = openhands.parse_probe("uid=0\nuser=root\nroot=1\nos_id=debian\napt=1\nsystemd=1\nnode=\nuv=\ncanvas=\nhttp=000\n")
    assert openhands.describe(bare) == ("offline", "OpenHands не установлен · root@debian — нажмите «Установить»")
    stopped = openhands.parse_probe(PROBE_OK.replace("http=200", "http=000").replace("service=active", "service=failed"))
    assert openhands.describe(stopped)[0] == "offline" and "не отвечает" in openhands.describe(stopped)[1]
    no_root = openhands.parse_probe(PROBE_OK.replace("root=1", "root=0").replace("user=root", "user=bob"))
    assert openhands.describe(no_root)[0] == "error" and "root" in openhands.describe(no_root)[1]


def test_the_plan_installs_only_what_is_missing_and_keeps_the_key_off_the_command_line():
    bare = openhands.parse_probe("uid=0\nuser=root\nroot=1\napt=1\nsystemd=1\nnode=v18.0.0\nuv=\ncanvas=\n")
    titles = [s.title for s in openhands.plan(bare, "1.41.0", "KEY123")]
    assert titles[0].startswith("Базовые") and any("Node.js 24" in t for t in titles) and any(t.startswith("uv") for t in titles)
    ready = [s.title for s in openhands.plan(openhands.parse_probe(PROBE_OK), "1.41.0", "KEY123")]
    assert not any("Node.js" in t or t.startswith("uv") for t in ready)  # already there
    service = next(s.script for s in openhands.plan(bare, "1.41.0", "KEY123") if s.title.startswith("Служба"))
    assert "KEY123" in service and "OH_BIND_HOST=127.0.0.1" in service and "User=root" in service and "@openhands/agent-canvas" not in service
    assert any("@openhands/agent-canvas@1.41.0" in s.script for s in openhands.plan(bare, "1.41.0", "KEY123"))


def test_the_plan_refuses_what_it_cannot_do():
    with pytest.raises(ValueError, match="root"):
        openhands.plan(openhands.parse_probe("root=0\nsystemd=1\napt=1\n"), "1.41.0", "k")
    with pytest.raises(ValueError, match="systemd"):
        openhands.plan(openhands.parse_probe("root=1\nsystemd=0\napt=1\n"), "1.41.0", "k")
    with pytest.raises(ValueError, match="apt"):
        openhands.plan(openhands.parse_probe("root=1\nsystemd=1\napt=0\nnode=\nuv=\n"), "1.41.0", "k")


# ── access ─────────────────────────────────────────────────────────────────

def test_only_users_an_administrator_allowed(client):
    _, admin = op_user(client, "admin@example.com", Role.admin)
    op, h = op_user(client, "op@example.com")
    _, viewer = op_user(client, "v@example.com", Role.viewer)
    assert client.get("/api/agent-server", headers=h).status_code == 403
    assert client.get("/api/agent-server", headers=viewer).status_code == 403
    assert client.get("/api/agent-server", headers=admin).status_code == 200  # administrators always may
    r = client.patch(f"/api/users/{op.id}", json={"agent_access": True}, headers=admin)
    assert r.status_code == 200 and r.json()["agent_access"] is True
    assert client.get("/api/agent-server", headers=h).json() is None  # allowed, no server yet
    assert client.get("/api/auth/me", headers=h).json()["agent_access"] is True


def test_each_user_has_their_own_server_and_its_secrets_stay_hidden(client):
    _, a = op_user(client, "a@example.com", Role.admin)
    _, b = op_user(client, "b@example.com", Role.admin)
    out = client.put("/api/agent-server", json=body(), headers=a).json()
    assert out["status"] == "new" and out["has_password"] and not out["has_key"]
    assert "s3cret" not in str(out)
    assert client.get("/api/agent-server", headers=b).json() is None  # B does not see A's
    with SessionLocal() as db:
        row = db.query(AgentServer).one()
        assert decrypt(row.ssh_password_enc) == "s3cret-pass"
    assert client.put("/api/agent-server", json=body(ssh_password=None, clear_password=True), headers=a).status_code == 422
    assert client.put("/api/agent-server", json=body(ssh_host="bad host; rm"), headers=a).status_code == 422


# ── checking and installing ────────────────────────────────────────────────

def test_first_check_shows_the_key_then_it_must_be_confirmed(client, monkeypatch):
    _, h = op_user(client, "a@example.com", Role.admin)
    client.put("/api/agent-server", json=body(), headers=h)
    monkeypatch.setattr(openhands, "fetch_host_key", lambda host, port: KEY)
    out = client.post("/api/agent-server/check", headers=h).json()
    assert out["status"] == "new" and out["pending_fingerprint"] and "Подтвердите ключ" in out["status_detail"]
    assert client.post("/api/agent-server/install", headers=h).status_code == 409  # not before the key is confirmed
    assert client.post("/api/agent-server/trust-host-key", json={"fingerprint": "SHA256:wrong-one-xxxx"}, headers=h).status_code == 409

    monkeypatch.setattr(openhands, "check", lambda server: openhands.CheckResult("online", "OpenHands 1.41.0 работает", state=openhands.parse_probe(PROBE_OK)))
    out = client.post("/api/agent-server/trust-host-key", json={"fingerprint": out["pending_fingerprint"]}, headers=h).json()
    assert out["host_key_fingerprint"] and out["pending_fingerprint"] is None and out["status"] == "online" and out["openhands_version"] == "1.41.0"


def test_install_runs_on_the_worker_and_keeps_the_panels_key(client, monkeypatch):
    _, h = op_user(client, "a@example.com", Role.admin)
    client.put("/api/agent-server", json=body(), headers=h)
    with SessionLocal() as db:
        db.query(AgentServer).update({"ssh_host_key": KEY})
        db.commit()

    def fake_install(server, emit, version=None):
        emit("▶ Шаг 1/1: OpenHands")
        return openhands.InstallOutcome("online", "OpenHands 1.41.0 работает", api_key_enc=openhands.encrypt("panel-key"), version="1.41.0")

    monkeypatch.setattr(openhands, "install", fake_install)
    out = client.post("/api/agent-server/install", headers=h).json()  # tasks run inline in tests
    assert out["status"] == "online" and out["openhands_version"] == "1.41.0" and not out["installing"]
    assert "OpenHands" in client.get("/api/agent-server/install-log", headers=h).json()["log"]
    with SessionLocal() as db:
        assert decrypt(db.query(AgentServer).one().api_key_enc) == "panel-key"
    assert tasks.agent_server_install.name == "oreshnik.agent_server_install"


def test_moving_the_server_forgets_its_key(client):
    _, h = op_user(client, "a@example.com", Role.admin)
    client.put("/api/agent-server", json=body(), headers=h)
    with SessionLocal() as db:
        db.query(AgentServer).update({"ssh_host_key": KEY, "api_key_enc": "x"})
        db.commit()
    out = client.put("/api/agent-server", json=body(ssh_host="203.0.113.10", ssh_password=None), headers=h).json()
    assert out["host_key_fingerprint"] is None and out["status"] == "new"
    assert client.delete("/api/agent-server", headers=h).status_code == 204
    assert client.get("/api/agent-server", headers=h).json() is None
