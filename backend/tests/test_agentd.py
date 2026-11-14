"""agentd on its own, against a stand-in for `codex app-server` (tests/fake_appserver.py) that speaks the protocol the way the real Codex 0.169
did in the recorded exchanges. Served by a real uvicorn, because what matters happens *during* a turn (live text, questions, steering, stopping)."""

import importlib.util
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

AGENTD_DIR = Path(__file__).resolve().parents[2] / "agentd"
FAKE = Path(__file__).parent / "fake_appserver.py"
TOKEN = "agent-test-token"
KEY = "secret-key-123456"


def load_agentd():
    if str(AGENTD_DIR) not in sys.path:
        sys.path.insert(0, str(AGENTD_DIR))
    spec = importlib.util.spec_from_file_location("agentd_main", AGENTD_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Live:
    """A running agentd and the calls a test makes to it."""

    def __init__(self, module, port: int):
        self.module, self.port = module, port
        self.url = f"http://127.0.0.1:{port}"
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        return httpx.request(method, self.url + path, headers=self.headers, timeout=60, **kw)

    def body(self, prompt: str = "hello", **over) -> dict:
        body = {"session_key": "k1", "action": "message", "prompt": prompt, "model": "m", "base_url": "http://x/v1", "api_key": KEY, "timeout_seconds": 30}
        body.update(over)
        return body

    def turn(self, prompt: str = "hello", **over) -> list[dict]:
        """Run a turn to its end and return its events."""
        events: list[dict] = []
        with httpx.stream("POST", self.url + "/turn", json=self.body(prompt, **over), headers=self.headers, timeout=60) as response:
            assert response.status_code == 200, response.read()
            for line in response.iter_lines():
                if line.strip():
                    events.append(json.loads(line))
        return events

    def start(self, prompt: str, **over) -> "Running":
        return Running(self, self.body(prompt, **over))

    def report(self, **over) -> dict:
        events = self.turn("report", **over)
        text = next(e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message")
        return json.loads(text)


class Running:
    """A turn in a background thread, so the test can answer, steer or stop it while it runs."""

    def __init__(self, live: Live, body: dict):
        self.events: list[dict] = []
        self.status: int | None = None
        self.done = threading.Event()
        self._response = None
        self._thread = threading.Thread(target=self._run, args=(live, body), daemon=True)
        self._thread.start()

    def _run(self, live: Live, body: dict) -> None:
        try:
            with httpx.stream("POST", live.url + "/turn", json=body, headers=live.headers, timeout=60) as response:
                self._response = response
                self.status = response.status_code
                if response.status_code == 200:
                    for line in response.iter_lines():
                        if line.strip():
                            self.events.append(json.loads(line))
        except httpx.HTTPError:
            pass
        finally:
            self.done.set()

    def wait_for(self, predicate, timeout: float = 15.0):
        end = time.time() + timeout
        while time.time() < end:
            for event in list(self.events):
                if predicate(event):
                    return event
            time.sleep(0.02)
        raise AssertionError(f"event never came; got {[e['type'] for e in self.events]}")

    def finish(self, timeout: float = 20.0) -> list[dict]:
        assert self.done.wait(timeout), "the turn did not end"
        return self.events

    def hang_up(self) -> None:
        if self._response is not None:
            self._response.close()
        self.done.wait(5)


@pytest.fixture
def agentd(monkeypatch, tmp_path):
    module = load_agentd()
    FAKE.chmod(0o755)
    monkeypatch.setattr(module, "CODEX_BIN", str(FAKE))
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "agent-data")
    monkeypatch.setattr(module, "TOKEN", TOKEN)
    monkeypatch.setattr(module, "PING_SECONDS", 0.3)
    monkeypatch.setattr(module, "LIVE_SECONDS", 0.02)
    monkeypatch.setattr(module, "INTERRUPT_WAIT", 3.0)
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(module, "PORT", port)
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield Live(module, port)
    server.should_exit = True
    thread.join(15)


def kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# ── auth and input checking ───────────────────────────────────────────────────────

def test_every_call_needs_the_token(agentd):
    for method, path, body in (("POST", "/turn", agentd.body()), ("POST", "/stop", {"session_key": "k"}), ("POST", "/steer", {"session_key": "k", "text": "x"}),
                               ("POST", "/approve", {"session_key": "k", "request_id": "1", "decision": "accept"}),
                               ("POST", "/sessions/k/fork", {"new_key": "k2"}), ("DELETE", "/sessions/k", None), ("GET", "/sessions/k/fs", None),
                               ("GET", "/sessions/k/file?path=a", None), ("PUT", "/sessions/k/file?path=a", None), ("GET", "/sessions/k/archive", None),
                               ("POST", "/sessions/k/clone", {"url": "https://h/o/r"})):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            r = httpx.request(method, agentd.url + path, json=body, headers=headers, timeout=20)
            assert r.status_code == 401, (method, path)


def test_an_unset_token_refuses_everything(agentd, monkeypatch):
    monkeypatch.setattr(agentd.module, "TOKEN", "")
    r = httpx.post(agentd.url + "/turn", json=agentd.body(), headers={"Authorization": "Bearer anything"}, timeout=20)
    assert r.status_code == 401


@pytest.mark.parametrize("over", [
    {"session_key": "../etc"}, {"session_key": "a/b"}, {"thread_id": "x; rm -rf /"}, {"model": "a b"}, {"model": "--oss"}, {"effort": "extreme"},
    {"prompt": "   "}, {"action": "shell", "prompt": ""}, {"action": "revert"}, {"action": "revert", "revert_before": "a b"},
    {"action": "review", "review_target": {"type": "baseBranch", "branch": "--evil"}}, {"action": "review", "review_target": {"type": "commit", "sha": "zz"}},
    {"action": "review", "review_target": {"type": "other"}}, {"action": "nope"},
])
def test_unsafe_or_incomplete_input_is_refused(agentd, over):
    r = agentd.call("POST", "/turn", json=agentd.body(**over))
    assert r.status_code in (404, 422), (over, r.text)


# ── a turn ────────────────────────────────────────────────────────────────────────

def test_a_turn_streams_the_answer_as_it_is_written_and_then_the_finished_item(agentd):
    events = agentd.turn("hello")
    assert kinds(events)[:2] == ["thread.started", "turn.started"] and kinds(events)[-2:] == ["turn.completed", "agentd.exit"]
    lives = [e["item"]["text"] for e in events if e["type"] == "live" and e["item"]["type"] == "agent_message"]
    assert lives and all(a in b for a, b in zip(lives, lives[1:]))  # a snapshot only ever grows
    final = [e for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
    assert [f["item"]["text"] for f in final] == ["Привет! Это ответ."]
    done = next(e for e in events if e["type"] == "turn.completed")
    assert done["status"] == "completed" and done["usage"]["input_tokens"] > 0 and done["usage"]["output_tokens"] > 0
    assert events[-1] == {"type": "agentd.exit", "code": 0, "timed_out": False, "lingered": False, "stderr": ""}


def test_the_users_own_message_is_not_echoed_back(agentd):
    events = agentd.turn("hello")
    assert not any(e["type"] == "item.completed" and e["item"].get("type") == "user_message" for e in events)


def test_what_codex_is_started_and_told(agentd):
    report = agentd.report(instructions="Всегда кратко.", effort="low")
    argv = report["argv"]
    assert argv[0] == "app-server" and 'model="m"' in argv and 'model_provider="oreshnik"' in argv
    assert any(a.startswith("model_providers.oreshnik=") and 'base_url = "http://x/v1"' in a and 'wire_api = "responses"' in a for a in argv)
    assert "--disable" not in argv  # the full tool set unless asked for function tools only
    assert report["key_env"] == KEY and report["leaked"] == []
    assert Path(report["codex_home"]).is_dir() and report["cwd"].endswith("/work") and (Path(report["cwd"]) / ".git").is_dir()
    seen = report["seen"]
    assert seen["sandbox"] == "danger-full-access" and seen["approvalPolicy"] == "never" and seen["developerInstructions"] == "Всегда кратко."
    assert seen["model"] == "m" and seen["effort"] == "low" and seen["method"] == "thread/start"


def test_a_gateway_gets_function_tools_only(agentd):
    argv = agentd.report(function_tools_only=True)["argv"]
    run = ["-c", 'web_search="disabled"', "--disable", "multi_agent", "--disable", "goals", "--disable", "image_generation"]
    assert any(argv[i:i + len(run)] == run for i in range(len(argv)))


def test_the_toml_in_the_command_line_cannot_be_broken_out_of(agentd):
    argv = agentd.report(base_url='http://x/v1"\nwire_api = "evil')["argv"]
    provider = next(a for a in argv if a.startswith("model_providers.oreshnik="))
    assert "\n" not in provider and '\\"' in provider


def test_the_same_process_serves_the_next_message_and_the_thread_goes_on(agentd):
    first = agentd.report()
    second = agentd.report()
    assert first["pid"] == second["pid"]
    assert first["seen"]["method"] == "thread/start"
    threads = {e["thread_id"] for e in agentd.turn("hello") if e["type"] == "thread.started"}
    assert len(threads) == 1


def test_a_changed_system_prompt_restarts_the_process_and_resumes_the_same_thread(agentd):
    first = agentd.report(instructions="одно")
    thread = next(e["thread_id"] for e in agentd.turn("hello", instructions="одно") if e["type"] == "thread.started")
    second = agentd.report(instructions="другое")
    assert first["pid"] != second["pid"]
    assert second["seen"]["method"] == "thread/resume" and second["seen"]["developerInstructions"] == "другое"
    assert next(e["thread_id"] for e in agentd.turn("hello", instructions="другое") if e["type"] == "thread.started") == thread


def test_a_lost_thread_is_replaced_instead_of_refusing_to_answer(agentd):
    report = agentd.report(thread_id="gone-forever")
    assert report["seen"]["method"] == "thread/start"


def test_the_effort_and_the_approval_mode_change_between_messages_without_a_restart(agentd):
    first = agentd.report(effort="low", approval_mode="auto")
    second = agentd.report(effort="high", approval_mode="ask")
    assert first["pid"] == second["pid"] and second["seen"]["effort"] == "high" and second["seen"]["approvalPolicy"] == "untrusted"


def test_a_second_turn_while_one_runs_is_refused(agentd):
    running = agentd.start("slow")
    running.wait_for(lambda e: e["type"] == "turn.started")
    assert agentd.call("POST", "/turn", json=agentd.body("again")).status_code == 409
    agentd.call("POST", "/stop", json={"session_key": "k1"})
    running.finish()


def test_sessions_are_separate_processes_with_their_own_folders(agentd):
    a = agentd.report(session_key="sa")
    b = agentd.report(session_key="sb")
    assert a["pid"] != b["pid"] and a["cwd"] != b["cwd"]


def test_too_many_active_sessions_are_refused_with_a_reason(agentd, monkeypatch):
    monkeypatch.setattr(agentd.module, "MAX_PROCESSES", 1)
    first = agentd.start("slow", session_key="sa")
    first.wait_for(lambda e: e["type"] == "turn.started")
    r = agentd.call("POST", "/turn", json=agentd.body("hi", session_key="sb"))
    assert r.status_code == 503 and "не больше 1" in r.json()["detail"]
    agentd.call("POST", "/stop", json={"session_key": "sa"})
    first.finish()
    assert agentd.call("POST", "/turn", json=agentd.body("hi", session_key="sb")).status_code == 200  # the idle one made room


def test_an_idle_session_gives_up_its_process_and_resumes_later(agentd, monkeypatch):
    monkeypatch.setattr(agentd.module, "IDLE_SECONDS", 0.2)
    first = agentd.report()
    deadline = time.time() + 40
    while time.time() < deadline and agentd.call("GET", "/health").json()["sessions"] != 0:
        time.sleep(1)
    assert agentd.call("GET", "/health").json()["sessions"] == 0
    second = agentd.report()
    assert second["pid"] != first["pid"] and second["seen"]["method"] == "thread/resume"


# ── what is shown and kept ─────────────────────────────────────────────────────────

def test_a_command_is_shown_as_the_user_would_type_it_with_its_output(agentd):
    events = agentd.turn("run a command")
    done = next(e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "command_execution")
    assert done["command"] == "echo hi-from-agent" and done["aggregated_output"] == "hi-from-agent\n" and done["exit_code"] == 0 and done["status"] == "completed"


def test_a_file_change_comes_with_its_diff_and_the_turns_diff(agentd):
    events = agentd.turn("patch this")
    change = next(e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "file_change")
    assert change["changes"] == [{"path": "hello.txt", "kind": "add", "diff": "hi\nthere\n"}] and change["status"] == "completed"
    assert any(e["type"] == "turn.diff" and "+++ b/hello.txt" in e["diff"] for e in events)
    assert (Path(agentd.module.DATA_DIR) / "k1" / "work" / "hello.txt").read_text() == "hi\nthere\n"


def test_a_plan_is_shown_live_and_kept_at_the_end(agentd):
    events = agentd.turn("plan")
    plan = next(e for e in events if e["type"] == "turn.plan")
    assert [(s["text"], s["completed"], s["in_progress"]) for s in plan["steps"]] == [("Прочитать код", True, False), ("Написать тест", False, True), ("Починить", False, False)]
    assert any(e["type"] == "live.plan" for e in events)


def test_a_failed_turn_says_why(agentd):
    done = next(e for e in agentd.turn("fail") if e["type"] == "turn.completed")
    assert done["status"] == "failed" and done["error"] == "model exploded"


def test_a_crashing_process_is_reported_and_the_next_message_starts_a_new_one(agentd):
    events = agentd.turn("crash")
    assert any(e["type"] == "agentd.error" for e in events) and events[-1]["type"] == "agentd.exit"
    assert agentd.report()["seen"]["method"] == "thread/resume"


# ── the user's side: questions, steering, stopping ───────────────────────────────────

def test_approving_a_command_lets_it_run(agentd):
    running = agentd.start("run a command", approval_mode="ask")
    asked = running.wait_for(lambda e: e["type"] == "approval.requested")
    assert asked["approval"]["kind"] == "command" and asked["approval"]["command"] == "echo hi-from-agent"
    assert not any(e["type"] == "item.completed" and e["item"]["type"] == "command_execution" for e in running.events)  # held until answered
    assert agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "accept"}).status_code == 200
    events = running.finish()
    assert any(e["type"] == "approval.resolved" and e["request_id"] == asked["request_id"] for e in events)
    assert next(e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "command_execution")["status"] == "completed"


