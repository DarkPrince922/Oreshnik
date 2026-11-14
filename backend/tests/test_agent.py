"""«Агент»: the panel's chat API, the worker's turn, and the whole path to agentd — against a stand-in for `codex app-server` that speaks the
protocol the way the real Codex 0.169 did in the recorded exchanges (tests/fake_appserver.py), served by a real agentd (see test_agentd.py)."""

import json
import threading
import time

import pytest

from app.core.config import settings
from app.core.crypto import encrypt
from app.core.db import SessionLocal
from app.models import Finding, LLMProfile, Provider, Role, ScanStatus
from app.models.agent import AgentEvent, AgentLive, AgentSession, AgentSessionStatus
from app.services import agent as agent_service
from tests.conftest import auth_headers, make_profile, make_runner, make_scan, make_user
from tests.test_agentd import TOKEN, agentd  # noqa: F401  (the `agentd` fixture)


@pytest.fixture
def wired(agentd, monkeypatch):
    """The panel talking to the real agentd that runs the stand-in Codex."""
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    monkeypatch.setattr(settings, "agent_url", agentd.url)
    return agentd


def openai_profile(user_id: int, name: str = "oa", models=()) -> LLMProfile:
    with SessionLocal() as db:
        p = LLMProfile(user_id=user_id, name=name, provider=Provider.openai, model="gpt-5", models=list(models), api_key_enc=encrypt("sk-live-key-9999"), api_key_hint="…9999")
        db.add(p)
        db.commit()
        db.refresh(p)
        return p


def custom_gateway(user_id: int, name: str = "gw") -> LLMProfile:
    with SessionLocal() as db:
        p = LLMProfile(user_id=user_id, name=name, provider=Provider.custom, model="m", base_url="http://gw.internal/v1", api_key_enc=encrypt("k" * 20), api_key_hint="…kkkk")
        db.add(p)
        db.commit()
        db.refresh(p)
        return p


def op_user(client, email="op@example.com", role: Role = Role.operator):
    user = make_user(email, role=role)
    return user, auth_headers(client, email)


def events(session_id: int) -> list[AgentEvent]:
    with SessionLocal() as db:
        return list(db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.seq))


def kinds_of(session_id: int) -> list[str]:
    return [e.kind for e in events(session_id)]


def load(session_id: int) -> AgentSession:
    with SessionLocal() as db:
        return db.get(AgentSession, session_id)


