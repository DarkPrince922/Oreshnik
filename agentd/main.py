"""agentd: the only thing that runs a coding agent (Codex CLI) for the panel.

It lives in its own container with NO access to the panel's database, Redis, secrets, or the Docker socket — the agent runs any command it
likes (sandbox policy `danger-full-access`, because Codex's own sandbox cannot run inside a container), so the container itself is the
sandbox. Every request carries `Authorization: Bearer $AGENT_TOKEN`. The model's API key arrives in the /turn body, is put in the Codex
process's environment, and is never written to disk or logged here.

One `codex app-server` process per session (spoken to over stdio JSON-RPC, see rpc.py), started on the first message and ended after
AGENT_IDLE_SECONDS without work; the conversation lives in the session's CODEX_HOME, so the next message resumes it.

  POST /turn            one action of a session (a message, compact, shell command, review, revert) -> a stream of events (application/x-ndjson)
  POST /stop            interrupt the running turn
  POST /steer           add an instruction to the running turn
  POST /approve         answer a pending question of the agent (approve a command, a file change, answer a question)
  POST /sessions/{key}/fork, DELETE /sessions/{key}
  GET/PUT /sessions/{key}/fs|file|archive, POST /sessions/{key}/clone     the session's files
  GET  /health
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.background import BackgroundTask

import fsops
from normalize import approval_from_request, approval_result, normalize_item, plan_steps, usage_from
from rpc import AppServer, RpcError

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR", "/data/agent"))
TOKEN = os.environ.get("AGENT_TOKEN", "")
KEY_ENV = "ORESHNIK_AGENT_API_KEY"
PORT = int(os.environ.get("AGENT_PORT", "8100"))  # where uvicorn listens: Codex reaches the compat proxy below on this port, on loopback
PING_SECONDS = float(os.environ.get("AGENT_PING_SECONDS", "15"))
LIVE_SECONDS = float(os.environ.get("AGENT_LIVE_SECONDS", "0.15"))        # at most this often a snapshot of an item still being written
IDLE_SECONDS = float(os.environ.get("AGENT_IDLE_SECONDS", "900"))          # a session's process is ended after this long without work
MAX_PROCESSES = int(os.environ.get("AGENT_MAX_PROCESSES", "8"))
APPROVAL_TIMEOUT = float(os.environ.get("AGENT_APPROVAL_TIMEOUT_SECONDS", "900"))  # an unanswered question is declined after this
INTERRUPT_WAIT = float(os.environ.get("AGENT_INTERRUPT_WAIT_SECONDS", "10"))
ORPHAN_SECONDS = 30.0   # a turn nobody ever started reading is given up on after this long

_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_THREAD_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}$")
_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,199}$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    reaper = asyncio.create_task(_reaper())
    yield
    reaper.cancel()
    for session in list(_sessions.values()):
        await _stop_server(session)


app = FastAPI(title="Oreshnik agentd", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {TOKEN}"
    if not TOKEN or not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "bad token")


# ── request bodies ───────────────────────────────────────────────────────────────────────────

class Turn(BaseModel):
    session_key: str
    action: Literal["message", "compact", "shell", "review", "revert"] = "message"
    prompt: str = Field(default="", max_length=100_000)     # the message, or the shell command
    thread_id: str | None = None
    model: str
    base_url: str = Field(max_length=500)
    api_key: str = Field(min_length=1, max_length=2000)
    timeout_seconds: int = Field(default=1800, ge=10, le=24 * 3600)
    # Offer the model plain function tools only. A gateway that is not OpenAI itself usually refuses Codex's other tool types with
    # 400 "This endpoint supports function tools only": the built-in web_search and the multi_agent namespace.
    function_tools_only: bool = False
    # Route Codex's requests through the compat proxy below, which completes the `usage` of the final stream event. Some gateways leave
    # `cached_tokens` out and Codex then treats the finished answer as a dropped stream: it asks again up to 5 times and fails.
    normalize_responses: bool = False
    instructions: str | None = Field(default=None, max_length=20_000)   # the session's own system prompt (Codex "developer instructions")
    effort: str | None = None                                           # reasoning effort of the model
    approval_mode: Literal["auto", "ask"] = "auto"                      # ask: Codex asks before commands and file changes
    review_target: dict | None = None
    revert_before: str | None = None

    @field_validator("effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if v not in _EFFORTS:
            raise ValueError("bad effort")
        return v

    @model_validator(mode="after")
    def _by_action(self) -> "Turn":
        if self.action in ("message", "shell") and not self.prompt.strip():
            raise ValueError("prompt is required")
        if self.action == "revert" and not (self.revert_before and _THREAD_RE.match(self.revert_before)):
            raise ValueError("revert_before is required")
        if self.action == "review":
            target = self.review_target or {"type": "uncommittedChanges"}
            kind = target.get("type")
            if kind == "baseBranch" and _BRANCH_RE.match(str(target.get("branch", ""))):
                self.review_target = {"type": "baseBranch", "branch": target["branch"]}
            elif kind == "commit" and _SHA_RE.match(str(target.get("sha", ""))):
                self.review_target = {"type": "commit", "sha": target["sha"], "title": None}
            elif kind == "uncommittedChanges":
                self.review_target = {"type": "uncommittedChanges"}
            else:
                raise ValueError("bad review_target")
        return self


class Stop(BaseModel):
    session_key: str


class Steer(BaseModel):
    session_key: str
    text: str = Field(min_length=1, max_length=100_000)


class Approve(BaseModel):
    session_key: str
    request_id: str = Field(max_length=64)
    decision: Literal["accept", "accept_session", "decline", "cancel", "answer"]
    answers: dict | None = None


class Fork(BaseModel):
    new_key: str


class Clone(BaseModel):
    url: str = Field(max_length=500)


def session_dir(key: str) -> Path:
    if not _SESSION_KEY_RE.match(key):
        raise HTTPException(422, "bad session_key")
    return DATA_DIR / key


def toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


# ── sessions ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class Session:
    key: str
    home: Path
    workdir: Path
    server: AppServer | None = None
    fingerprint: str = ""
    thread_id: str | None = None
    # the turn in progress
    busy: bool = False
    busy_since: float = 0.0
    attached: bool = False                 # a client is reading this turn's stream
    sink: "asyncio.Queue[str | None] | None" = None
    active_turn: str | None = None
    live: dict[str, dict] = field(default_factory=dict)       # item id -> the item as written so far (not finished)
    dirty: set[str] = field(default_factory=set)
    diff: str | None = None
    plan: dict | None = None
    usage_base: dict | None = None
    usage_last: dict | None = None
    pending: dict[str, dict] = field(default_factory=dict)    # request id -> {"id": raw id, "kind": ...} awaiting the user
    compat_token: str | None = None
    last_used: float = field(default_factory=time.monotonic)

    def emit(self, event: dict) -> None:
        if self.sink is not None:
            self.sink.put_nowait(json.dumps(event, ensure_ascii=False))

    def wake(self) -> None:
        if self.sink is not None:
            self.sink.put_nowait(None)


_sessions: dict[str, Session] = {}
_compat: dict[str, str] = {}   # compat proxy: unguessable token -> the upstream base URL (never the key: the caller sends it)


def get_session(key: str) -> Session:
    home = session_dir(key)
    session = _sessions.get(key)
    if session is None:
        session = _sessions[key] = Session(key=key, home=home, workdir=home / "work")
    return session


def prepare_dirs(session: Session) -> None:
    session.workdir.mkdir(parents=True, exist_ok=True)
    (session.home / ".codex").mkdir(parents=True, exist_ok=True)  # Codex refuses to start when CODEX_HOME does not exist


async def init_git(session: Session) -> None:
    """A git repository in the working directory: Codex shows a turn's diff and reviews changes through it."""
    if (session.workdir / ".git").exists():
        return
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    for args in (["init", "-q"], ["config", "user.name", "Oreshnik agent"], ["config", "user.email", "agent@oreshnik.local"]):
        proc = await asyncio.create_subprocess_exec("git", *args, cwd=session.workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()


def spawn_env(turn: Turn, session: Session) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update({
        "HOME": str(session.home), "CODEX_HOME": str(session.home / ".codex"), "TERM": "dumb", "NO_COLOR": "1", KEY_ENV: turn.api_key,
        "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Oreshnik agent", "GIT_AUTHOR_EMAIL": "agent@oreshnik.local",
        "GIT_COMMITTER_NAME": "Oreshnik agent", "GIT_COMMITTER_EMAIL": "agent@oreshnik.local",
    })
    return env


def spawn_argv(turn: Turn, base_url: str) -> list[str]:
    if not _MODEL_RE.match(turn.model):
        raise HTTPException(422, "bad model")
    provider = "{ name = \"Oreshnik\", base_url = " + toml_string(base_url) + f", env_key = \"{KEY_ENV}\", wire_api = \"responses\" }}"
    argv = [CODEX_BIN, "app-server", "-c", f"model={toml_string(turn.model)}", "-c", 'model_provider="oreshnik"', "-c", f"model_providers.oreshnik={provider}"]
    if turn.function_tools_only:
        # Checked with the real Codex 0.169 against a request-logging endpoint: by default it sends function tools plus a `web_search`
        # tool and a `namespace` tool (multi_agent_v1); with these only `function` tools are left (exec_command, write_stdin, view_image…).
        # `goals` and `image_generation` are switched off too, to keep the tool list short for the simpler models behind such gateways.
        argv += ["-c", 'web_search="disabled"', "--disable", "multi_agent", "--disable", "goals", "--disable", "image_generation"]
    return argv


def fingerprint(turn: Turn) -> str:
    """What cannot change under a running process: the endpoint, the key, the tool set and the system prompt. A change restarts it."""
    raw = json.dumps([turn.base_url, turn.api_key, turn.function_tools_only, turn.normalize_responses, turn.instructions or ""])
    return hashlib.sha256(raw.encode()).hexdigest()


def approval_policy(turn: Turn) -> str:
    return "untrusted" if turn.approval_mode == "ask" else "never"


async def _stop_server(session: Session) -> None:
    server, session.server = session.server, None
    if session.compat_token:
        _compat.pop(session.compat_token, None)
        session.compat_token = None
    session.pending.clear()
    if server is not None:
        await server.stop()


async def _make_room() -> None:
    running = [s for s in _sessions.values() if s.server is not None and s.server.alive]
    if len(running) < MAX_PROCESSES:
        return
    idle = sorted((s for s in running if not s.busy and not s.pending), key=lambda s: s.last_used)
    if not idle:
        raise HTTPException(503, f"Слишком много активных сессий агента (не больше {MAX_PROCESSES} одновременно)")
    await _stop_server(idle[0])


async def ensure_server(session: Session, turn: Turn) -> None:
    """The session's app-server process, running and attached to its thread (a new one when the settings that need a restart changed)."""
    prepare_dirs(session)
    await init_git(session)
    fp = fingerprint(turn)
    if session.server is not None and session.server.alive and session.fingerprint == fp:
        return
    await _stop_server(session)
    await _make_room()
    base_url = turn.base_url
    if turn.normalize_responses:
        session.compat_token = secrets.token_urlsafe(16)
        _compat[session.compat_token] = turn.base_url.rstrip("/")
        base_url = f"http://127.0.0.1:{PORT}/compat/{session.compat_token}"
    # The callbacks know which process they belong to: a process being replaced may still report (its own exit, a last notification)
    # while the next one already serves a turn, and that must not reach the new turn.
    holder: list[AppServer] = []
    server = AppServer(
        spawn_argv(turn, base_url), spawn_env(turn, session), str(session.workdir),
        on_notification=lambda m: session.server is holder[0] and on_notification(session, m),
        on_request=lambda m: on_request(session, m, holder[0]),
        on_exit=lambda: on_exit(session, holder[0]),
    )
    holder.append(server)
    session.server = server
    try:
        await server.start()
        await server.request("initialize", {"clientInfo": {"name": "oreshnik", "title": "Oreshnik", "version": "1"}}, timeout=30)
        server.notify("initialized")
        params: dict[str, Any] = {
            "cwd": str(session.workdir), "approvalPolicy": approval_policy(turn), "sandbox": "danger-full-access", "model": turn.model,
            "developerInstructions": turn.instructions or None,
        }
        thread_id = session.thread_id or turn.thread_id
        result = None
        if thread_id:
            try:
                result = await server.request("thread/resume", {"threadId": thread_id, **params}, timeout=60)
            except RpcError:
                result = None  # the thread's files are gone (the container was rebuilt): start over rather than refuse to answer
        if result is None:
            result = await server.request("thread/start", params, timeout=60)
        session.thread_id = result["thread"]["id"]
        session.fingerprint = fp
    except BaseException:
        tail = bytes(server.stderr_tail).decode("utf-8", "replace").replace(turn.api_key, "[key]").strip()
        await _stop_server(session)
        if tail:
            raise HTTPException(502, f"Codex не запустился: {tail[-400:]}")
        raise


# ── what the process tells us ───────────────────────────────────────────────────────────────

def on_exit(session: Session, server: AppServer) -> None:
    if session.server is server and session.busy and session.sink is not None:
        session.emit({"type": "agentd.error", "message": "Процесс Codex завершился во время хода"})
        session.sink.put_nowait("__done__")


def _live_item(session: Session, item_id: str, kind: str) -> dict:
    item = session.live.get(item_id)
    if item is None:
        item = session.live[item_id] = {"id": item_id, "type": kind, **({"text": ""} if kind in ("agent_message", "reasoning", "plan_text") else {})}
        if kind == "command_execution":
            item.update({"command": "", "aggregated_output": "", "exit_code": None, "status": "in_progress", "source": "agent"})
    return item


def _append(item: dict, field_name: str, text: str, cap: int = 200_000) -> None:
    if len(item[field_name]) < cap:
        item[field_name] = (item[field_name] + text)[:cap]


def on_notification(session: Session, message: dict) -> None:
    method = message.get("method", "")
    params = message.get("params") or {}
    thread = params.get("threadId")
    if thread and session.thread_id and thread != session.thread_id:
        return
    cwd = str(session.workdir)

    if method == "turn/started":
        session.active_turn = (params.get("turn") or {}).get("id")
        session.emit({"type": "turn.started", "turn_id": session.active_turn})
    elif method in ("item/started", "item/completed"):
        item = normalize_item(params.get("item") or {}, cwd, completed=(method == "item/completed"))
        if item is None:
            return
        previous = session.live.get(item["id"])
        if previous and item["type"] in ("agent_message", "reasoning") and not item.get("text"):
            item["text"] = previous.get("text", "")  # a finished item carries its full text; a just-started one does not
        if method == "item/started":
            session.live[item["id"]] = item
            session.dirty.add(item["id"])
        else:
            session.live.pop(item["id"], None)
            session.dirty.discard(item["id"])
            session.emit({"type": "item.completed", "item": item})
    elif method == "item/agentMessage/delta":
        _append(_live_item(session, str(params.get("itemId")), "agent_message"), "text", str(params.get("delta") or ""))
        session.dirty.add(str(params.get("itemId")))
    elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
        _append(_live_item(session, str(params.get("itemId")), "reasoning"), "text", str(params.get("delta") or ""))
        session.dirty.add(str(params.get("itemId")))
    elif method == "item/reasoning/summaryPartAdded":
        item = _live_item(session, str(params.get("itemId")), "reasoning")
        if item["text"]:
            _append(item, "text", "\n\n")
    elif method == "item/plan/delta":
        _append(_live_item(session, str(params.get("itemId")), "plan_text"), "text", str(params.get("delta") or ""))
        session.dirty.add(str(params.get("itemId")))
    elif method == "item/commandExecution/outputDelta":
        _append(_live_item(session, str(params.get("itemId")), "command_execution"), "aggregated_output", str(params.get("delta") or ""))
        session.dirty.add(str(params.get("itemId")))
    elif method == "item/fileChange/patchUpdated":
        item = normalize_item({"type": "fileChange", "id": params.get("itemId"), "changes": params.get("changes"), "status": "inProgress"}, cwd)
        if item:
            session.live[item["id"]] = item
            session.dirty.add(item["id"])
    elif method == "turn/diff/updated":
        session.diff = str(params.get("diff") or "")
        session.dirty.add("__diff__")
    elif method == "turn/plan/updated":
        session.plan = {"steps": plan_steps(params.get("plan")), "explanation": params.get("explanation")}
        session.dirty.add("__plan__")
    elif method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage") or {}
        if session.usage_base is None:
            # what the thread had spent before this turn's first model call: its total then minus that call's own part
            total, last = usage.get("total") or {}, usage.get("last") or {}
            session.usage_base = {k: (total.get(k) or 0) - (last.get(k) or 0) for k in total if isinstance(total.get(k), int)}
        session.usage_last = usage
    elif method == "serverRequest/resolved":
        request_id = str(params.get("requestId"))
        session.pending.pop(request_id, None)
        session.emit({"type": "approval.resolved", "request_id": request_id})
    elif method == "warning":
        session.emit({"type": "item.completed", "item": {"id": "", "type": "error", "message": str(params.get("message") or "")}})
    elif method == "error":
        # Not necessarily the end: with `willRetry` Codex is about to ask the model again. Shown quietly; a failed turn reports itself
        # in `turn/completed`.
        err = params.get("error")
        text = (err.get("message") if isinstance(err, dict) else None) or params.get("message") or "Ошибка Codex"
        session.emit({"type": "item.completed", "item": {"id": "", "type": "error", "message": ("Повтор: " if params.get("willRetry") else "") + str(text)}})
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        if turn.get("id") and session.active_turn and turn["id"] != session.active_turn:
            return  # the end of a turn that is not the one being read (e.g. an interrupted one finishing late)
        flush_live(session, force=True)
        if session.diff:
            session.emit({"type": "turn.diff", "diff": session.diff})
        if session.plan and session.plan["steps"]:
            session.emit({"type": "turn.plan", **session.plan})
        error = turn.get("error")
        session.emit({
            "type": "turn.completed", "turn_id": turn.get("id"), "status": turn.get("status") or "completed",
            "error": (error.get("message") if isinstance(error, dict) else error) or None,
            "usage": usage_from(session.usage_last, session.usage_base),
        })
        session.active_turn = None
        if session.sink is not None:
            session.sink.put_nowait("__done__")
    session.wake()


_last_flush: dict[str, float] = {}


def flush_live(session: Session, force: bool = False) -> None:
    """Snapshots of what is still being written (a message so far, a command's output so far, the turn's diff, the plan), at most
    every LIVE_SECONDS — the page shows them as they grow. They are not stored as history; the finished item is."""
    now = time.monotonic()
    if not session.dirty or (not force and now - _last_flush.get(session.key, 0.0) < LIVE_SECONDS):
        return
    _last_flush[session.key] = now
    for key in sorted(session.dirty):
        if key == "__diff__":
            session.emit({"type": "live.diff", "diff": session.diff or ""})
        elif key == "__plan__":
            session.emit({"type": "live.plan", **(session.plan or {})})
        elif key in session.live:
            session.emit({"type": "live", "item": session.live[key]})
    session.dirty.clear()


async def on_request(session: Session, message: dict, server: AppServer) -> None:
    """Something the agent needs answered before it goes on: a command or a file change to approve, a question to the user."""
    if session.server is not server:
        return
    method, params, request_id = message.get("method", ""), message.get("params") or {}, message.get("id")
    approval = approval_from_request(method, params, str(session.workdir))
    if approval is None:
        server.respond(request_id, error={"code": -32601, "message": f"{method} is not supported"})
        return
    key = str(request_id)
    session.pending[key] = {"id": request_id, "kind": approval["kind"]}
    session.emit({"type": "approval.requested", "request_id": key, "approval": approval})
    session.wake()
    await asyncio.sleep(APPROVAL_TIMEOUT)
    if key in session.pending and session.server is server:  # nobody answered in time: refuse, so the turn does not hang for ever
        result = approval_result(approval["kind"], "decline")
        if result is not None:
            server.respond(request_id, result)


# ── the turn ────────────────────────────────────────────────────────────────────────────────────────

async def interrupt(session: Session) -> bool:
    server, turn_id = session.server, session.active_turn
    if server is None or not server.alive or not turn_id or session.thread_id is None:
        return False
    for key, pending in list(session.pending.items()):  # a turn waiting for an answer would never see the interrupt
        result = approval_result(pending["kind"], "cancel")
        if result is not None:
            server.respond(pending["id"], result)
    try:
        await server.request("turn/interrupt", {"threadId": session.thread_id, "turnId": turn_id}, timeout=10)
    except (RpcError, ConnectionError, TimeoutError):
        return False
    return True


async def dispatch(session: Session, turn: Turn) -> None:
    """Hand the action to Codex. The answers come back as notifications into `session.sink`."""
    server, thread = session.server, session.thread_id
    assert server is not None and thread is not None
    if turn.action == "message":
        params: dict[str, Any] = {"threadId": thread, "input": [{"type": "text", "text": turn.prompt, "text_elements": []}], "model": turn.model,
                                  "approvalPolicy": approval_policy(turn)}
        if turn.effort:
            params["effort"] = turn.effort
        await server.request("turn/start", params, timeout=60)
    elif turn.action == "compact":
        await server.request("thread/compact/start", {"threadId": thread}, timeout=60)
    elif turn.action == "shell":
        await server.request("thread/shellCommand", {"threadId": thread, "command": turn.prompt, "timeoutMs": turn.timeout_seconds * 1000}, timeout=60)
    elif turn.action == "review":
        await server.request("review/start", {"threadId": thread, "target": turn.review_target}, timeout=60)
    elif turn.action == "revert":
        await server.request("thread/revert", {"threadId": thread, "beforeTurnId": turn.revert_before}, timeout=60)


async def _events(session: Session, turn: Turn, queue: "asyncio.Queue[str | None]"):
    """The lines of the response: events as they come, a ping while nothing does, and at the end an `agentd.exit` line."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + turn.timeout_seconds
    timed_out = False
    finished = False
    session.attached = True
    try:
        if turn.action == "revert":
            yield json.dumps({"type": "thread.reverted"}) + "\n"
            finished = True
        while not finished:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(queue.get(), min(PING_SECONDS, LIVE_SECONDS if session.dirty else PING_SECONDS, remaining))
            except TimeoutError:
                line = None
                if not session.dirty:
                    yield '{"type":"agentd.ping"}\n'
            flush_live(session)
            while True:  # everything the handler queued meanwhile, in order
                if line == "__done__":
                    finished = True
                elif line:
                    yield line + "\n"
                try:
                    line = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if finished:
                while not queue.empty():
                    tail = queue.get_nowait()
                    if tail and tail != "__done__":
                        yield tail + "\n"
    finally:
        # Reached on a normal end, a timeout, and when the panel hangs up mid-turn: a turn nobody reads must not go on.
        if not finished:
            # shielded: when the panel hangs up, the task is being cancelled, and a cancelled scope would cut every `await` below short
            with anyio.CancelScope(shield=True):
                await interrupt(session)
                deadline_wait = loop.time() + INTERRUPT_WAIT
                while session.active_turn and loop.time() < deadline_wait:
                    await asyncio.sleep(0.1)
                if session.active_turn and session.server is not None:
                    await _stop_server(session)  # it did not stop: end the process (the thread stays on disk)
        session.sink = None
        session.busy = False
        session.attached = False
        session.active_turn = None
        session.last_used = time.monotonic()
    while not queue.empty():  # after a timeout or an interrupt the turn's last events (its end, "interrupted") are still in the queue
        rest = queue.get_nowait()
        if rest and rest != "__done__":
            yield rest + "\n"
    tail = ""
    if session.server is not None:
        tail = bytes(session.server.stderr_tail).decode("utf-8", "replace").replace(turn.api_key, "[key]")
    yield json.dumps({"type": "agentd.exit", "code": 0, "timed_out": timed_out, "lingered": False, "stderr": tail[-1000:] if timed_out else ""}) + "\n"


@app.post("/turn", dependencies=[Depends(require_token)])
async def run_turn(body: Turn) -> StreamingResponse:
    session = get_session(body.session_key)
    if body.thread_id is not None and not _THREAD_RE.match(body.thread_id):
        raise HTTPException(422, "bad thread_id")
    if session.busy:
        raise HTTPException(409, "a turn of this session is already running")
    session.busy, session.busy_since, session.attached = True, time.monotonic(), False
    queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    session.sink = queue
    session.live.clear()
    session.dirty.clear()
    session.diff = session.plan = session.usage_base = session.usage_last = None
    try:
        await ensure_server(session, body)
        queue.put_nowait(json.dumps({"type": "thread.started", "thread_id": session.thread_id}))
        await dispatch(session, body)
    except HTTPException:
        session.sink, session.busy = None, False
        raise
    except RpcError as exc:
        session.sink, session.busy = None, False
        raise HTTPException(502, str(exc)[:400])
    except (ConnectionError, TimeoutError, OSError) as exc:
        session.sink, session.busy = None, False
        await _stop_server(session)
        raise HTTPException(502, f"Codex не отвечает ({exc.__class__.__name__})")
    return StreamingResponse(_events(session, body, queue), media_type="application/x-ndjson")


@app.post("/stop", dependencies=[Depends(require_token)])
async def stop(body: Stop) -> dict:
    session = _sessions.get(body.session_key)
    if session is None or not session.busy:
        return {"stopped": False}
    return {"stopped": await interrupt(session)}


@app.post("/steer", dependencies=[Depends(require_token)])
async def steer(body: Steer) -> dict:
    session = _sessions.get(body.session_key)
    if session is None or not session.busy or not session.active_turn or session.server is None or session.thread_id is None:
        raise HTTPException(409, "no turn is running")
    try:
        await session.server.request("turn/steer", {
            "threadId": session.thread_id, "expectedTurnId": session.active_turn, "input": [{"type": "text", "text": body.text, "text_elements": []}],
        }, timeout=30)
    except RpcError as exc:
        raise HTTPException(409, str(exc)[:300])
    except (ConnectionError, TimeoutError):
        raise HTTPException(502, "Codex не отвечает")
    return {"steered": True}


@app.post("/approve", dependencies=[Depends(require_token)])
async def approve(body: Approve) -> dict:
    session = _sessions.get(body.session_key)
    pending = session.pending.get(body.request_id) if session else None
    if session is None or pending is None or session.server is None:
        raise HTTPException(404, "Вопрос уже закрыт")
    result = approval_result(pending["kind"], body.decision, body.answers)
    if result is None:
        raise HTTPException(422, "Такой ответ здесь невозможен")
    session.server.respond(pending["id"], result)
    return {"answered": True}


@app.post("/sessions/{session_key}/fork", dependencies=[Depends(require_token)])
async def fork(session_key: str, body: Fork) -> dict:
    """A copy of the session — its files, its conversation — under a new key. The thread id stays: the copy has its own CODEX_HOME."""
    source = get_session(session_key)
    if source.busy:
        raise HTTPException(409, "Сначала дождитесь конца хода")
    target = session_dir(body.new_key)
    if target.exists():
        raise HTTPException(409, "Такая сессия уже есть")
    if not source.home.exists():
        raise HTTPException(404, "У сессии ещё нет данных")
    await _stop_server(source)  # its database is copied: nothing may be writing to it
    await asyncio.to_thread(shutil.copytree, source.home, target, symlinks=True)
    copy = get_session(body.new_key)
    copy.thread_id = source.thread_id
    return {"thread_id": source.thread_id}


@app.delete("/sessions/{session_key}", dependencies=[Depends(require_token)])
async def delete_session(session_key: str) -> dict:
    session = get_session(session_key)
    if session.busy:
        raise HTTPException(409, "a turn of this session is running")
    await _stop_server(session)
    _sessions.pop(session_key, None)
    shutil.rmtree(session_dir(session_key), ignore_errors=True)
    return {"deleted": True}


# ── files ─────────────────────────────────────────────────────────────────────────────────────────

def _root(key: str) -> Path:
    session = get_session(key)
    session.workdir.mkdir(parents=True, exist_ok=True)
    return session.workdir


def _fs(call):
    try:
        return call()
    except fsops.FsError as exc:
        raise HTTPException(exc.status, exc.message)


@app.get("/sessions/{session_key}/fs", dependencies=[Depends(require_token)])
async def files_list(session_key: str, path: str = "") -> dict:
    return _fs(lambda: fsops.list_dir(_root(session_key), path))


@app.get("/sessions/{session_key}/file", dependencies=[Depends(require_token)])
async def file_read(session_key: str, path: str, download: bool = False) -> Response:
    root = _root(session_key)
    if download:
        target = _fs(lambda: fsops.resolve(root, path))
        if not target.is_file():
            raise HTTPException(404, "Файла нет")
        return FileResponse(target, filename=target.name, headers={"X-Content-Type-Options": "nosniff"})
    return Response(json.dumps(_fs(lambda: fsops.read_preview(root, path)), ensure_ascii=False), media_type="application/json")


@app.put("/sessions/{session_key}/file", dependencies=[Depends(require_token)])
async def file_write(session_key: str, path: str, request: Request) -> dict:
    root = _root(session_key)
    try:
        size = await fsops.write_stream(root, path, request.stream())
    except fsops.FsError as exc:
        raise HTTPException(exc.status, exc.message)
    return {"path": path, "size": size}


@app.post("/sessions/{session_key}/clone", dependencies=[Depends(require_token)])
async def clone(session_key: str, body: Clone) -> dict:
    root = _root(session_key)
    try:
        url, dest = fsops.clone_target(root, body.url)
        await fsops.git_clone(url, dest, {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ})
    except fsops.FsError as exc:
        raise HTTPException(exc.status, exc.message)
    return {"path": dest.name}


@app.get("/sessions/{session_key}/archive", dependencies=[Depends(require_token)])
async def archive(session_key: str) -> FileResponse:
    root = _root(session_key)
    try:
        path = await asyncio.to_thread(fsops.make_archive, root, DATA_DIR / ".tmp")
    except fsops.FsError as exc:
        raise HTTPException(exc.status, exc.message)
    return FileResponse(path, filename=f"{session_key}.zip", media_type="application/zip", background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


# ── housekeeping ───────────────────────────────────────────────────────────────────────────────────

async def _reaper() -> None:
    """Ends processes of sessions that have been idle for long, and gives up on turns whose stream nobody ever started reading."""
    while True:
        await asyncio.sleep(30)
        now = time.monotonic()
        for session in list(_sessions.values()):
            if session.busy and not session.attached and now - session.busy_since > ORPHAN_SECONDS:
                await interrupt(session)
                await _stop_server(session)
                session.sink, session.busy, session.active_turn = None, False, None
            elif session.server is not None and not session.busy and not session.pending and now - session.last_used > IDLE_SECONDS:
                await _stop_server(session)


_codex_check: tuple[float, bool, str] | None = None
CODEX_CHECK_SECONDS = 60.0


async def codex_status() -> tuple[bool, str]:
    """Whether Codex really STARTS (`codex --version`), not merely whether a file called codex is on the PATH: the launcher can be
    installed while its platform binary is missing. Remembered for a minute so polling the health does not spawn a process each time."""
    global _codex_check
    now = asyncio.get_running_loop().time()
    if _codex_check is not None and now - _codex_check[0] < CODEX_CHECK_SECONDS:
        return _codex_check[1], _codex_check[2]
    if shutil.which(CODEX_BIN) is None:
        ok, detail = False, f"{CODEX_BIN}: не найден в PATH"
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                CODEX_BIN, "--version", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={k: os.environ[k] for k in _PASSTHROUGH if k in os.environ} | {"HOME": str(DATA_DIR), "CODEX_HOME": str(DATA_DIR / ".codex-check")},
            )
            out, err = await asyncio.wait_for(proc.communicate(), 15)
            text = (out or err).decode("utf-8", "replace").strip()
            ok, detail = proc.returncode == 0, (text.splitlines()[0] if proc.returncode == 0 and text else text[-300:] or f"код {proc.returncode}")
        except (OSError, TimeoutError) as exc:
            ok, detail = False, f"не запускается ({exc.__class__.__name__})"
    _codex_check = (now, ok, detail)
    return ok, detail


@app.get("/health")
async def health() -> dict:
    ok, detail = await codex_status()
    return {"ok": True, "codex": ok, "codex_detail": detail, "sessions": sum(1 for s in _sessions.values() if s.server is not None and s.server.alive)}


# ── the compat proxy: a gateway's answers, made digestible for Codex ─────────────────────────────
#
# Reproduced with the real Codex 0.169 against an endpoint whose final `response.completed` event carries `input_tokens_details` without
# `cached_tokens`: Codex fails to parse it ("stream disconnected before completion: failed to parse ResponseCompleted: missing field
# `cached_tokens`"), repeats the whole request, and after its retries fails the turn. The proxy changes nothing but the `usage` numbers of
# the final events, filling in what is missing with zeros; everything else passes through byte for byte.

_LOOPBACK = {"127.0.0.1", "::1"}
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
        "host", "content-length", "content-encoding", "accept-encoding"}
_FINAL_EVENTS = {"response.completed", "response.incomplete", "response.failed", "response.done"}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def fix_usage(usage: dict) -> dict:
    """`usage` with every number Codex's parser insists on present: the `*_tokens_details` objects with `cached_tokens` / `reasoning_tokens`."""
    usage = dict(usage)
    if "input_tokens" not in usage and "prompt_tokens" in usage:  # a chat-completions-style usage in a Responses stream
        usage["input_tokens"] = usage["prompt_tokens"]
    if "output_tokens" not in usage and "completion_tokens" in usage:
        usage["output_tokens"] = usage["completion_tokens"]
    usage["input_tokens"] = _count(usage.get("input_tokens"))
    usage["output_tokens"] = _count(usage.get("output_tokens"))
    usage["total_tokens"] = _count(usage.get("total_tokens")) or usage["input_tokens"] + usage["output_tokens"]
    for details, field_name in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        value = usage.get(details)
        value = dict(value) if isinstance(value, dict) else {}
        value[field_name] = _count(value.get(field_name))
        usage[details] = value
    return usage


def fix_sse_line(line: str) -> str:
    """One line of the event stream, with the final events' `usage` completed; any other line is returned as it came."""
    if not line.startswith("data:") or '"usage"' not in line:
        return line
    try:
        event = json.loads(line[5:].strip())
    except ValueError:
        return line
    response = event.get("response") if isinstance(event, dict) else None
    if not isinstance(response, dict) or event.get("type") not in _FINAL_EVENTS or not isinstance(response.get("usage"), dict):
        return line
    response["usage"] = fix_usage(response["usage"])
    return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))


@app.api_route("/compat/{token}/{path:path}", methods=["GET", "POST"])
async def compat(token: str, path: str, request: Request) -> Response:
    upstream = _compat.get(token)
    if upstream is None or request.client is None or request.client.host not in _LOOPBACK:
        raise HTTPException(404)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    upstream_request = client.build_request(
        request.method, f"{upstream}/{path}", params=request.query_params, headers=headers, content=await request.body()
    )
    try:
        reply = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"upstream: {exc.__class__.__name__}") from exc
    out_headers = {k: v for k, v in reply.headers.items() if k.lower() not in _HOP}
    if "text/event-stream" not in reply.headers.get("content-type", ""):
        body = await reply.aread()
        await reply.aclose()
        await client.aclose()
        return Response(body, status_code=reply.status_code, headers=out_headers)

    async def events():
        try:
            async for line in reply.aiter_lines():
                yield fix_sse_line(line) + "\n"
        finally:
            await reply.aclose()
            await client.aclose()

    return StreamingResponse(events(), status_code=reply.status_code, headers=out_headers, media_type="text/event-stream")