def test_declining_a_command_stops_it_from_running(agentd):
    running = agentd.start("run a command", approval_mode="ask")
    asked = running.wait_for(lambda e: e["type"] == "approval.requested")
    agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "decline"})
    done = next(e["item"] for e in running.finish() if e["type"] == "item.completed" and e["item"]["type"] == "command_execution")
    assert done["status"] == "declined" and not done["aggregated_output"]


def test_a_file_change_waits_for_the_users_word_too(agentd):
    running = agentd.start("patch this", approval_mode="ask")
    asked = running.wait_for(lambda e: e["type"] == "approval.requested")
    assert asked["approval"]["kind"] == "file"
    assert not (Path(agentd.module.DATA_DIR) / "k1" / "work" / "hello.txt").exists()
    agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "accept_session"})
    running.finish()
    assert (Path(agentd.module.DATA_DIR) / "k1" / "work" / "hello.txt").exists()


def test_no_questions_are_asked_when_the_mode_is_auto(agentd):
    assert not any(e["type"] == "approval.requested" for e in agentd.turn("run a command"))


def test_the_agent_can_ask_the_user_a_question_and_gets_the_answer(agentd):
    running = agentd.start("ask me")
    asked = running.wait_for(lambda e: e["type"] == "approval.requested")
    question = asked["approval"]["questions"][0]
    assert asked["approval"]["kind"] == "input" and question["id"] == "color" and [o["label"] for o in question["options"]] == ["красный", "синий"]
    agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "answer", "answers": {"color": "синий"}})
    text = next(e["item"]["text"] for e in running.finish() if e["type"] == "item.completed" and e["item"]["type"] == "agent_message")
    assert text == "выбрано: синий"


