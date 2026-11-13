"""What the Strix agents are doing: commands, messages, sub-agents.

In headless mode (`-n`) Strix prints only a banner and its findings to stdout. The agents' actual
work is persisted in the run directory (strix-agent 1.7.0, verified with the real library):

    <run>/.state/agents.json   {"statuses": {id: status}, "names": {...}, "parent_of": {...}, "errors": {...}}
    <run>/.state/agents.db     SQLite (WAL), table agent_messages(id, session_id=agent id, message_data, created_at)
                               message_data is an OpenAI-Agents-SDK item:
                                 {"role": "user"|"assistant", "content": str | [{"type":"output_text","text":..}]}
                                 {"type": "function_call", "call_id", "name", "arguments": "<json>"}
                                 {"type": "function_call_output", "call_id", "output"}
                                 {"type": "reasoning", ...}                       (ignored)

The worker runs an ActivityTailer next to the Strix process. It converts those rows into a stable,
size-bounded JSON-lines file (`activity.jsonl`) and a roster (`agents.json`) inside the scan
directory; the API then serves them like the log, independent of Strix's internal format and of
SQLite locking. Everything here is untrusted text (commands and tool output quote the target).
"""

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.scan_files import activity_path, agents_path, find_run_dir, scan_dir

log = logging.getLogger("oreshnik.activity")

MAX_EVENTS = 20_000
MAX_FILE_BYTES = 50_000_000
POLL_SECONDS = 1.0

CLIP_MESSAGE = 6000
CLIP_OUTPUT = 6000
CLIP_ARG = 3000
CLIP_SUMMARY = 300

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t and \n

# Turns the *system* injects as "user" messages (same list Strix's own viewer uses to tell them apart).
_SKIP_PREFIXES = (
    "== Inherited context from parent",
    "Your previous message ended a turn without a tool call.",
    "Your previous response ended the autonomous run without a lifecycle tool call.",
)
_AGENT_MESSAGE_PREFIX = "[Message from "
_BUDGET_PREFIXES = tuple(
    f"[{label}] {subject}" for label in ("NOTICE", "URGENT", "CRITICAL") for subject in ("Turn budget:", "Scan cost budget:")
)