def new_session(client, headers, profile, **over) -> dict:
    r = client.post("/api/agent/sessions", json={"llm_profile_id": profile.id, **over}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def wait_for(predicate, timeout: float = 20.0, what: str = "the condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"{what} never came")


def wait_idle(session_id: int, timeout: float = 25.0) -> AgentSession:
    return wait_for(lambda: (s := load(session_id)).status == AgentSessionStatus.idle and s, timeout, "idle")


def send(client, headers, session_id: int, text: str, path: str = "messages"):
    return client.post(f"/api/agent/sessions/{session_id}/{path}", json={"text": text}, headers=headers)


class Bg:
    """A request in a background thread: the worker runs inline here, so a message request lasts as long as the turn."""

    def __init__(self, fn):
        self.result = None
        self.thread = threading.Thread(target=lambda: setattr(self, "result", fn()), daemon=True)
        self.thread.start()

    def join(self, timeout: float = 30.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the request did not end"
        return self.result


def start_turn(client, headers, session_id: int, text: str) -> Bg:
    return Bg(lambda: send(client, headers, session_id, text))


def has_event(session_id: int, kind: str):
    return lambda: next((e for e in events(session_id) if e.kind == kind), None)


def report(client, headers, session_id: int) -> dict:
    send(client, headers, session_id, "report")
    wait_idle(session_id)
    last = [e for e in events(session_id) if e.kind == "item.completed" and e.payload["item"]["type"] == "agent_message"][-1]
    return json.loads(last.payload["item"]["text"])


# ── access ──────────────────────────────────────────────────────────────────

def test_disabled_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_token", "")
    user, h = op_user(client)
    assert client.post("/api/agent/sessions", json={"llm_profile_id": openai_profile(user.id).id}, headers=h).status_code == 503
    assert client.get("/api/agent/health", headers=h).json()["ok"] is False


def test_viewer_and_anonymous_are_refused(client, viewer):
    assert client.get("/api/agent/sessions", headers=viewer).status_code == 403
    assert client.get("/api/agent/sessions").status_code == 401


def test_a_claude_profile_is_refused_with_a_reason(client, wired):
    user, h = op_user(client)
    r = client.post("/api/agent/sessions", json={"llm_profile_id": make_profile(user.id).id}, headers=h)
    assert r.status_code == 422 and "Responses" in r.json()["detail"]


def test_someone_elses_profile_and_session_are_invisible(client, wired):
    a, ha = op_user(client, "a@example.com")
    b, hb = op_user(client, "b@example.com")
    assert client.post("/api/agent/sessions", json={"llm_profile_id": openai_profile(a.id).id}, headers=hb).status_code == 404
    s = new_session(client, ha, openai_profile(a.id, "mine"))
    sid = s["id"]
    for method, path, body in (("GET", "", None), ("PATCH", "", {"title": "x"}), ("POST", "/messages", {"text": "hi"}), ("POST", "/steer", {"text": "hi"}),
                               ("POST", "/stop", None), ("POST", "/fork", None), ("POST", "/actions", {"action": "compact"}), ("GET", "/events", None),
                               ("POST", "/approvals/1", {"decision": "accept"}), ("GET", "/files", None), ("GET", "/files/content?path=a", None),
                               ("GET", "/files/download?path=a", None), ("PUT", "/files?path=a", None), ("GET", "/files/archive", None),
                               ("POST", "/files/clone", {"url": "https://h/o/r"}), ("DELETE", "", None)):
        r = client.request(method, f"/api/agent/sessions/{sid}{path}", json=body, headers=hb)
        assert r.status_code == 404, (method, path, r.status_code)
    assert client.get("/api/agent/sessions", headers=hb).json() == []


# ── a turn ──────────────────────────────────────────────────────────────────

def test_a_turn_stores_the_conversation_and_the_thread_goes_on(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    assert send(client, h, s["id"], "first").status_code == 202
    first = wait_idle(s["id"])
    assert first.thread_id and first.turn_count == 1 and first.last_error is None and first.title == "first"
    got = events(s["id"])
    assert [e.seq for e in got] == list(range(len(got)))
    assert [e.kind for e in got] == ["user", "turn.started", "item.completed", "turn.completed"]
    assert got[0].payload == {"text": "first"} and got[2].payload["item"]["text"] == "Привет! Это ответ."
    done = got[3].payload
    assert done["status"] == "completed" and done["usage"]["input_tokens"] > 0 and done["usage"]["output_tokens"] > 0
    send(client, h, s["id"], "second")
    second = wait_idle(s["id"])
    assert second.thread_id == first.thread_id and second.turn_count == 2


def test_the_api_key_never_reaches_the_history(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    info = report(client, h, s["id"])
    assert info["key_env"] == "[ключ скрыт]"  # the process did get the key: that is what was redacted
    assert "sk-live-key-9999" not in json.dumps([e.payload for e in events(s["id"])])


def test_what_codex_is_given_comes_from_the_session(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id, models=["gpt-5", "o3"]), llm_model="o3", instructions="Всегда кратко.", reasoning_effort="low", approval_mode="ask")
    seen = report(client, h, s["id"])["seen"]
    assert seen["model"] == "o3" and seen["developerInstructions"] == "Всегда кратко." and seen["effort"] == "low" and seen["approvalPolicy"] == "untrusted"
    assert seen["sandbox"] == "danger-full-access" and seen["method"] == "thread/start"


def test_a_big_command_output_is_cut_before_it_is_stored(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "bigout")
    wait_idle(s["id"])
    out = next(e.payload["item"]["aggregated_output"] for e in events(s["id"]) if e.kind == "item.completed")
    assert len(out) < 21_000 and "обрезано" in out


def test_a_failed_turn_is_reported_and_the_chat_stays_usable(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "fail")
    done = wait_idle(s["id"])
    assert done.last_error == "model exploded" and kinds_of(s["id"])[-1] == "turn.error"
    send(client, h, s["id"], "again")
    assert wait_idle(s["id"]).last_error is None


def test_a_crashing_codex_is_reported_and_the_next_message_works(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "crash")
    assert "завершился" in wait_idle(s["id"]).last_error
    send(client, h, s["id"], "again")
    assert wait_idle(s["id"]).last_error is None


def test_an_unreachable_agent_container_is_an_error_not_a_hang(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    monkeypatch.setattr(settings, "agent_url", "http://127.0.0.1:1")
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "hi")
    assert "Нет связи" in wait_idle(s["id"]).last_error


def test_a_lost_worker_is_reaped_and_what_it_was_writing_is_dropped(client, wired):
    from datetime import datetime, timedelta, timezone

    from app.worker.tasks import reap_stale_agent_turns

    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    with SessionLocal() as db:
        row = db.get(AgentSession, s["id"])
        row.status = AgentSessionStatus.running
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=settings.agent_stale_seconds + 5)
        db.add(AgentLive(session_id=s["id"], key="m1", payload={"id": "m1", "type": "agent_message", "text": "half"}, rev=3))
        db.commit()
    assert reap_stale_agent_turns() == 1
    after = load(s["id"])
    assert after.status == AgentSessionStatus.idle and "Воркер потерян" in after.last_error
    with SessionLocal() as db:
        assert db.query(AgentLive).count() == 0
    assert reap_stale_agent_turns() == 0


def test_a_message_while_a_turn_runs_is_refused_and_leaves_no_trace(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "slow")
    wait_for(has_event(s["id"], "turn.started"), what="the turn")
    before = len(events(s["id"]))
    assert send(client, h, s["id"], "x").status_code == 409
    assert client.delete(f"/api/agent/sessions/{s['id']}", headers=h).status_code == 409
    assert len(events(s["id"])) == before
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    bg.join()


# ── live view ───────────────────────────────────────────────────────────────

def test_what_is_being_written_is_visible_live_and_gone_when_the_item_is_finished(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "slow")

    def live_text():
        with SessionLocal() as db:
            row = db.query(AgentLive).filter(AgentLive.session_id == s["id"], AgentLive.key == "m1").first()
            return row.payload["text"] if row else None

    first = wait_for(live_text, what="a live snapshot")
    later = wait_for(lambda: (t := live_text()) and len(t) > len(first) and t, what="a longer snapshot")
    assert later.startswith(first)
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    bg.join()
    with SessionLocal() as db:
        assert db.query(AgentLive).count() == 0


def collect_frames(client, token, session_id, until, timeout=25.0):
    """The frames the socket sends until `until(frame, all_frames)` holds. Read in a thread, so a frame that never comes fails the test
    instead of blocking it (the thread ends when the socket closes with the session)."""
    frames: list[dict] = []

    def read():
        try:
            with client.websocket_connect(f"/api/agent/sessions/{session_id}/ws") as ws:
                ws.send_text(json.dumps({"token": token, "from": 0}))
                while True:
                    frames.append(ws.receive_json())
        except Exception:
            pass

    threading.Thread(target=read, daemon=True).start()
    end = time.time() + timeout
    while time.time() < end:
        for index, frame in enumerate(list(frames)):
            if until(frame, frames[: index + 1]):
                return frames[: index + 1]
        time.sleep(0.05)
    raise AssertionError(f"the frame never came; got {[(f['type'], f.get('kind') or f.get('key')) for f in frames]}")


def test_the_socket_sends_live_text_growing_and_then_the_finished_event(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    token = h["Authorization"].split()[1]
    bg = start_turn(client, h, s["id"], "hello")
    frames = collect_frames(client, token, s["id"], lambda f, all_: f["type"] == "event" and f["kind"] == "turn.completed")
    bg.join()
    assert frames[0] == {"type": "status", "status": "running"} or frames[0]["type"] == "status"
    live = [f["payload"]["text"] for f in frames if f["type"] == "live" and f["key"] == "m1"]
    assert all(a in b for a, b in zip(live, live[1:]))
    assert any(f["type"] == "event" and f["kind"] == "item.completed" and f["payload"]["item"]["text"] == "Привет! Это ответ." for f in frames)
    seqs = [f["seq"] for f in frames if f["type"] == "event"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_a_late_client_gets_the_history_and_what_is_being_written_now(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    token = h["Authorization"].split()[1]
    bg = start_turn(client, h, s["id"], "slow")
    wait_for(lambda: len(events(s["id"])) >= 2 and has_event(s["id"], "turn.started")(), what="the turn")
    time.sleep(0.5)
    frames = collect_frames(client, token, s["id"], lambda f, all_: any(x["type"] == "live" and x["key"] == "m1" for x in all_))
    assert [f["kind"] for f in frames if f["type"] == "event"][:2] == ["user", "turn.started"]
    assert any(f["type"] == "live" and f["key"] == "m1" and f["payload"]["text"].startswith("слово0") for f in frames)
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    bg.join()


def test_the_socket_says_when_a_live_item_is_gone(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    token = h["Authorization"].split()[1]
    bg = start_turn(client, h, s["id"], "slow")
    watcher = Bg(lambda: collect_frames(client, token, s["id"], lambda f, all_: f["type"] == "live_end"))
    wait_for(lambda: has_event(s["id"], "turn.started")() and _live_rows(s["id"]), what="an unfinished item")
    time.sleep(0.6)  # the socket polls: let it see the item before the turn ends
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    bg.join()
    frames = watcher.join()
    assert any(f["type"] == "live" and f["key"] == "m1" for f in frames) and frames[-1] == {"type": "live_end", "key": "m1"}


def _live_rows(session_id: int) -> int:
    with SessionLocal() as db:
        return db.query(AgentLive).filter(AgentLive.session_id == session_id).count()


def test_the_socket_rejects_a_bad_token_and_a_foreign_session(client, wired):
    a, ha = op_user(client, "a@example.com")
    _, hb = op_user(client, "b@example.com")
    s = new_session(client, ha, openai_profile(a.id))
    for token in ("nope", hb["Authorization"].split()[1]):
        with client.websocket_connect(f"/api/agent/sessions/{s['id']}/ws") as ws:
            ws.send_text(json.dumps({"token": token, "from": 0}))
            with pytest.raises(Exception):
                ws.receive_json()


# ── questions to the user ──────────────────────────────────────────────────

def test_approving_a_command_in_the_ask_mode(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id), approval_mode="ask")
    bg = start_turn(client, h, s["id"], "run a command")
    asked = wait_for(has_event(s["id"], "approval.requested"), what="the question").payload
    assert asked["approval"]["kind"] == "command" and asked["approval"]["command"] == "echo hi-from-agent"
    assert not any(e.kind == "item.completed" and e.payload["item"]["type"] == "command_execution" for e in events(s["id"]))
    assert client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "accept"}, headers=h).status_code == 204
    bg.join()
    got = events(s["id"])
    assert any(e.kind == "approval.resolved" and e.payload["request_id"] == asked["request_id"] for e in got)
    assert next(e.payload["item"] for e in got if e.kind == "item.completed" and e.payload["item"]["type"] == "command_execution")["status"] == "completed"


def test_declining_keeps_the_command_from_running(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id), approval_mode="ask")
    bg = start_turn(client, h, s["id"], "run a command")
    asked = wait_for(has_event(s["id"], "approval.requested"), what="the question").payload
    client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "decline"}, headers=h)
    bg.join()
    assert next(e.payload["item"] for e in events(s["id"]) if e.kind == "item.completed" and e.payload["item"]["type"] == "command_execution")["status"] == "declined"


def test_a_file_change_waits_for_approval_and_its_diff_is_kept(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id), approval_mode="ask")
    bg = start_turn(client, h, s["id"], "patch this")
    asked = wait_for(has_event(s["id"], "approval.requested"), what="the question").payload
    assert asked["approval"]["kind"] == "file"
    client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "accept_session"}, headers=h)
    bg.join()
    got = {e.kind: e for e in events(s["id"])}
    assert "+++ b/hello.txt" in got["turn.diff"].payload["diff"]
    change = next(e.payload["item"] for e in events(s["id"]) if e.kind == "item.completed" and e.payload["item"]["type"] == "file_change")
    assert change["changes"][0]["path"] == "hello.txt" and change["changes"][0]["diff"] == "hi\nthere\n"