def test_a_bad_or_late_answer_is_refused(agentd):
    assert agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": "1", "decision": "accept"}).status_code == 404
    running = agentd.start("run a command", approval_mode="ask")
    asked = running.wait_for(lambda e: e["type"] == "approval.requested")
    wrong = agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "answer", "answers": {"a": "b"}})
    assert wrong.status_code == 422  # a command cannot be "answered"
    agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "accept"})
    running.finish()
    assert agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "accept"}).status_code == 404


def test_an_unanswered_question_is_declined_after_a_while(agentd, monkeypatch):
    monkeypatch.setattr(agentd.module, "APPROVAL_TIMEOUT", 0.5)
    events = agentd.turn("run a command", approval_mode="ask")
    done = next(e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "command_execution")
    assert done["status"] == "declined"


def test_an_instruction_added_to_a_running_turn_is_taken_into_account(agentd):
    running = agentd.start("slow")
    running.wait_for(lambda e: e["type"] == "turn.started")
    assert agentd.call("POST", "/steer", json={"session_key": "k1", "text": "и ещё ура"}).status_code == 200
    text = next(e["item"]["text"] for e in running.finish() if e["type"] == "item.completed" and e["item"]["type"] == "agent_message")
    assert text.endswith("(учёл: и ещё ура)")


