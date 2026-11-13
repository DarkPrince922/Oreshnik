"""Agent activity: normalisation of Strix's SDK items, the tailer, and secrets/size handling."""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.engine.mock_agents import MockAgents
from app.services import activity
from app.services.activity import ActivityTailer, Normalizer, clip, read_new_messages, read_roster, summarize_call
from app.services.scan_files import activity_path, agents_path, scan_dir


def fc(name, args, call_id="c1"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}


# ── pure helpers ────────────────────────────────────────────────────────

def test_clip_strips_ansi_and_control_chars_but_keeps_newlines():
    assert clip("\x1b[31mred\x1b[0m\x00 ok\nline2\tx", 100) == ("red ok\nline2\tx", False)


def test_clip_keeps_head_and_tail_and_says_how_much_was_dropped():
    text, cut = clip("A" * 5000 + "MIDDLE" + "Z" * 5000, 300)
    assert cut and text.startswith("AAAA") and text.endswith("ZZZZ") and "MIDDLE" not in text
    assert "пропущено символов" in text and len(text) < 400


@pytest.mark.parametrize("tool,args,expected", [
    ("exec_command", {"cmd": "nmap -sV  example.com\n"}, "nmap -sV example.com"),
    ("exec_command", {"command": "ls"}, "ls"),
    ("write_stdin", {"chars": "id\n"}, "stdin ← id"),
    ("create_agent", {"name": "XSS Specialist", "task": "test the search box"}, "XSS Specialist: test the search box"),
    ("agent_finish", {"summary": "done"}, "done"),
    ("browse", {"url": "https://x.test"}, "https://x.test"),
    ("weird", {"a": 1}, '{"a": 1}'),
    ("exec_command", {}, ""),
])
def test_summarize_call(tool, args, expected):
    assert summarize_call(tool, args) == expected


def test_summary_is_bounded():
    assert len(summarize_call("exec_command", {"cmd": "x" * 5000})) < 400


# ── Normalizer ──────────────────────────────────────────────────────────

def test_first_user_turn_is_the_agents_task_and_later_user_turns_are_messages():
    n = Normalizer()
    first = n.convert("a1", {"role": "user", "content": "Test the login form"}, "2026-01-01 10:00:00")
    second = n.convert("a1", {"role": "user", "content": "please also check logout"}, "2026-01-01 10:00:05")
    assert (first["kind"], first["text"]) == ("task", "Test the login form")
    assert (second["kind"], second["role"]) == ("message", "user")
    assert first["ts"] == "2026-01-01T10:00:00+00:00"  # naive SQLite time is UTC


def test_system_noise_is_dropped_but_inter_agent_messages_and_budget_notices_are_kept():
    n = Normalizer()
    n.convert("a1", {"role": "user", "content": "task"}, None)
    assert n.convert("a1", {"role": "user", "content": "== Inherited context from parent ==\n..."}, None) is None
    assert n.convert("a1", {"role": "user", "content": "Your previous message ended a turn without a tool call."}, None) is None
    msg = n.convert("a1", {"role": "user", "content": "[Message from Root Agent] focus on /admin"}, None)
    note = n.convert("a1", {"role": "user", "content": "[URGENT] Scan cost budget: 95% used"}, None)
    assert msg["kind"] == "agent_message" and note["kind"] == "system"


def test_assistant_message_with_content_parts():
    e = Normalizer().convert("a", {"role": "assistant", "content": [{"type": "output_text", "text": "Thinking "}, {"type": "output_text", "text": "aloud"}]}, None)
    assert (e["kind"], e["role"], e["text"]) == ("message", "assistant", "Thinking aloud")


def test_function_call_arguments_are_parsed_summarised_and_clipped():
    n = Normalizer()
    e = n.convert("a", fc("exec_command", {"cmd": "curl x", "script": "S" * 20000}), None)
    assert e["kind"] == "tool_call" and e["tool"] == "exec_command" and e["summary"] == "curl x"
    assert e["args"]["cmd"] == "curl x" and len(e["args"]["script"]) < 3500 and e["truncated"]


