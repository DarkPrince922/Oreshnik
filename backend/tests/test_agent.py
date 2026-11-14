"""«Агент»: the panel's chat API, the worker's turn, and agentd — together, against a fake Codex (tests/fake_codex.py).

The real Codex 0.169 was exercised separately against a fake Responses endpoint: provider/model through `-c`, the key
read from the env var named in `env_key`, `resume <thread_id>` carrying the history. What these tests pin is everything
around it.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.crypto import encrypt
from app.core.db import SessionLocal
from app.models import LLMProfile, Provider
from app.models.agent import AgentEvent, AgentSession, AgentSessionStatus
from app.services import agent as agent_service
from tests.conftest import auth_headers, make_profile, make_user

AGENTD_PATH = Path(__file__).resolve().parents[2] / "agentd" / "main.py"
TOKEN = "agent-test-token"


@pytest.fixture
def agentd(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("agentd_main", AGENTD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = Path(__file__).parent / "fake_codex.py"
    monkeypatch.setattr(module, "CODEX_BIN", str(fake))
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "agent-data")
    monkeypatch.setattr(module, "TOKEN", TOKEN)
    monkeypatch.setattr(module, "STOP_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(module, "PING_SECONDS", 0.2)
    return module


@pytest.fixture
def wired(agentd, monkeypatch):
    """The panel talking to the real agentd app (in-process) that runs the fake Codex."""
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    monkeypatch.setattr(
        agent_service, "agent_client",
        lambda timeout=10.0: TestClient(agentd.app, base_url="http://agent", headers={"Authorization": f"Bearer {TOKEN}"}),
    )
    return agentd


def openai_profile(user_id: int, name: str = "oa") -> LLMProfile:
    with SessionLocal() as db:
        p = LLMProfile(user_id=user_id, name=name, provider=Provider.openai, model="gpt-5", api_key_enc=encrypt("sk-live-key-9999"), api_key_hint="…9999")
        db.add(p)
        db.commit()
        db.refresh(p)
        return p


def op_user(client, email="op@example.com"):
    user = make_user(email)
    return user, auth_headers(client, email)


def events(session_id: int) -> list[AgentEvent]:
    with SessionLocal() as db:
        return list(db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.seq))


def load(session_id: int) -> AgentSession:
    with SessionLocal() as db:
        return db.get(AgentSession, session_id)


def new_session(client, headers, profile) -> dict:
    r = client.post("/api/agent/sessions", json={"llm_profile_id": profile.id}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def wait_idle(session_id: int, timeout: float = 15.0) -> AgentSession:
    end = time.time() + timeout
    while time.time() < end:
        s = load(session_id)
        if s.status == AgentSessionStatus.idle:
            return s
        time.sleep(0.05)
    raise AssertionError("session did not become idle")


# ── API ────────────────────────────────────────────────────────────────────

def test_disabled_without_token(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_token", "")
    user, h = op_user(client)
    r = client.post("/api/agent/sessions", json={"llm_profile_id": openai_profile(user.id).id}, headers=h)
    assert r.status_code == 503
    assert client.get("/api/agent/health", headers=h).json()["ok"] is False


def test_viewer_and_anonymous_are_refused(client, viewer):
    assert client.get("/api/agent/sessions", headers=viewer).status_code == 403
    assert client.get("/api/agent/sessions").status_code == 401


def test_claude_profile_is_refused_with_a_reason(client, wired):
    user, h = op_user(client)
    r = client.post("/api/agent/sessions", json={"llm_profile_id": make_profile(user.id).id}, headers=h)
    assert r.status_code == 422 and "Responses" in r.json()["detail"]


def test_someone_elses_profile_and_session_are_invisible(client, wired):
    a, ha = op_user(client, "a@example.com")
    b, hb = op_user(client, "b@example.com")
    assert client.post("/api/agent/sessions", json={"llm_profile_id": openai_profile(a.id).id}, headers=hb).status_code == 404
    s = new_session(client, ha, openai_profile(a.id, "mine"))
    assert client.get(f"/api/agent/sessions/{s['id']}", headers=hb).status_code == 404
    assert client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "hi"}, headers=hb).status_code == 404
    assert client.get("/api/agent/sessions", headers=hb).json() == []


def test_a_turn_stores_the_whole_conversation_and_resumes_the_thread(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    r = client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "first"}, headers=h)
    assert r.status_code == 202
    first = wait_idle(s["id"])
    assert first.thread_id and first.turn_count == 1 and first.last_error is None
    assert first.title == "first"  # the title is the first message
    kinds = [e.kind for e in events(s["id"])]
    assert kinds[0] == "user" and "thread.started" in kinds and kinds[-1] == "turn.completed"
    assert [e.seq for e in events(s["id"])] == list(range(len(kinds)))

    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "second"}, headers=h)
    second = wait_idle(s["id"])
    assert second.thread_id == first.thread_id  # same Codex thread: `resume <id>` was used
    assert second.turn_count == 2
    texts = [e.payload["item"]["text"] for e in events(s["id"]) if e.kind == "item.completed" and e.payload["item"]["type"] == "agent_message"]
    assert texts == ["echo: first", "echo: second"]


def test_what_codex_is_given(client, wired):
    """Provider, model and key reach Codex the way the real one needs them; nothing of the panel does."""
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "report"}, headers=h)
    wait_idle(s["id"])
    report = json.loads([e for e in events(s["id"]) if e.kind == "item.completed"][-1].payload["item"]["text"])
    argv = report["argv"]
    assert argv[0] == "exec" and "--json" in argv and "--dangerously-bypass-approvals-and-sandbox" in argv
    assert 'model="gpt-5"' in argv and 'model_provider="oreshnik"' in argv
    assert any(a.startswith("model_providers.oreshnik=") and 'base_url = "https://api.openai.com/v1"' in a and 'wire_api = "responses"' in a for a in argv)
    assert argv[-2:] == ["--", "report"]
    assert report["key_env"] == "[ключ скрыт]"  # the process did get the key (that is what was redacted)
    assert report["leaked"] == []
    assert Path(report["codex_home"]).is_dir() and report["cwd"].endswith("/work")
    # the key is never stored in the history
    assert "sk-live-key-9999" not in json.dumps([e.payload for e in events(s["id"])])


def test_custom_profile_uses_its_own_base_url(client, wired):
    user, h = op_user(client)
    with SessionLocal() as db:
        p = LLMProfile(user_id=user.id, name="gw", provider=Provider.custom, model="my/model", base_url="http://gw.internal/v1", api_key_enc=encrypt("k" * 20), api_key_hint="…kkkk")
        db.add(p)
        db.commit()
        db.refresh(p)
    s = new_session(client, h, p)
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "report"}, headers=h)
    wait_idle(s["id"])
    report = json.loads([e for e in events(s["id"]) if e.kind == "item.completed"][-1].payload["item"]["text"])
    assert any('base_url = "http://gw.internal/v1"' in a for a in report["argv"]) and 'model="my/model"' in report["argv"]


def test_a_prompt_that_looks_like_an_option_stays_a_prompt(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "--help"}, headers=h)
    wait_idle(s["id"])
    assert [e.payload["item"]["text"] for e in events(s["id"]) if e.kind == "item.completed" and e.payload["item"]["type"] == "agent_message"] == ["echo: --help"]


def test_a_failed_turn_is_reported_and_the_chat_stays_usable(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "fail"}, headers=h)
    done = wait_idle(s["id"])
    assert done.last_error == "boom"
    assert events(s["id"])[-1].kind == "turn.error"
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "again"}, headers=h)
    assert wait_idle(s["id"]).last_error is None


def test_a_second_message_while_running_is_refused(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    with SessionLocal() as db:  # a turn in progress
        db.get(AgentSession, s["id"]).status = AgentSessionStatus.running
        db.commit()
    assert client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "x"}, headers=h).status_code == 409
    assert client.delete(f"/api/agent/sessions/{s['id']}", headers=h).status_code == 409
    assert events(s["id"]) == []  # the refused message left no trace


@pytest.fixture
def live(agentd, monkeypatch):
    """agentd served by a real uvicorn on a local port, reached over real HTTP: the only way to see events *while* a turn runs."""
    import socket
    import threading

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(agentd.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    monkeypatch.setattr(settings, "agent_url", f"http://127.0.0.1:{port}")
    yield agentd
    server.should_exit = True
    thread.join(10)


def test_stop_interrupts_a_running_turn(client, live):
    import threading

    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    sent = threading.Thread(target=lambda: client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "hang"}, headers=h))
    sent.start()  # the worker runs inline here, so the message request lasts as long as the turn
    deadline = time.time() + 10
    while time.time() < deadline and not any(e.kind == "item.started" for e in events(s["id"])):
        time.sleep(0.05)
    assert any(e.kind == "item.started" for e in events(s["id"])), "the turn never started"
    assert load(s["id"]).status == AgentSessionStatus.running
    assert client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h).json()["cancel_requested"] is True
    sent.join(30)
    done = wait_idle(s["id"])
    assert done.last_error is None and done.cancel_requested is False
    assert events(s["id"])[-1].kind == "turn.stopped"


def test_a_silent_long_command_keeps_the_turn_alive(client, live, monkeypatch):
    """No events for a while must not look like a lost worker: agentd pings and the worker moves the heartbeat."""
    import threading

    from app.worker.tasks import reap_stale_agent_turns

    monkeypatch.setattr(settings, "agent_stale_seconds", 30)
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    sent = threading.Thread(target=lambda: client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "hang"}, headers=h))
    sent.start()
    deadline = time.time() + 10
    while time.time() < deadline and not any(e.kind == "item.started" for e in events(s["id"])):
        time.sleep(0.05)
    before = load(s["id"]).heartbeat_at
    time.sleep(0.8)  # several pings (PING_SECONDS = 0.2)
    assert load(s["id"]).heartbeat_at > before
    assert reap_stale_agent_turns() == 0
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    sent.join(30)


def test_big_output_is_cut_before_it_is_stored(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "bigout"}, headers=h)
    wait_idle(s["id"])
    out = [e for e in events(s["id"]) if e.kind == "item.completed"][0].payload["item"]["aggregated_output"]
    assert len(out) < 21_000 and "обрезано" in out


def test_unreachable_agent_container_is_an_error_not_a_hang(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    monkeypatch.setattr(settings, "agent_url", "http://127.0.0.1:1")
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "hi"}, headers=h)
    done = wait_idle(s["id"])
    assert "Нет связи" in done.last_error


def test_a_lost_worker_is_reaped(client, wired, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.worker.tasks import reap_stale_agent_turns

    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    with SessionLocal() as db:
        row = db.get(AgentSession, s["id"])
        row.status = AgentSessionStatus.running
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=settings.agent_stale_seconds + 5)
        db.commit()
    assert reap_stale_agent_turns() == 1
    after = load(s["id"])
    assert after.status == AgentSessionStatus.idle and "Воркер потерян" in after.last_error
    assert reap_stale_agent_turns() == 0


def test_delete_removes_session_and_history(client, wired, agentd):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "hi"}, headers=h)
    wait_idle(s["id"])
    key = load(s["id"]).key
    assert (agentd.DATA_DIR / key).is_dir()
    assert client.delete(f"/api/agent/sessions/{s['id']}", headers=h).status_code == 204
    assert not (agentd.DATA_DIR / key).exists() and events(s["id"]) == []


def test_websocket_replays_and_resumes(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "hi"}, headers=h)
    wait_idle(s["id"])
    token = h["Authorization"].split()[1]
    total = len(events(s["id"]))
    with client.websocket_connect(f"/api/agent/sessions/{s['id']}/ws") as ws:
        ws.send_text(json.dumps({"token": token, "from": 0}))
        assert ws.receive_json() == {"type": "status", "status": "idle"}
        got = [ws.receive_json() for _ in range(total)]
    assert [g["seq"] for g in got] == list(range(total)) and got[0]["kind"] == "user"
    with client.websocket_connect(f"/api/agent/sessions/{s['id']}/ws") as ws:  # reconnect: only what was not seen
        ws.send_text(json.dumps({"token": token, "from": total - 1}))
        ws.receive_json()
        assert ws.receive_json()["seq"] == total - 1


def test_websocket_rejects_a_bad_token_and_a_foreign_session(client, wired):
    a, ha = op_user(client, "a@example.com")
    _, hb = op_user(client, "b@example.com")
    s = new_session(client, ha, openai_profile(a.id))
    for token in ("nope", hb["Authorization"].split()[1]):
        with client.websocket_connect(f"/api/agent/sessions/{s['id']}/ws") as ws:
            ws.send_text(json.dumps({"token": token, "from": 0}))
            with pytest.raises(Exception):
                ws.receive_json()


# ── agentd on its own ───────────────────────────────────────────────────────

def turn_body(**kw):
    body = {"session_key": "k1", "prompt": "hi", "model": "m", "base_url": "http://x/v1", "api_key": "secret-key", "timeout_seconds": 30}
    body.update(kw)
    return body


def post_turn(agentd, **kw):
    c = TestClient(agentd.app)
    r = c.post("/turn", json=turn_body(**kw), headers={"Authorization": f"Bearer {TOKEN}"})
    return r, [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_agentd_needs_the_token(agentd):
    c = TestClient(agentd.app)
    assert c.post("/turn", json=turn_body()).status_code == 401
    assert c.post("/turn", json=turn_body(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/stop", json={"session_key": "k"}).status_code == 401
    assert c.delete("/sessions/k").status_code == 401


def test_agentd_refuses_an_unset_token(agentd, monkeypatch):
    monkeypatch.setattr(agentd, "TOKEN", "")
    assert TestClient(agentd.app).post("/turn", json=turn_body(), headers={"Authorization": "Bearer "}).status_code == 401


@pytest.mark.parametrize("kw", [{"session_key": "../etc"}, {"session_key": "a/b"}, {"thread_id": "x; rm -rf /"}, {"model": "a b"}, {"model": "--oss"}])
def test_agentd_rejects_unsafe_identifiers(agentd, kw):
    r, _ = post_turn(agentd, **kw)
    assert r.status_code in (404, 422)


def test_agentd_ends_the_stream_with_an_exit_line_and_hides_the_key(agentd):
    r, evs = post_turn(agentd, prompt="fail")
    assert evs[-1]["type"] == "agentd.exit" and evs[-1]["code"] == 1
    assert "secret-key" not in r.text


def test_agentd_kills_a_turn_that_runs_too_long(agentd):
    start = time.time()
    _, evs = post_turn(agentd, prompt="hang", timeout_seconds=10)
    assert evs[-1]["type"] == "agentd.exit" and evs[-1]["timed_out"] is True
    assert any(e["type"] == "agentd.ping" for e in evs)
    assert time.time() - start < 40


def test_toml_quoting_cannot_be_broken_out_of(agentd):
    body = agentd.Turn(**turn_body(base_url='http://x/v1"\nwire_api = "evil', model="m"))
    provider = next(a for a in agentd.build_argv(body) if a.startswith("model_providers.oreshnik="))
    assert provider.count("\n") == 0 and '\\"' in provider


# ── health: Codex must really start ─────────────────────────────────────────

def script(tmp_path, body: str, name: str = "codex"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_health_is_ok_when_codex_starts(agentd, tmp_path, monkeypatch):
    monkeypatch.setattr(agentd, "CODEX_BIN", script(tmp_path, "echo codex-cli 0.169.0\n"))
    body = TestClient(agentd.app).get("/health").json()
    assert body == {"ok": True, "codex": True, "codex_detail": "codex-cli 0.169.0"}


def test_health_catches_a_launcher_whose_binary_is_missing(agentd, tmp_path, monkeypatch):
    """The real failure seen after a build: `codex` is on the PATH, but its platform package was never installed."""
    msg = "Error: Missing optional dependency @openai/codex-linux-x64. Reinstall Codex: npm install -g @openai/codex@latest"
    monkeypatch.setattr(agentd, "CODEX_BIN", script(tmp_path, f"echo '{msg}' >&2\nexit 1\n"))
    body = TestClient(agentd.app).get("/health").json()
    assert body["codex"] is False and "Missing optional dependency" in body["codex_detail"]


def test_health_reports_a_missing_binary(agentd, monkeypatch):
    monkeypatch.setattr(agentd, "CODEX_BIN", "definitely-not-installed-codex")
    body = TestClient(agentd.app).get("/health").json()
    assert body["codex"] is False and "не найден" in body["codex_detail"]


def test_health_does_not_start_codex_on_every_poll(agentd, tmp_path, monkeypatch):
    counter = tmp_path / "runs"
    monkeypatch.setattr(agentd, "CODEX_BIN", script(tmp_path, f"echo x >> {counter}\necho v\n"))
    client = TestClient(agentd.app)
    for _ in range(3):
        assert client.get("/health").json()["codex"] is True
    assert len(counter.read_text().splitlines()) == 1


def test_the_panel_shows_why_codex_does_not_start(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(wired, "CODEX_BIN", script(tmp_path, "echo 'Missing optional dependency @openai/codex-linux-x64' >&2\nexit 1\n"))
    health = agent_service.agent_health()
    assert health["ok"] is False and "не запускается" in health["detail"] and "Missing optional dependency" in health["detail"]


def test_the_panel_says_the_agent_is_fine_when_codex_starts(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(wired, "CODEX_BIN", script(tmp_path, "echo codex-cli 0.169.0\n"))
    assert agent_service.agent_health() == {"ok": True, "detail": "Агент на связи"}


# ── gateways refuse Codex's non-function tools ──────────────────────────────

def report_argv(client, headers, profile) -> list[str]:
    s = new_session(client, headers, profile)
    client.post(f"/api/agent/sessions/{s['id']}/messages", json={"text": "report"}, headers=headers)
    wait_idle(s["id"])
    return json.loads([e for e in events(s["id"]) if e.kind == "item.completed"][-1].payload["item"]["text"])["argv"]


FUNCTION_ONLY = ["-c", 'web_search="disabled"', "--disable", "multi_agent", "--disable", "goals", "--disable", "image_generation"]


def has_run(argv: list[str], run: list[str]) -> bool:
    return any(argv[i:i + len(run)] == run for i in range(len(argv)))


def test_a_gateway_gets_function_tools_only(client, wired):
    """The real failure: 400 "This endpoint supports function tools only" from a custom gateway."""
    user, h = op_user(client)
    with SessionLocal() as db:
        p = LLMProfile(user_id=user.id, name="gw", provider=Provider.custom, model="m", base_url="http://gw.internal/v1", api_key_enc=encrypt("k" * 20), api_key_hint="…kkkk")
        db.add(p)
        db.commit()
        db.refresh(p)
    assert has_run(report_argv(client, h, p), FUNCTION_ONLY)


def test_openai_itself_keeps_all_of_codexs_tools(client, wired):
    user, h = op_user(client)
    argv = report_argv(client, h, openai_profile(user.id))
    assert "--disable" not in argv and 'web_search="disabled"' not in argv


def test_the_setting_overrides_the_automatic_choice(client, wired, monkeypatch):
    user, h = op_user(client)
    monkeypatch.setattr(settings, "agent_tools", "functions")
    assert has_run(report_argv(client, h, openai_profile(user.id, "a")), FUNCTION_ONLY)
    monkeypatch.setattr(settings, "agent_tools", "full")
    with SessionLocal() as db:
        p = LLMProfile(user_id=user.id, name="gw2", provider=Provider.custom, model="m", base_url="http://gw.internal/v1", api_key_enc=encrypt("k" * 20), api_key_hint="…kkkk")
        db.add(p)
        db.commit()
        db.refresh(p)
    assert "--disable" not in report_argv(client, h, p)


def test_resuming_keeps_the_function_only_flags_before_the_thread_id(agentd):
    body = agentd.Turn(**turn_body(thread_id="thread-1", function_tools_only=True))
    argv = agentd.build_argv(body)
    assert argv[:3] == [agentd.CODEX_BIN, "exec", "resume"] and has_run(argv, FUNCTION_ONLY)
    assert argv.index("thread-1") > argv.index("image_generation") and argv[-2:] == ["--", "hi"]


def test_agent_tools_setting_is_validated():
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(_env_file=None).agent_tools == "auto"
    assert Settings(_env_file=None, agent_tools=" Functions ").agent_tools == "functions"
    with pytest.raises(ValidationError, match="AGENT_TOOLS"):
        Settings(_env_file=None, agent_tools="some")