def test_steering_needs_a_running_turn(agentd):
    assert agentd.call("POST", "/steer", json={"session_key": "k1", "text": "x"}).status_code == 409
    agentd.turn("hello")
    assert agentd.call("POST", "/steer", json={"session_key": "k1", "text": "x"}).status_code == 409


def test_stop_interrupts_the_turn_and_the_session_goes_on(agentd):
    running = agentd.start("slow")
    running.wait_for(lambda e: e["type"] == "turn.started")
    assert agentd.call("POST", "/stop", json={"session_key": "k1"}).json() == {"stopped": True}
    events = running.finish()
    assert next(e for e in events if e["type"] == "turn.completed")["status"] == "interrupted" and events[-1]["code"] == 0
    assert agentd.report()["pid"]  # the same session answers the next message


def test_stopping_an_idle_session_is_harmless(agentd):
    assert agentd.call("POST", "/stop", json={"session_key": "nothing"}).json() == {"stopped": False}


def test_a_turn_nobody_reads_any_more_is_interrupted(agentd):
    running = agentd.start("slow")
    running.wait_for(lambda e: e["type"] == "turn.started")
    running.hang_up()
    deadline = time.time() + 15
    while time.time() < deadline and agentd.call("POST", "/turn", json=agentd.body("again")).status_code == 409:
        time.sleep(0.3)
    assert agentd.report()["pid"]  # the session is free again, not stuck "running"


