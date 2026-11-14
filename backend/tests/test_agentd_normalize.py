"""agentd/normalize.py: Codex app-server items and requests -> what the panel stores (pure functions)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agentd"))

from normalize import approval_from_request, approval_result, normalize_item, plan_steps, relative, usage_from  # noqa: E402


def test_a_message_and_its_reasoning_keep_their_text():
    assert normalize_item({"type": "agentMessage", "id": "m", "text": "hi"}) == {"id": "m", "type": "agent_message", "text": "hi"}
    assert normalize_item({"type": "reasoning", "id": "r", "summary": ["a", "b"], "content": []}) == {"id": "r", "type": "reasoning", "text": "a\n\nb"}
    assert normalize_item({"type": "reasoning", "id": "r", "summary": [], "content": ["raw"]})["text"] == "raw"


def test_the_users_own_message_is_dropped():
    assert normalize_item({"type": "userMessage", "id": "u", "content": []}) is None


def test_a_command_shows_what_was_typed_not_the_shell_wrapper():
    item = {"type": "commandExecution", "id": "c", "command": '/bin/bash -lc "ls -la"', "commandActions": [{"type": "unknown", "command": "ls -la"}],
            "status": "completed", "aggregatedOutput": "x\n", "exitCode": 0, "source": "unifiedExecStartup"}
    assert normalize_item(item) == {"id": "c", "type": "command_execution", "command": "ls -la", "aggregated_output": "x\n", "exit_code": 0, "status": "completed", "source": "unifiedExecStartup"}
    plain = normalize_item({"type": "commandExecution", "id": "c", "command": "ls", "status": "inProgress"})
    assert plain["command"] == "ls" and plain["status"] == "in_progress" and plain["exit_code"] is None and plain["aggregated_output"] == ""


def test_statuses_are_translated_and_a_finished_item_is_finished_even_without_one():
    assert normalize_item({"type": "commandExecution", "id": "c", "status": "declined"})["status"] == "declined"
    assert normalize_item({"type": "contextCompaction", "id": "x"})["status"] == "in_progress"
    assert normalize_item({"type": "contextCompaction", "id": "x"}, completed=True)["status"] == "completed"
    assert normalize_item({"type": "commandExecution", "id": "c", "status": "inProgress"}, completed=True)["status"] == "completed"


def test_file_changes_get_relative_paths_kinds_and_diffs():
    item = {"type": "fileChange", "id": "f", "status": "completed", "changes": [
        {"path": "/work/a.txt", "kind": {"type": "update", "move_path": None}, "diff": "@@\n-a\n+b\n"}, {"path": "/work/sub/n.txt", "kind": {"type": "add"}, "diff": "x"},
        {"path": "/elsewhere/z", "kind": {"type": "delete"}, "diff": ""}, "junk"]}
    out = normalize_item(item, "/work")
    assert out["changes"] == [{"path": "a.txt", "kind": "update", "diff": "@@\n-a\n+b\n"}, {"path": "sub/n.txt", "kind": "add", "diff": "x"}, {"path": "/elsewhere/z", "kind": "delete", "diff": ""}]


def test_relative_paths():
    assert relative("/w/a/b", "/w") == "a/b" and relative("/x/y", "/w") == "/x/y" and relative("rel", "/w") == "rel" and relative("/w/a", "") == "/w/a"


def test_other_items_are_kept_not_dropped():
    assert normalize_item({"type": "mcpToolCall", "id": "t", "server": "s", "tool": "x", "arguments": {"a": 1}, "status": "completed"})["tool"] == "x"
    assert normalize_item({"type": "webSearch", "id": "w", "query": "q"})["query"] == "q"
    assert normalize_item({"type": "enteredReviewMode", "id": "e", "review": "r"}) == {"id": "e", "type": "review_mode", "entered": True, "review": "r"}
    assert normalize_item({"type": "exitedReviewMode", "id": "e"})["entered"] is False
    assert normalize_item({"type": "somethingNew", "id": "n"})["type"] == "other"


def test_a_huge_text_is_cut():
    assert len(normalize_item({"type": "agentMessage", "id": "m", "text": "x" * 500_000})["text"]) < 201_000


def test_a_plan_becomes_a_checklist():
    assert plan_steps([{"step": "a", "status": "completed"}, {"step": "b", "status": "inProgress"}, {"step": "c", "status": "pending"}, "junk"]) == [
        {"text": "a", "completed": True, "in_progress": False}, {"text": "b", "completed": False, "in_progress": True}, {"text": "c", "completed": False, "in_progress": False}]
    assert plan_steps(None) == []


def test_a_turns_usage_is_the_threads_total_minus_what_it_was_before():
    usage = {"total": {"inputTokens": 1500, "cachedInputTokens": 100, "outputTokens": 70, "reasoningOutputTokens": 5}}
    before = {"inputTokens": 1000, "cachedInputTokens": 100, "outputTokens": 20, "reasoningOutputTokens": 0}
    assert usage_from(usage, before) == {"input_tokens": 500, "cached_input_tokens": 0, "output_tokens": 50, "reasoning_output_tokens": 5}
    assert usage_from(usage, None)["input_tokens"] == 1500 and usage_from(None, None)["output_tokens"] == 0
    assert usage_from({"total": {"inputTokens": 5}}, {"inputTokens": 9})["input_tokens"] == 0  # never negative


def test_a_command_question():
    asked = approval_from_request("item/commandExecution/requestApproval", {"itemId": "c", "command": "/bin/bash -lc x", "commandActions": [{"command": "x"}], "cwd": "/work/sub", "reason": None}, "/work")
    assert asked == {"kind": "command", "item_id": "c", "command": "x", "cwd": "sub", "reason": None}
    assert approval_from_request("item/fileChange/requestApproval", {"itemId": "f", "reason": "outside", "grantRoot": "/"})["kind"] == "file"


def test_a_question_to_the_user():
    asked = approval_from_request("item/tool/requestUserInput", {"itemId": "q", "questions": [
        {"id": "a", "header": "H", "question": "Q?", "isSecret": True, "isOther": True, "options": [{"label": "x", "description": "d"}]}, "junk"]})
    assert asked == {"kind": "input", "item_id": "q", "questions": [{"id": "a", "header": "H", "question": "Q?", "secret": True, "other": True, "options": [{"label": "x", "description": "d"}]}]}


def test_requests_the_panel_cannot_answer_are_not_offered():
    assert approval_from_request("mcpServer/elicitation/request", {}) is None and approval_from_request("item/tool/call", {}) is None


def test_decisions_become_what_codex_expects():
    assert approval_result("command", "accept") == {"decision": "accept"} and approval_result("file", "accept_session") == {"decision": "acceptForSession"}
    assert approval_result("command", "decline") == {"decision": "decline"} and approval_result("command", "cancel") == {"decision": "cancel"}
    assert approval_result("command", "answer", {"a": "b"}) is None and approval_result("command", "nonsense") is None
    assert approval_result("input", "answer", {"a": "b", "c": ["d", "e"]}) == {"answers": {"a": {"answers": ["b"]}, "c": {"answers": ["d", "e"]}}}
    assert approval_result("input", "decline") == {"answers": {}} and approval_result("input", "accept") is None and approval_result("input", "answer", None) is None
    assert approval_result("permissions", "decline") == {"permissions": {}} and approval_result("permissions", "accept") is None  # extra permissions are never granted