def test_the_agent_can_ask_a_question_and_the_answer_reaches_it(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "ask me")
    asked = wait_for(has_event(s["id"], "approval.requested"), what="the question").payload
    assert asked["approval"]["kind"] == "input" and asked["approval"]["questions"][0]["options"][1]["label"] == "синий"
    r = client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "answer", "answers": {"color": "синий"}}, headers=h)
    assert r.status_code == 204
    bg.join()
    assert any(e.kind == "item.completed" and e.payload["item"].get("text") == "выбрано: синий" for e in events(s["id"]))


def test_a_wrong_or_late_answer_is_refused(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id), approval_mode="ask")
    assert client.post(f"/api/agent/sessions/{s['id']}/approvals/1", json={"decision": "accept"}, headers=h).status_code == 404
    assert client.post(f"/api/agent/sessions/{s['id']}/approvals/1", json={"decision": "maybe"}, headers=h).status_code == 422
    bg = start_turn(client, h, s["id"], "run a command")
    asked = wait_for(has_event(s["id"], "approval.requested"), what="the question").payload
    assert client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "answer", "answers": {"a": "b"}}, headers=h).status_code == 422
    client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "accept"}, headers=h)
    bg.join()
    assert client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "accept"}, headers=h).status_code == 404


# ── steering and stopping ───────────────────────────────────────────────────