def test_a_turn_that_runs_too_long_is_interrupted_and_flagged(agentd):
    events = agentd.turn("slower", timeout_seconds=10)
    assert events[-1]["type"] == "agentd.exit" and events[-1]["timed_out"] is True
    assert next(e for e in events if e["type"] == "turn.completed")["status"] == "interrupted"


def test_a_turn_waiting_for_an_answer_keeps_pinging(agentd):
    running = agentd.start("ask me")
    running.wait_for(lambda e: e["type"] == "approval.requested")
    first = sum(1 for e in running.events if e["type"] == "agentd.ping")
    time.sleep(1.0)
    assert sum(1 for e in running.events if e["type"] == "agentd.ping") > first
    asked = next(e for e in running.events if e["type"] == "approval.requested")
    agentd.call("POST", "/approve", json={"session_key": "k1", "request_id": asked["request_id"], "decision": "cancel"})
    running.finish()


# ── other actions ───────────────────────────────────────────────────────────────

def test_compacting_the_conversation(agentd):
    agentd.turn("hello")
    events = agentd.turn(action="compact", prompt="")
    assert any(e["type"] == "item.completed" and e["item"]["type"] == "context_compaction" for e in events) and kinds(events)[-2] == "turn.completed"


def test_a_command_the_user_runs_themselves(agentd):
    events = agentd.turn(action="shell", prompt="ls -la")
    item = next(e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "command_execution")
    assert item["source"] == "userShell" and item["aggregated_output"] == "ran: ls -la\n"


def test_a_review_of_the_changes(agentd):
    events = agentd.turn(action="review", prompt="", review_target={"type": "uncommittedChanges"})
    reviews = [e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "review_mode"]
    assert [r["entered"] for r in reviews] == [True, False]
    assert any(e["type"] == "item.completed" and e["item"].get("text") == "Замечаний нет." for e in events)


def test_a_review_target_is_passed_on_in_the_form_codex_wants(agentd):
    events = agentd.turn(action="review", prompt="", review_target={"type": "baseBranch", "branch": "main", "extra": "dropped"})
    entered = next(e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "review_mode" and e["item"]["entered"])
    assert json.loads(entered["review"]) == {"type": "baseBranch", "branch": "main"}


def test_undoing_the_last_turn(agentd):
    events = agentd.turn(action="revert", prompt="", revert_before="turn-1")
    assert kinds(events) == ["thread.started", "thread.reverted", "agentd.exit"]  # no turn to wait for: it is over at once