# ── pure helpers (unit-tested) ───────────────────────────────────────────


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Strip ANSI/control characters and shorten to `limit`, keeping the head and the tail."""
    text = _CTRL.sub("", _ANSI.sub("", text))
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [пропущено символов: {omitted}] …\n{text[-tail:]}", True


def _text_of(content: Any) -> str:
    """Text of an SDK message `content` (a string or a list of parts)."""
    parts = content if isinstance(content, list) else [content]
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                out.append(text)
            elif "image_url" in part or part.get("type") in ("input_image", "image"):
                out.append("[изображение]")
    return "".join(out)


def _output_text(output: Any) -> str:
    """Text of a tool result: a string, a list of blocks, or a dict. Images are never inlined."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (list, dict)):
        if isinstance(output, dict) and ("image_url" in output or output.get("type") in ("image", "input_image")):
            return "[изображение]"
        if isinstance(output, list):
            parts = [_output_text(o) for o in output]
            return "\n".join(p for p in parts if p)
        if isinstance(output.get("text"), str):
            return output["text"]
        try:
            return json.dumps(output, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(output)
    return str(output)


def _clip_args(value: Any) -> tuple[Any, bool]:
    """Clip every string inside an arguments structure (they can contain whole scripts)."""
    if isinstance(value, str):
        return clip(value, CLIP_ARG)
    if isinstance(value, dict):
        out, cut = {}, False
        for k, v in list(value.items())[:50]:
            out[str(k)[:100]], c = _clip_args(v)
            cut = cut or c
        return out, cut
    if isinstance(value, list):
        out, cut = [], False
        for v in value[:50]:
            c_v, c = _clip_args(v)
            out.append(c_v)
            cut = cut or c
        return out, cut
    return value, False


def summarize_call(tool: str, args: dict[str, Any]) -> str:
    """One line describing a tool call: the shell command, the task given to a new agent, ..."""

    def first(*keys: str) -> str:
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    if tool in ("exec_command", "shell", "bash", "run_command"):
        text = first("cmd", "command")
    elif tool == "write_stdin":
        text = "stdin ← " + first("chars", "input")
    elif tool == "create_agent":
        text = f"{first('name')}: {first('task')}".strip(": ")
    elif tool in ("send_message_to_agent", "stop_agent", "agent_finish", "wait_for_agents"):
        text = first("message", "summary", "agent_id", "reason", "result")
    else:
        text = first("url", "query", "title", "name", "path", "command", "cmd", "task", "message")
        if not text and args:
            try:
                text = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(args)
    text, _ = clip(" ".join(text.split()), CLIP_SUMMARY)
    return text


def _iso(created_at: Any) -> str:
    """SQLite CURRENT_TIMESTAMP is naive UTC ('YYYY-MM-DD HH:MM:SS')."""
    if isinstance(created_at, str):
        try:
            parsed = datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Normalizer:
    """Turns raw SDK items into events. Stateful: needs to pair outputs with calls and spot task turns."""

    calls: dict[str, str] = field(default_factory=dict)      # call_id -> tool name
    seen_user: set[str] = field(default_factory=set)         # agents whose first user turn was already consumed

    def convert(self, agent: str, item: Any, created_at: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        ts = _iso(created_at)
        item_type, role = item.get("type"), item.get("role")

        if role in ("user", "assistant") and item_type in (None, "message"):
            text = _text_of(item.get("content", "")).strip()
            if not text:
                return None
            if role == "user":
                first_turn = agent not in self.seen_user
                self.seen_user.add(agent)
                if text.startswith(_SKIP_PREFIXES):
                    return None
                if text.startswith(_AGENT_MESSAGE_PREFIX):
                    return self._event(agent, ts, "agent_message", text, CLIP_MESSAGE)
                if text.startswith(_BUDGET_PREFIXES):
                    return self._event(agent, ts, "system", text, 800)
                if first_turn:
                    return self._event(agent, ts, "task", text, CLIP_MESSAGE)
            return self._event(agent, ts, "message", text, CLIP_MESSAGE, role=role)

        if item_type == "function_call":
            tool = str(item.get("name") or "tool")[:100]
            call_id = str(item.get("call_id") or item.get("id") or "")[:200]
            raw = item.get("arguments")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {"raw": raw}
            args, cut = _clip_args(raw if isinstance(raw, dict) else {"value": raw})
            if call_id:
                self.calls[call_id] = tool
            return {
                "ts": ts, "agent": agent, "kind": "tool_call", "tool": tool, "call_id": call_id,
                "args": args, "summary": summarize_call(tool, args if isinstance(args, dict) else {}),
                "text": None, "truncated": cut,
            }

        if item_type == "function_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "")[:200]
            event = self._event(agent, ts, "tool_output", _output_text(item.get("output")), CLIP_OUTPUT, allow_empty=True)
            event["call_id"] = call_id
            event["tool"] = self.calls.get(call_id)
            return event
        return None  # reasoning items and anything unknown

    @staticmethod
    def _event(agent: str, ts: str, kind: str, text: str, limit: int, *, role: str | None = None, allow_empty: bool = False) -> dict[str, Any]:
        clipped, cut = clip(text, limit)
        return {"ts": ts, "agent": agent, "kind": kind, "role": role, "text": clipped, "truncated": cut,
                "tool": None, "call_id": None, "args": None, "summary": None}


# ── reading Strix's files ────────────────────────────────────────────────


def read_new_messages(db_path: Path, after_id: int, limit: int = 500) -> list[tuple[int, str, str, str]]:
    """Rows of agent_messages with id > after_id. Read-only; tolerant of a missing/locked database."""
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            return conn.execute(
                "select id, session_id, message_data, created_at from agent_messages where id > ? order by id limit ?",
                (after_id, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug("agents.db not readable yet: %s", exc)
        return []


def read_roster(path: Path) -> list[dict[str, Any]]:
    """Normalised agent list from Strix's agents.json (root first, then by name)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("statuses"), dict):
        return []
    names, parents, errors = (data.get(k) if isinstance(data.get(k), dict) else {} for k in ("names", "parent_of", "errors"))
    roster = []
    for agent_id, status in data["statuses"].items():
        if not isinstance(agent_id, str):
            continue
        parent = parents.get(agent_id)
        error = errors.get(agent_id)
        roster.append({
            "id": agent_id[:200],
            "name": clip(str(names.get(agent_id) or agent_id), 120)[0],
            "parent_id": parent if isinstance(parent, str) else None,
            "status": str(status).lower()[:30],
            "error": clip(str(error), 500)[0] if error else None,
        })
    roster.sort(key=lambda a: (a["parent_id"] is not None, a["name"].lower()))
    return roster


# ── the worker-side tailer ───────────────────────────────────────────────


class ActivityTailer:
    """Copies agent activity from the Strix run directory into the scan's own files.

    `poll()` is one cycle (also used directly in tests and for the final flush); `start()`/`stop()`
    run it every second on a thread. Nothing in here may ever break a scan: errors are logged and dropped.
    """

    def __init__(self, scan_id: int, secrets: list[str] | None = None):
        self.scan_id = scan_id
        self.work_dir = scan_dir(scan_id)
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]
        self._norm = Normalizer()
        self._last_id = 0
        self._seq = 0
        self._bytes = 0
        self._capped = False
        self._roster_json = ""
        self._run_dir: Path | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- one cycle
    def poll(self) -> int:
        """Returns the number of events appended."""
        try:
            if self._run_dir is None:
                self._run_dir = find_run_dir(self.work_dir)
                if self._run_dir is None:
                    return 0
            state = self._run_dir / ".state"
            self._write_roster(state / "agents.json")
            return self._append_events(state / "agents.db")
        except Exception:
            log.exception("activity poll failed for scan %s", self.scan_id)
            return 0

    def _redact(self, value: Any) -> Any:
        if not self.secrets:
            return value
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "***")
            return value
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _write_roster(self, source: Path) -> None:
        roster = read_roster(source)
        if not roster:
            return
        serialized = json.dumps(roster, ensure_ascii=False)
        if serialized == self._roster_json:
            return
        target = agents_path(self.scan_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(serialized, encoding="utf-8")
        tmp.replace(target)  # atomic: the API never sees a half-written roster
        self._roster_json = serialized

    def _append_events(self, db_path: Path) -> int:
        appended = 0
        while not self._capped:
            rows = read_new_messages(db_path, self._last_id)
            if not rows:
                break
            out = activity_path(self.scan_id)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                for row_id, agent, raw, created_at in rows:
                    self._last_id = row_id
                    try:
                        item = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    event = self._norm.convert(str(agent), item, created_at)
                    if event is None:
                        continue
                    if self._seq >= MAX_EVENTS or self._bytes >= MAX_FILE_BYTES:
                        event = {"ts": event["ts"], "agent": event["agent"], "kind": "system", "role": None, "tool": None,
                                 "call_id": None, "args": None, "summary": None, "truncated": False,
                                 "text": "Дальнейшая активность агентов не сохраняется: достигнут лимит размера."}
                        self._capped = True
                    event = self._redact(event)
                    event["seq"] = self._seq
                    self._seq += 1
                    line = json.dumps(event, ensure_ascii=False) + "\n"
                    fh.write(line)
                    self._bytes += len(line.encode("utf-8"))
                    appended += 1
                    if self._capped:
                        break
            if len(rows) < 500:
                break
        return appended

    # -- thread control
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"activity-{self.scan_id}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            self.poll()

    def stop(self) -> None:
        """Stop the thread, then flush once more so the last rows written before exit are not lost."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.poll()
