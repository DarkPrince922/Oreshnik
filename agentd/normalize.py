"""Codex app-server items and notifications -> the event shapes the panel stores and shows.

Items are given the shapes `codex exec --json` used (agent_message, reasoning, command_execution, file_change, ...), so one feed model
renders both. Pure functions: nothing here touches a process or the network.
"""

import os
from typing import Any

MAX_TEXT = 200_000   # a single item's text is cut here (the panel cuts again, shorter, before it stores anything)
STATUS = {"inProgress": "in_progress", "completed": "completed", "failed": "failed", "declined": "declined"}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n[обрезано]"


def relative(path: str, cwd: str) -> str:
    """`path` as shown to the user: relative to the session's working directory when it is inside it."""
    try:
        if cwd and os.path.isabs(path):
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".."):
                return rel
    except ValueError:
        pass
    return path


def command_text(item: dict) -> str:
    """The command as the user would type it: Codex wraps it as `/bin/bash -lc "..."`, its own parse is in `commandActions`."""
    for action in item.get("commandActions") or []:
        if isinstance(action, dict) and _text(action.get("command")):
            return action["command"]
    return _text(item.get("command"))


def normalize_item(item: dict, cwd: str = "", completed: bool = False) -> dict | None:
    """The panel's version of an app-server `ThreadItem`, or None for items that carry nothing to show (the user's own message)."""
    kind = item.get("type")
    ident = _text(item.get("id"))
    # `completed`: the item comes from an `item/completed` notification, so it is finished whatever its own status field says (or omits)
    status = STATUS.get(_text(item.get("status")), "completed" if completed or item.get("completedAtMs") else "in_progress")
    if completed and status == "in_progress":
        status = "completed"
    if kind == "userMessage":
        return None
    if kind == "agentMessage":
        return {"id": ident, "type": "agent_message", "text": _clip(_text(item.get("text")))}
    if kind == "reasoning":
        parts = [p for p in (item.get("summary") or []) if isinstance(p, str)] or [p for p in (item.get("content") or []) if isinstance(p, str)]
        return {"id": ident, "type": "reasoning", "text": _clip("\n\n".join(parts))}
    if kind == "commandExecution":
        return {
            "id": ident, "type": "command_execution", "command": command_text(item), "aggregated_output": _clip(_text(item.get("aggregatedOutput"))),
            "exit_code": item.get("exitCode") if isinstance(item.get("exitCode"), int) else None, "status": status,
            "source": _text(item.get("source")) or "agent",
        }
    if kind == "fileChange":
        changes = []
        for change in item.get("changes") or []:
            if not isinstance(change, dict):
                continue
            how = change.get("kind")
            changes.append({
                "path": relative(_text(change.get("path")), cwd), "kind": _text(how.get("type") if isinstance(how, dict) else how) or "update",
                "diff": _clip(_text(change.get("diff"))),
            })
        return {"id": ident, "type": "file_change", "changes": changes, "status": status}
    if kind == "plan":
        return {"id": ident, "type": "plan_text", "text": _clip(_text(item.get("text")))}
    if kind == "mcpToolCall":
        return {"id": ident, "type": "mcp_tool_call", "server": _text(item.get("server")), "tool": _text(item.get("tool")),
                "arguments": item.get("arguments"), "status": status}
    if kind == "webSearch":
        return {"id": ident, "type": "web_search", "query": _text(item.get("query")), "status": status}
    if kind == "contextCompaction":
        return {"id": ident, "type": "context_compaction", "status": status}
    if kind in ("enteredReviewMode", "exitedReviewMode"):
        return {"id": ident, "type": "review_mode", "entered": kind == "enteredReviewMode", "review": _text(item.get("review"))}
    return {"id": ident, "type": "other", "kind": _text(kind), "status": status}


def plan_steps(plan: Any) -> list[dict]:
    """A `turn/plan/updated` plan as [{"text", "completed", "in_progress"}] (the checklist the page shows)."""
    out = []
    for step in plan if isinstance(plan, list) else []:
        if isinstance(step, dict):
            status = _text(step.get("status"))
            out.append({"text": _text(step.get("step")) or _text(step.get("text")), "completed": status == "completed", "in_progress": status == "inProgress"})
    return out


def usage_from(token_usage: Any, baseline: dict | None) -> dict:
    """The turn's token spend from a `thread/tokenUsage/updated` payload: the thread's running total minus what it was when the turn began."""
    total = (token_usage or {}).get("total") or {}
    base = baseline or {}

    def diff(field: str) -> int:
        value, before = total.get(field), base.get(field)
        return max(0, (value if isinstance(value, int) else 0) - (before if isinstance(before, int) else 0))

    return {
        "input_tokens": diff("inputTokens"), "cached_input_tokens": diff("cachedInputTokens"), "output_tokens": diff("outputTokens"),
        "reasoning_output_tokens": diff("reasoningOutputTokens"),
    }


def approval_from_request(method: str, params: dict, cwd: str = "") -> dict | None:
    """What the user is asked, for a server request that needs an answer; None for requests the panel does not handle."""
    item_id = _text(params.get("itemId"))
    if method == "item/commandExecution/requestApproval":
        return {"kind": "command", "item_id": item_id, "command": command_text(params), "cwd": relative(_text(params.get("cwd")), cwd) or ".",
                "reason": _text(params.get("reason")) or None}
    if method == "item/fileChange/requestApproval":
        return {"kind": "file", "item_id": item_id, "reason": _text(params.get("reason")) or None, "grant_root": _text(params.get("grantRoot")) or None}
    if method == "item/permissions/requestApproval":
        return {"kind": "permissions", "item_id": item_id, "reason": _text(params.get("reason")) or None, "permissions": params.get("permissions")}
    if method == "item/tool/requestUserInput":
        questions = []
        for q in params.get("questions") or []:
            if isinstance(q, dict):
                questions.append({
                    "id": _text(q.get("id")), "header": _text(q.get("header")), "question": _text(q.get("question")), "secret": bool(q.get("isSecret")),
                    "other": bool(q.get("isOther")),
                    "options": [{"label": _text(o.get("label")), "description": _text(o.get("description"))} for o in (q.get("options") or []) if isinstance(o, dict)],
                })
        return {"kind": "input", "item_id": item_id, "questions": questions}
    return None


DECISIONS = {
    "command": {"accept": "accept", "accept_session": "acceptForSession", "decline": "decline", "cancel": "cancel"},
    "file": {"accept": "accept", "accept_session": "acceptForSession", "decline": "decline", "cancel": "cancel"},
}


def approval_result(kind: str, decision: str, answers: dict | None = None) -> dict | None:
    """The JSON-RPC `result` to send for the user's decision, or None when the decision is not valid for this kind of request."""
    if kind in DECISIONS:
        mapped = DECISIONS[kind].get(decision)
        return {"decision": mapped} if mapped else None
    if kind == "permissions":
        return {"permissions": {}} if decision in ("decline", "cancel") else None  # granting extra permissions is not offered
    if kind == "input":
        if decision in ("decline", "cancel"):
            return {"answers": {}}
        if decision == "answer" and isinstance(answers, dict):
            return {"answers": {str(k): {"answers": [str(x) for x in (v if isinstance(v, list) else [v])]} for k, v in answers.items()}}
    return None