# ── forking, files ─────────────────────────────────────────────────────────────────

def test_a_fork_has_the_files_and_resumes_the_same_conversation(agentd):
    thread = next(e["thread_id"] for e in agentd.turn("patch this") if e["type"] == "thread.started")
    r = agentd.call("POST", "/sessions/k1/fork", json={"new_key": "k2"})
    assert r.status_code == 200 and r.json() == {"thread_id": thread}
    assert (Path(agentd.module.DATA_DIR) / "k2" / "work" / "hello.txt").read_text() == "hi\nthere\n"
    report = agentd.report(session_key="k2", thread_id=thread)
    assert report["seen"]["method"] == "thread/resume" and report["cwd"].endswith("k2/work")
    assert agentd.call("POST", "/sessions/k1/fork", json={"new_key": "k2"}).status_code == 409  # the copy exists already
    assert agentd.call("POST", "/sessions/nothing/fork", json={"new_key": "k3"}).status_code == 404


def test_a_session_cannot_be_forked_or_deleted_while_it_runs(agentd):
    running = agentd.start("slow")
    running.wait_for(lambda e: e["type"] == "turn.started")
    assert agentd.call("POST", "/sessions/k1/fork", json={"new_key": "k2"}).status_code == 409
    assert agentd.call("DELETE", "/sessions/k1").status_code == 409
    agentd.call("POST", "/stop", json={"session_key": "k1"})
    running.finish()


def test_deleting_a_session_ends_its_process_and_removes_its_files(agentd):
    agentd.turn("patch this")
    assert (Path(agentd.module.DATA_DIR) / "k1").is_dir()
    assert agentd.call("DELETE", "/sessions/k1").json() == {"deleted": True}
    assert not (Path(agentd.module.DATA_DIR) / "k1").exists() and agentd.call("GET", "/health").json()["sessions"] == 0


def test_files_can_be_uploaded_listed_read_and_downloaded(agentd):
    assert agentd.call("PUT", "/sessions/k1/file", params={"path": "docs/a.txt"}, content=b"hello\nworld").json() == {"path": "docs/a.txt", "size": 11}
    listing = agentd.call("GET", "/sessions/k1/fs").json()
    assert [e["name"] for e in listing["entries"]] == ["docs"] and listing["entries"][0]["dir"] is True
    inner = agentd.call("GET", "/sessions/k1/fs", params={"path": "docs"}).json()
    assert inner["path"] == "docs" and inner["entries"][0]["name"] == "a.txt" and inner["entries"][0]["size"] == 11
    assert agentd.call("GET", "/sessions/k1/file", params={"path": "docs/a.txt"}).json() == {"binary": False, "size": 11, "text": "hello\nworld", "truncated": False}
    download = agentd.call("GET", "/sessions/k1/file", params={"path": "docs/a.txt", "download": "true"})
    assert download.content == b"hello\nworld" and "a.txt" in download.headers["content-disposition"] and download.headers["x-content-type-options"] == "nosniff"


def test_a_binary_file_is_described_not_sent(agentd):
    agentd.call("PUT", "/sessions/k1/file", params={"path": "b.bin"}, content=b"\x00\x01\x02" * 100)
    assert agentd.call("GET", "/sessions/k1/file", params={"path": "b.bin"}).json() == {"binary": True, "size": 300, "text": None, "truncated": False}


def test_nothing_outside_the_working_directory_can_be_reached(agentd):
    agentd.turn("hello")
    work = Path(agentd.module.DATA_DIR) / "k1" / "work"
    secret = Path(agentd.module.DATA_DIR) / "outside.txt"
    secret.write_text("secret")
    (work / "link").symlink_to(secret)        # the agent can make links; they must lead nowhere
    (work / "dirlink").symlink_to(secret.parent)
    for call in (lambda p: agentd.call("GET", "/sessions/k1/file", params={"path": p}), lambda p: agentd.call("GET", "/sessions/k1/fs", params={"path": p}),
                 lambda p: agentd.call("PUT", "/sessions/k1/file", params={"path": p}, content=b"x"), lambda p: agentd.call("GET", "/sessions/k1/file", params={"path": p, "download": "true"})):
        for path in ("../outside.txt", "..", "a/../../outside.txt", "/etc/passwd", "link", "dirlink/outside.txt", "dirlink"):
            assert call(path).status_code in (400, 404), path
    assert secret.read_text() == "secret"