def test_unparseable_and_non_object_arguments_do_not_crash():
    n = Normalizer()
    bad = n.convert("a", {"type": "function_call", "call_id": "x", "name": "t", "arguments": "{not json"}, None)
    listy = n.convert("a", {"type": "function_call", "call_id": "y", "name": "t", "arguments": "[1,2]"}, None)
    assert bad["args"] == {"raw": "{not json"} and listy["args"] == {"value": [1, 2]}


def test_tool_output_is_paired_with_its_call():
    n = Normalizer()
    n.convert("a", fc("exec_command", {"cmd": "id"}, "c9"), None)
    out = n.convert("a", {"type": "function_call_output", "call_id": "c9", "output": "uid=0(root)"}, None)
    assert (out["kind"], out["tool"], out["call_id"], out["text"]) == ("tool_output", "exec_command", "c9", "uid=0(root)")
    unknown = n.convert("a", {"type": "function_call_output", "call_id": "nope", "output": ""}, None)
    assert unknown["tool"] is None and unknown["text"] == ""


def test_images_are_never_inlined_and_structured_output_is_readable():
    n = Normalizer()
    img = n.convert("a", {"type": "function_call_output", "call_id": "1", "output": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 100000}]}, None)
    assert img["text"] == "[изображение]"
    blocks = n.convert("a", {"type": "function_call_output", "call_id": "2", "output": [{"type": "text", "text": "one"}, {"text": "two"}]}, None)
    assert blocks["text"] == "one\ntwo"
    obj = n.convert("a", {"type": "function_call_output", "call_id": "3", "output": {"status": "ok"}}, None)
    assert '"status": "ok"' in obj["text"]


def test_huge_tool_output_is_clipped():
    e = Normalizer().convert("a", {"type": "function_call_output", "call_id": "1", "output": "x" * 500000}, None)
    assert e["truncated"] and len(e["text"]) < 6500


@pytest.mark.parametrize("item", [None, "str", 5, [], {"type": "reasoning", "summary": []}, {"role": "assistant", "content": ""}, {"role": "tool"}, {}])
def test_irrelevant_items_yield_no_event(item):
    assert Normalizer().convert("a", item, None) is None


# ── reading Strix's files ───────────────────────────────────────────────

def test_roster_is_normalised_with_root_first(tmp_path):
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"statuses": {"b": "Running", "r": "completed", "a": "failed"}, "names": {"r": "Root", "a": "Alpha", "b": "Beta"},
                             "parent_of": {"r": None, "a": "r", "b": "r"}, "errors": {"a": "boom"}}))
    roster = read_roster(p)
    assert [a["id"] for a in roster] == ["r", "a", "b"]
    assert roster[1]["error"] == "boom" and roster[2]["status"] == "running"  # lower-cased


@pytest.mark.parametrize("content", ["", "{bad", "[]", '{"statuses": 5}', '{"nothing": 1}'])
def test_roster_tolerates_garbage(tmp_path, content):
    p = tmp_path / "agents.json"
    p.write_text(content)
    assert read_roster(p) == []
    assert read_roster(tmp_path / "missing.json") == []


def test_read_new_messages_is_incremental_and_tolerates_absence_and_garbage(tmp_path):
    db = tmp_path / "agents.db"
    assert read_new_messages(db, 0) == []                      # not created yet
    db.write_bytes(b"this is not a sqlite file")
    assert read_new_messages(db, 0) == []                      # corrupt
    db.unlink()
    agents = MockAgents(tmp_path)  # creates tmp_path/.state/agents.db
    agents.spawn("r", "Root", None, "task")
    agents.say("r", "hello")
    path = tmp_path / ".state" / "agents.db"
    first = read_new_messages(path, 0)
    assert [r[0] for r in first] == [1, 2]
    agents.say("r", "again")
    assert [r[0] for r in read_new_messages(path, 2)] == [3]
    assert read_new_messages(path, 3) == []
    agents.close()