def test_an_instruction_can_be_added_to_the_turn_that_is_running(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "slow")
    wait_for(has_event(s["id"], "turn.started"), what="the turn")
    r = send(client, h, s["id"], "и ещё ура", "steer")
    assert r.status_code == 200 and r.json()["status"] == "running"
    bg.join()
    got = events(s["id"])
    assert [e.payload for e in got if e.kind == "user"] == [{"text": "slow"}, {"text": "и ещё ура", "steer": True}]
    assert next(e.payload["item"]["text"] for e in got if e.kind == "item.completed").endswith("(учёл: и ещё ура)")


def test_steering_needs_a_running_turn(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    assert send(client, h, s["id"], "x", "steer").status_code == 409
    assert send(client, h, s["id"], "   ", "steer").status_code == 422


def test_stop_ends_the_turn_and_says_so(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "slow")
    wait_for(has_event(s["id"], "turn.started"), what="the turn")
    assert client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h).json()["cancel_requested"] is True
    bg.join()
    done = load(s["id"])
    assert done.status == AgentSessionStatus.idle and done.last_error is None and done.cancel_requested is False
    assert kinds_of(s["id"])[-1] == "turn.stopped"
    send(client, h, s["id"], "hello")
    assert wait_idle(s["id"]).last_error is None


# ── settings of the session ─────────────────────────────────────────────────