def test_an_upload_over_the_limit_leaves_nothing_behind(agentd, monkeypatch):
    import fsops

    monkeypatch.setattr(fsops, "MAX_UPLOAD_BYTES", 1000)
    r = agentd.call("PUT", "/sessions/k1/file", params={"path": "big.bin"}, content=b"x" * 5000)
    assert r.status_code == 413
    assert agentd.call("GET", "/sessions/k1/fs").json()["entries"] == []  # not even a half-written temp file


def test_the_whole_folder_can_be_downloaded_as_a_zip_without_git_internals(agentd):
    import io
    import zipfile

    agentd.turn("hello")
    agentd.call("PUT", "/sessions/k1/file", params={"path": "src/m.py"}, content=b"print(1)")
    reply = agentd.call("GET", "/sessions/k1/archive")
    assert reply.status_code == 200 and reply.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(reply.content)).namelist()
    assert "src/m.py" in names and not any(n.startswith(".git") for n in names)
    assert list((Path(agentd.module.DATA_DIR) / ".tmp").glob("*.zip")) == []  # the temporary file is removed


def test_cloning_accepts_only_plain_https_links(agentd):
    for url in ("git@github.com:o/r.git", "file:///etc", "ext::sh -c id", "http://h/o/r", "https://h", "https://h/o/r;rm", "https://h/o/r --upload-pack=x", "ssh://h/o/r", "https://u:p@h/o/r"):
        assert agentd.call("POST", "/sessions/k1/clone", json={"url": url}).status_code == 422, url


def test_a_clone_lands_in_its_own_new_folder(agentd, monkeypatch, tmp_path):
    import fsops

    async def fake_clone(url, dest, env, timeout=300.0):
        dest.mkdir(parents=True)
        (dest / "README").write_text(url)

    monkeypatch.setattr(fsops, "git_clone", fake_clone)
    r = agentd.call("POST", "/sessions/k1/clone", json={"url": "https://github.com/octo/hello.git"})
    assert r.status_code == 200 and r.json() == {"path": "hello"}
    assert (Path(agentd.module.DATA_DIR) / "k1" / "work" / "hello" / "README").read_text() == "https://github.com/octo/hello.git"
    assert agentd.call("POST", "/sessions/k1/clone", json={"url": "https://github.com/octo/hello.git"}).status_code == 409


# ── health, compat proxy ────────────────────────────────────────────────────────────