# ── the tailer ──────────────────────────────────────────────────────────

def make_run(scan_id=1):
    run = scan_dir(scan_id) / "strix_runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    return run


def events(scan_id=1):
    path = activity_path(scan_id)
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_nothing_happens_until_strix_creates_its_run_directory():
    t = ActivityTailer(1)
    assert t.poll() == 0 and not activity_path(1).exists()
    make_run()
    assert t.poll() == 0  # run dir exists but no agents.db yet


def test_tailer_appends_only_new_events_in_order_and_publishes_the_roster():
    run = make_run()
    agents = MockAgents(run)
    t = ActivityTailer(1)
    agents.spawn("agent_root", "Root Agent", None, "do the thing")
    agents.run_command("agent_root", "id", "uid=0")
    assert t.poll() == 3
    agents.say("agent_root", "next step")
    assert t.poll() == 1 and t.poll() == 0
    evs = events()
    assert [e["seq"] for e in evs] == [0, 1, 2, 3]
    assert [e["kind"] for e in evs] == ["task", "tool_call", "tool_output", "message"]
    assert json.loads(agents_path(1).read_text())[0]["name"] == "Root Agent"
    agents.set_status("agent_root", "completed")
    t.poll()
    assert json.loads(agents_path(1).read_text())[0]["status"] == "completed"
    agents.close()


def test_a_corrupt_row_is_skipped_without_losing_the_rest():
    run = make_run()
    agents = MockAgents(run)
    agents.spawn("r", "Root", None, "task")
    agents._conn.execute("INSERT INTO agent_messages (session_id, message_data) VALUES ('r', 'not json')")
    agents.say("r", "after the corrupt row")
    t = ActivityTailer(1)
    t.poll()
    assert [e["kind"] for e in events()] == ["task", "message"]
    agents.close()


def test_secrets_are_redacted_everywhere_in_text_and_arguments():
    run = make_run()
    agents = MockAgents(run)
    agents.spawn("r", "Root", None, "use key sk-live-SECRET-123456 please")
    agents.say("r", "the key is sk-live-SECRET-123456")
    agents.run_command("r", "curl -H 'Authorization: Bearer sk-live-SECRET-123456' x", "echo sk-live-SECRET-123456")
    ActivityTailer(1, secrets=["sk-live-SECRET-123456", "abc"]).poll()
    raw = activity_path(1).read_text()
    assert "SECRET-123456" not in raw and raw.count("***") >= 4
    agents.close()


def test_size_cap_stops_recording_and_says_so(monkeypatch):
    monkeypatch.setattr(activity, "MAX_EVENTS", 5)
    run = make_run()
    agents = MockAgents(run)
    agents.spawn("r", "Root", None, "task")
    for i in range(20):
        agents.say("r", f"message {i}")
    t = ActivityTailer(1)
    t.poll()
    evs = events()
    assert len(evs) == 6 and evs[-1]["kind"] == "system" and "не сохраняется" in evs[-1]["text"]
    agents.say("r", "late")
    assert t.poll() == 0  # stays capped
    agents.close()


def test_stop_flushes_rows_written_just_before_the_process_exited(monkeypatch):
    monkeypatch.setattr(activity, "POLL_SECONDS", 0.05)
    run = make_run()
    agents = MockAgents(run)
    t = ActivityTailer(1)
    t.start()
    agents.spawn("r", "Root", None, "task")
    deadline = time.time() + 3
    while not events() and time.time() < deadline:
        time.sleep(0.05)
    assert events(), "the background thread should have picked up the first rows by itself"
    agents.say("r", "the very last words")
    t.stop()  # no waiting for the next tick: stop() polls once more
    assert events()[-1]["text"] == "the very last words"
    agents.close()


def test_poll_never_raises(monkeypatch):
    t = ActivityTailer(1)
    monkeypatch.setattr(activity, "find_run_dir", lambda _p: (_ for _ in ()).throw(RuntimeError("disk exploded")))
    assert t.poll() == 0