def test_the_settings_can_be_changed_between_messages(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    first = report(client, h, s["id"])
    assert first["seen"]["approvalPolicy"] == "never" and first["seen"].get("developerInstructions") is None
    r = client.patch(f"/api/agent/sessions/{s['id']}", json={"instructions": " Отвечай по-русски. ", "reasoning_effort": "high", "approval_mode": "ask", "title": "Новое имя"}, headers=h)
    assert r.status_code == 200 and r.json()["instructions"] == "Отвечай по-русски." and r.json()["reasoning_effort"] == "high" and r.json()["title"] == "Новое имя"
    second = report(client, h, s["id"])
    assert second["seen"]["effort"] == "high" and second["seen"]["approvalPolicy"] == "untrusted" and second["seen"]["developerInstructions"] == "Отвечай по-русски."
    assert second["pid"] != first["pid"] and second["seen"]["method"] == "thread/resume"  # a new system prompt needs a fresh process, on the same thread
    cleared = client.patch(f"/api/agent/sessions/{s['id']}", json={"instructions": "", "reasoning_effort": ""}, headers=h).json()
    assert cleared["instructions"] is None and cleared["reasoning_effort"] is None


def test_effort_and_approval_changes_do_not_restart_the_process(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    first = report(client, h, s["id"])
    client.patch(f"/api/agent/sessions/{s['id']}", json={"reasoning_effort": "low", "approval_mode": "ask"}, headers=h)
    second = report(client, h, s["id"])
    assert first["pid"] == second["pid"] and second["seen"]["effort"] == "low" and second["seen"]["approvalPolicy"] == "untrusted"


@pytest.mark.parametrize("body", [{"reasoning_effort": "extreme"}, {"approval_mode": "yolo"}, {"instructions": "x" * 20_001}, {"title": ""}])
def test_invalid_settings_are_refused(client, wired, body):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    assert client.patch(f"/api/agent/sessions/{s['id']}", json=body, headers=h).status_code == 422


def test_settings_can_be_given_when_the_session_is_created(client, wired):
    user, h = op_user(client)
    r = client.post("/api/agent/sessions", json={"llm_profile_id": openai_profile(user.id).id, "approval_mode": "yolo"}, headers=h)
    assert r.status_code == 422
    s = new_session(client, h, openai_profile(user.id, "ok"), instructions="  ", reasoning_effort="")
    assert s["instructions"] is None and s["reasoning_effort"] is None and s["approval_mode"] == "auto"


# ── compress, review, a command of your own, undo ─────────────────────────────

def act(client, headers, session_id: int, **body):
    return client.post(f"/api/agent/sessions/{session_id}/actions", json=body, headers=headers)


def test_the_conversation_can_be_compressed(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "hello")
    wait_idle(s["id"])
    assert act(client, h, s["id"], action="compact").status_code == 202
    wait_idle(s["id"])
    got = events(s["id"])
    assert got[-4].kind == "action" and got[-4].payload == {"action": "compact"}
    assert any(e.kind == "item.completed" and e.payload["item"]["type"] == "context_compaction" for e in got)


def test_the_user_can_run_a_command_in_the_session(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    assert act(client, h, s["id"], action="shell").status_code == 422 and act(client, h, s["id"], action="shell", command="  ").status_code == 422
    assert act(client, h, s["id"], action="shell", command="ls -la").status_code == 202
    wait_idle(s["id"])
    got = events(s["id"])
    assert got[0].kind == "action" and got[0].payload == {"action": "shell", "command": "ls -la"}
    item = next(e.payload["item"] for e in got if e.kind == "item.completed")
    assert item["source"] == "userShell" and item["aggregated_output"] == "ran: ls -la\n"


def test_the_changes_can_be_reviewed(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    assert act(client, h, s["id"], action="review").status_code == 202
    wait_idle(s["id"])
    assert events(s["id"])[0].payload == {"action": "review", "target": {"type": "uncommittedChanges"}}
    reviews = [e.payload["item"] for e in events(s["id"]) if e.kind == "item.completed" and e.payload["item"]["type"] == "review_mode"]
    assert [r["entered"] for r in reviews] == [True, False]
    assert act(client, h, s["id"], action="review", target={"type": "baseBranch", "branch": "--bad"}).status_code in (202, 502, 422)  # refused by agentd or run: never a crash
    wait_idle(s["id"])


def test_the_last_turn_can_be_undone_one_after_another(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    assert act(client, h, s["id"], action="revert").status_code == 409  # nothing to undo yet
    send(client, h, s["id"], "one")
    wait_idle(s["id"])
    send(client, h, s["id"], "two")
    wait_idle(s["id"])
    turns = [e.payload["turn_id"] for e in events(s["id"]) if e.kind == "turn.started"]
    assert len(turns) == 2
    assert act(client, h, s["id"], action="revert").status_code == 202
    wait_idle(s["id"])
    assert [e.payload for e in events(s["id"]) if e.kind == "thread.reverted"] == [{"turn_id": turns[1]}]
    assert act(client, h, s["id"], action="revert").status_code == 202
    wait_idle(s["id"])
    assert [e.payload["turn_id"] for e in events(s["id"]) if e.kind == "thread.reverted"] == [turns[1], turns[0]]
    assert act(client, h, s["id"], action="revert").status_code == 409  # everything is undone


def test_nothing_can_be_started_while_a_turn_runs(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "slow")
    wait_for(has_event(s["id"], "turn.started"), what="the turn")
    for body in ({"action": "compact"}, {"action": "shell", "command": "ls"}, {"action": "review"}, {"action": "revert"}):
        assert act(client, h, s["id"], **body).status_code == 409
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    bg.join()


# ── a copy of the session ──────────────────────────────────────────────────────

def test_a_fork_has_the_history_the_files_and_the_settings(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id), instructions="Кратко.", approval_mode="ask")
    bg = start_turn(client, h, s["id"], "patch this")
    asked = wait_for(has_event(s["id"], "approval.requested"), what="the question").payload
    client.post(f"/api/agent/sessions/{s['id']}/approvals/{asked['request_id']}", json={"decision": "accept"}, headers=h)
    bg.join()
    wait_idle(s["id"])
    copy = client.post(f"/api/agent/sessions/{s['id']}/fork", headers=h)
    assert copy.status_code == 201, copy.text
    c = copy.json()
    assert c["id"] != s["id"] and c["title"].endswith("(копия)") and c["instructions"] == "Кратко." and c["approval_mode"] == "ask" and c["status"] == "idle"
    assert [(e.seq, e.kind) for e in events(c["id"])] == [(e.seq, e.kind) for e in events(s["id"])]
    assert load(c["id"]).thread_id == load(s["id"]).thread_id and load(c["id"]).key != load(s["id"]).key
    listing = client.get(f"/api/agent/sessions/{c['id']}/files", headers=h).json()
    assert "hello.txt" in [e["name"] for e in listing["entries"]]
    # the copy goes on from the same conversation, and the original is untouched
    info = report(client, h, c["id"])
    assert info["seen"]["method"] == "thread/resume" and info["cwd"].endswith(load(c["id"]).key + "/work")
    assert len(events(c["id"])) > len(events(s["id"]))


def test_a_running_session_cannot_be_forked(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    bg = start_turn(client, h, s["id"], "slow")
    wait_for(has_event(s["id"], "turn.started"), what="the turn")
    assert client.post(f"/api/agent/sessions/{s['id']}/fork", headers=h).status_code == 409
    client.post(f"/api/agent/sessions/{s['id']}/stop", headers=h)
    bg.join()


def test_a_fork_counts_against_the_limit_of_sessions(client, wired, monkeypatch):
    monkeypatch.setattr(settings, "max_agent_sessions_per_user", 1)
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "hello")
    wait_idle(s["id"])
    assert client.post(f"/api/agent/sessions/{s['id']}/fork", headers=h).status_code == 409


def test_deleting_removes_the_history_and_the_files(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    send(client, h, s["id"], "patch this")
    wait_idle(s["id"])
    key = load(s["id"]).key
    assert (wired.module.DATA_DIR / key).is_dir()
    assert client.delete(f"/api/agent/sessions/{s['id']}", headers=h).status_code == 204
    assert not (wired.module.DATA_DIR / key).exists() and events(s["id"]) == []


# ── the session's files ──────────────────────────────────────────────────────

def test_files_can_be_uploaded_listed_read_downloaded_and_zipped(client, wired):
    import io
    import zipfile

    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    base = f"/api/agent/sessions/{s['id']}/files"
    r = client.put(f"{base}?path=docs/a.txt", content=b"hello\nworld", headers=h)
    assert r.status_code == 200 and r.json() == {"path": "docs/a.txt", "size": 11}
    assert [e["name"] for e in client.get(base, headers=h).json()["entries"]] == ["docs"]
    inner = client.get(base, params={"path": "docs"}, headers=h).json()
    assert inner["path"] == "docs" and inner["entries"][0]["name"] == "a.txt"
    assert client.get(f"{base}/content", params={"path": "docs/a.txt"}, headers=h).json()["text"] == "hello\nworld"
    download = client.get(f"{base}/download", params={"path": "docs/a.txt"}, headers=h)
    assert download.status_code == 200 and download.content == b"hello\nworld" and "a.txt" in download.headers["content-disposition"]
    archive = client.get(f"{base}/archive", headers=h)
    assert archive.status_code == 200 and "docs/a.txt" in zipfile.ZipFile(io.BytesIO(archive.content)).namelist()


def test_the_files_endpoints_pass_on_the_containers_refusals(client, wired):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    base = f"/api/agent/sessions/{s['id']}/files"
    assert client.get(f"{base}/content", params={"path": "../x"}, headers=h).status_code == 400
    assert client.get(f"{base}/content", params={"path": "missing"}, headers=h).status_code == 404
    assert client.get(f"{base}/download", params={"path": "missing"}, headers=h).status_code == 404
    assert client.put(f"{base}?path=/etc/passwd", content=b"x", headers=h).status_code == 400
    assert client.post(f"{base}/clone", json={"url": "git@github.com:o/r.git"}, headers=h).status_code == 422
    assert client.post(f"{base}/clone", json={"url": ""}, headers=h).status_code == 422


def test_files_are_off_without_a_token_and_report_a_missing_container(client, wired, monkeypatch):
    user, h = op_user(client)
    s = new_session(client, h, openai_profile(user.id))
    base = f"/api/agent/sessions/{s['id']}/files"
    monkeypatch.setattr(settings, "agent_url", "http://127.0.0.1:1")
    assert client.get(base, headers=h).status_code == 502 and client.get(f"{base}/archive", headers=h).status_code == 502
    assert client.put(f"{base}?path=a", content=b"x", headers=h).status_code == 502
    monkeypatch.setattr(settings, "agent_token", "")
    assert client.get(base, headers=h).status_code == 503


# ── «Разобрать в агенте» ──────────────────────────────────────────────────────

def scan_with_results(user_id: int, **extra):
    from pathlib import Path

    from app.services.scan_files import scan_dir

    scan = make_scan(user_id, make_runner().id, status=ScanStatus.succeeded, targets=["https://shop.example"], instruction="только оплата", **extra)
    run = scan_dir(scan.id) / "strix_runs" / "r1"
    (run / "vulnerabilities").mkdir(parents=True)
    (run / "run.json").write_text("{}")
    (run / "penetration_test_report.md").write_text("# Отчёт\nНайдена SQL-инъекция")
    (run / "vulnerabilities.json").write_text("[]")
    (run / "vulnerabilities" / "vuln-1.md").write_text("# SQLi in /pay")
    with SessionLocal() as db:
        db.add(Finding(scan_id=scan.id, vuln_id="vuln-1", title="SQL-инъекция в /pay", severity="high", target="https://shop.example", endpoint="/pay", method="POST", data={"poc": "' OR 1=1"}))
        db.commit()
    return scan


def test_a_scans_results_become_the_start_of_a_session(client, wired):
    user, h = op_user(client)
    scan = scan_with_results(user.id)
    profile = openai_profile(user.id, models=["gpt-5", "o3"])
    r = client.post(f"/api/agent/from-scan/{scan.id}", json={"llm_profile_id": profile.id, "llm_model": "o3"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session"]["title"].startswith(f"Скан #{scan.id}: https://shop.example") and body["session"]["llm_model"] == "o3" and body["files"] >= 5
    assert str(scan.id) in body["draft"] and "scan/README.md" in body["draft"] and "Находок: 1" in body["draft"]
    base = f"/api/agent/sessions/{body['session']['id']}/files"
    names = [e["name"] for e in client.get(base, params={"path": "scan"}, headers=h).json()["entries"]]
    assert {"README.md", "findings.json", "report.md", "vulnerabilities.json", "vulnerabilities"} <= set(names)
    readme = client.get(f"{base}/content", params={"path": "scan/README.md"}, headers=h).json()["text"]
    assert "https://shop.example" in readme and "только оплата" in readme
    findings = json.loads(client.get(f"{base}/content", params={"path": "scan/findings.json"}, headers=h).json()["text"])
    assert findings[0]["title"] == "SQL-инъекция в /pay" and findings[0]["severity"] == "high" and findings[0]["details"] == {"poc": "' OR 1=1"}
    assert "SQL-инъекция" in client.get(f"{base}/content", params={"path": "scan/report.md"}, headers=h).json()["text"]
    assert "SQLi in /pay" in client.get(f"{base}/content", params={"path": "scan/vulnerabilities/vuln-1.md"}, headers=h).json()["text"]


def test_a_scan_without_files_on_disk_still_gives_its_stored_findings(client, wired):
    user, h = op_user(client)
    scan = make_scan(user.id, make_runner().id, status=ScanStatus.succeeded)
    r = client.post(f"/api/agent/from-scan/{scan.id}", json={"llm_profile_id": openai_profile(user.id).id}, headers=h)
    assert r.status_code == 201 and r.json()["files"] == 2 and "Находок: 0" in r.json()["draft"]


def test_scans_of_other_people_are_not_available_to_an_operator_but_to_an_admin(client, wired):
    owner, _ = op_user(client, "owner@example.com")
    scan = scan_with_results(owner.id)
    stranger, hs = op_user(client, "stranger@example.com")
    assert client.post(f"/api/agent/from-scan/{scan.id}", json={"llm_profile_id": openai_profile(stranger.id).id}, headers=hs).status_code == 404
    admin, ha = op_user(client, "boss@example.com", Role.admin)
    assert client.post(f"/api/agent/from-scan/{scan.id}", json={"llm_profile_id": openai_profile(admin.id).id}, headers=ha).status_code == 201


def test_from_scan_checks_the_profile_the_model_and_the_scan(client, wired):
    user, h = op_user(client)
    scan = make_scan(user.id, make_runner().id, status=ScanStatus.succeeded)
    assert client.post(f"/api/agent/from-scan/{scan.id}", json={"llm_profile_id": make_profile(user.id).id}, headers=h).status_code == 422  # Claude
    assert client.post(f"/api/agent/from-scan/{scan.id}", json={"llm_profile_id": openai_profile(user.id).id, "llm_model": "nope"}, headers=h).status_code == 422
    assert client.post("/api/agent/from-scan/9999", json={"llm_profile_id": openai_profile(user.id, "x").id}, headers=h).status_code == 404
    assert client.get("/api/agent/sessions", headers=h).json() == []  # nothing was left half-made


def test_from_scan_is_closed_to_viewers_and_when_the_agent_is_off(client, wired, viewer, monkeypatch):
    assert client.post("/api/agent/from-scan/1", json={"llm_profile_id": 1}, headers=viewer).status_code == 403
    user, h = op_user(client)
    monkeypatch.setattr(settings, "agent_token", "")
    assert client.post("/api/agent/from-scan/1", json={"llm_profile_id": 1}, headers=h).status_code == 503


# ── gateways ────────────────────────────────────────────────────────────────

FUNCTION_ONLY = ["-c", 'web_search="disabled"', "--disable", "multi_agent", "--disable", "goals", "--disable", "image_generation"]


def has_run(argv: list[str], run: list[str]) -> bool:
    return any(argv[i:i + len(run)] == run for i in range(len(argv)))


def provider_arg(argv: list[str]) -> str:
    return next(a for a in argv if a.startswith("model_providers.oreshnik="))


def test_a_gateway_gets_function_tools_only_and_goes_through_the_proxy(client, wired):
    user, h = op_user(client)
    argv = report(client, h, new_session(client, h, custom_gateway(user.id))["id"])["argv"]
    assert has_run(argv, FUNCTION_ONLY)
    assert f'base_url = "http://127.0.0.1:{wired.port}/compat/' in provider_arg(argv) and "gw.internal" not in provider_arg(argv)


def test_openai_itself_keeps_all_tools_and_is_not_proxied(client, wired):
    user, h = op_user(client)
    argv = report(client, h, new_session(client, h, openai_profile(user.id))["id"])["argv"]
    assert "--disable" not in argv and 'web_search="disabled"' not in argv and 'base_url = "https://api.openai.com/v1"' in provider_arg(argv)


def test_the_settings_override_the_automatic_choices(client, wired, monkeypatch):
    user, h = op_user(client)
    monkeypatch.setattr(settings, "agent_tools", "functions")
    assert has_run(report(client, h, new_session(client, h, openai_profile(user.id, "a"))["id"])["argv"], FUNCTION_ONLY)
    monkeypatch.setattr(settings, "agent_tools", "full")
    monkeypatch.setattr(settings, "agent_response_fix", False)
    argv = report(client, h, new_session(client, h, custom_gateway(user.id))["id"])["argv"]
    assert "--disable" not in argv and 'base_url = "http://gw.internal/v1"' in provider_arg(argv)


def test_agent_tools_setting_is_validated():
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(_env_file=None).agent_tools == "auto"
    assert Settings(_env_file=None, agent_tools=" Functions ").agent_tools == "functions"
    with pytest.raises(ValidationError, match="AGENT_TOOLS"):
        Settings(_env_file=None, agent_tools="some")


# ── health ────────────────────────────────────────────────────────────────────

def test_the_panel_shows_why_codex_does_not_start(client, wired, tmp_path, monkeypatch):
    path = tmp_path / "codex"
    path.write_text("#!/bin/sh\necho 'Missing optional dependency @openai/codex-linux-x64' >&2\nexit 1\n")
    path.chmod(0o755)
    monkeypatch.setattr(wired.module, "CODEX_BIN", str(path))
    health = agent_service.agent_health()
    assert health["ok"] is False and "не запускается" in health["detail"] and "Missing optional dependency" in health["detail"]


def test_the_panel_says_the_agent_is_fine_when_codex_starts(client, wired, tmp_path, monkeypatch):
    path = tmp_path / "codex"
    path.write_text("#!/bin/sh\necho codex-cli 0.169.0\n")
    path.chmod(0o755)
    monkeypatch.setattr(wired.module, "CODEX_BIN", str(path))
    assert agent_service.agent_health() == {"ok": True, "detail": "Агент на связи"}