def script(tmp_path, body: str, name: str = "codex"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_health_is_ok_when_codex_starts(agentd, tmp_path, monkeypatch):
    monkeypatch.setattr(agentd.module, "CODEX_BIN", script(tmp_path, "echo codex-cli 0.169.0\n"))
    assert agentd.call("GET", "/health").json() == {"ok": True, "codex": True, "codex_detail": "codex-cli 0.169.0", "sessions": 0}


def test_health_catches_a_launcher_whose_binary_is_missing(agentd, tmp_path, monkeypatch):
    """The real failure seen after a build: `codex` is on the PATH, but its platform package was never installed."""
    msg = "Error: Missing optional dependency @openai/codex-linux-x64. Reinstall Codex: npm install -g @openai/codex@latest"
    monkeypatch.setattr(agentd.module, "CODEX_BIN", script(tmp_path, f"echo '{msg}' >&2\nexit 1\n"))
    body = agentd.call("GET", "/health").json()
    assert body["codex"] is False and "Missing optional dependency" in body["codex_detail"]


def test_health_reports_a_missing_binary(agentd, monkeypatch):
    monkeypatch.setattr(agentd.module, "CODEX_BIN", "definitely-not-installed-codex")
    body = agentd.call("GET", "/health").json()
    assert body["codex"] is False and "не найден" in body["codex_detail"]


def test_health_does_not_start_codex_on_every_poll(agentd, tmp_path, monkeypatch):
    counter = tmp_path / "runs"
    monkeypatch.setattr(agentd.module, "CODEX_BIN", script(tmp_path, f"echo x >> {counter}\necho v\n"))
    for _ in range(3):
        assert agentd.call("GET", "/health").json()["codex"] is True
    assert len(counter.read_text().splitlines()) == 1


BROKEN = {"type": "response.completed", "response": {"id": "r1", "usage": {"input_tokens": 7, "input_tokens_details": {}, "output_tokens": 3, "output_tokens_details": {}}}}


def sse(event: dict) -> str:
    return "data: " + json.dumps(event, separators=(",", ":"))


def test_the_missing_usage_numbers_are_filled_in(agentd):
    fixed = json.loads(agentd.module.fix_sse_line(sse(BROKEN))[5:])["response"]["usage"]
    assert fixed == {"input_tokens": 7, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 3, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 10}


def test_numbers_the_gateway_did_send_are_kept(agentd):
    event = {"type": "response.completed", "response": {"usage": {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
             "input_tokens_details": {"cached_tokens": 64, "text_tokens": 100}, "output_tokens_details": {"reasoning_tokens": 2}}}}
    usage = json.loads(agentd.module.fix_sse_line(sse(event))[5:])["response"]["usage"]
    assert usage["input_tokens_details"] == {"cached_tokens": 64, "text_tokens": 100} and usage["output_tokens_details"] == {"reasoning_tokens": 2} and usage["total_tokens"] == 105


def test_a_chat_style_usage_in_a_responses_stream_is_mapped(agentd):
    event = {"type": "response.completed", "response": {"usage": {"prompt_tokens": 9, "completion_tokens": 4}}}
    usage = json.loads(agentd.module.fix_sse_line(sse(event))[5:])["response"]["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (9, 4, 13)


@pytest.mark.parametrize("line", [
    "", "event: response.completed", ": keep-alive", "data: [DONE]", "data: not json {",
    sse({"type": "response.output_text.delta", "delta": "hi", "usage": "word"}),
    sse({"type": "response.completed", "response": {"id": "r"}}), sse({"type": "response.completed", "response": {"usage": None}}),
])
def test_every_other_line_passes_through_untouched(agentd, line):
    assert agentd.module.fix_sse_line(line) == line


def test_the_proxy_is_closed_to_everyone_but_this_container(agentd):
    agentd.module._compat["tok"] = "http://127.0.0.1:1/v1"
    assert httpx.post(agentd.url + "/compat/unknown/v1/responses", json={}, timeout=20).status_code == 404  # no such token


def run_upstream(events: list[str], seen: list):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in events:
                self.wfile.write((line + "\n").encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_gateways_stream_comes_through_with_a_complete_usage(agentd):
    seen: list = []
    delta = sse({"type": "response.output_text.delta", "delta": "Привет"})
    upstream = run_upstream(["event: response.created", sse({"type": "response.created", "response": {"id": "r1"}}), "", "event: response.output_text.delta", delta, "",
                             "event: response.completed", sse(BROKEN), "", "data: [DONE]", ""], seen)
    try:
        agentd.module._compat["tok"] = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        r = httpx.post(f"{agentd.url}/compat/tok/responses", json={"model": "m", "input": []}, headers={"Authorization": "Bearer gateway-key"}, timeout=20)
    finally:
        upstream.shutdown()
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert seen[0]["path"] == "/v1/responses" and seen[0]["auth"] == "Bearer gateway-key" and seen[0]["body"] == {"model": "m", "input": []}
    lines = r.text.split("\n")
    assert delta in lines and "data: [DONE]" in lines and "event: response.completed" in lines
    done = json.loads(next(l for l in lines if '"response.completed"' in l and l.startswith("data:"))[5:])["response"]["usage"]
    assert done["input_tokens_details"] == {"cached_tokens": 0} and done["output_tokens_details"] == {"reasoning_tokens": 0}


def test_a_gateway_turn_goes_through_the_proxy_and_the_route_is_dropped_with_the_process(agentd):
    report = agentd.report(normalize_responses=True)
    provider = next(a for a in report["argv"] if a.startswith("model_providers.oreshnik="))
    assert f'base_url = "http://127.0.0.1:{agentd.port}/compat/' in provider and "x/v1" not in provider  # Codex never talks to the gateway itself
    assert len(agentd.module._compat) == 1
    agentd.call("DELETE", "/sessions/k1")
    assert agentd.module._compat == {}


def test_openai_itself_is_not_proxied(agentd):
    provider = next(a for a in agentd.report(base_url="https://api.openai.com/v1")["argv"] if a.startswith("model_providers.oreshnik="))
    assert 'base_url = "https://api.openai.com/v1"' in provider and agentd.module._compat == {}
