"""agentd: the only thing that runs a coding agent (Codex CLI) for the panel.

It lives in its own container with NO access to the panel's database, Redis, secrets, or the Docker socket — the
agent is told to run any command it likes (`--dangerously-bypass-approvals-and-sandbox`, because Codex's own sandbox
cannot run inside a container), so the container itself is the sandbox. Its whole surface:

  POST /turn   one user message -> a stream of the agent's JSONL events (application/x-ndjson)
  POST /stop   interrupt the turn of a session
  GET  /health

Every request carries `Authorization: Bearer $AGENT_TOKEN`. The model's API key arrives in the /turn body, is put in
the Codex process's environment, and is never written to disk or logged here.
"""

import asyncio
import json
import os
import re
import secrets
import signal
import shutil
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR", "/data/agent"))
TOKEN = os.environ.get("AGENT_TOKEN", "")
PING_SECONDS = float(os.environ.get("AGENT_PING_SECONDS", "15"))
EXIT_GRACE_SECONDS = float(os.environ.get("AGENT_EXIT_GRACE_SECONDS", "10"))  # how long Codex may take to exit after it reported the turn over
STOP_GRACE_SECONDS = float(os.environ.get("AGENT_STOP_GRACE_SECONDS", "10"))
MAX_STDERR_TAIL = 4000
KEY_ENV = "ORESHNIK_AGENT_API_KEY"
PORT = int(os.environ.get("AGENT_PORT", "8100"))  # where uvicorn listens: Codex reaches the compat proxy below on this port, on loopback

_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_THREAD_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}$")

app = FastAPI(title="Oreshnik agentd", docs_url=None, redoc_url=None, openapi_url=None)
_running: dict[str, asyncio.subprocess.Process] = {}


def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {TOKEN}"
    if not TOKEN or not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "bad token")


class Turn(BaseModel):
    session_key: str
    prompt: str = Field(min_length=1, max_length=100_000)
    thread_id: str | None = None
    model: str
    base_url: str = Field(max_length=500)
    api_key: str = Field(min_length=1, max_length=2000)
    timeout_seconds: int = Field(default=1800, ge=10, le=24 * 3600)
    # Offer the model plain function tools only. A gateway that is not OpenAI itself usually refuses Codex's other tool types with
    # 400 "This endpoint supports function tools only": the built-in web_search and the multi_agent namespace.
    function_tools_only: bool = False
    # Route Codex's requests through the compat proxy below, which completes the `usage` of the final stream event. Some gateways leave
    # `cached_tokens` out and Codex then treats the finished answer as a dropped stream: it asks again up to 5 times (the answer
    # appears several times, the tokens are paid each time) and fails.
    normalize_responses: bool = False


class Stop(BaseModel):
    session_key: str


def session_dir(key: str) -> Path:
    if not _SESSION_KEY_RE.match(key):
        raise HTTPException(422, "bad session_key")
    return DATA_DIR / key


def toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def build_argv(turn: Turn, base_url: str | None = None) -> list[str]:
    if not _MODEL_RE.match(turn.model):
        raise HTTPException(422, "bad model")
    if turn.thread_id is not None and not _THREAD_RE.match(turn.thread_id):
        raise HTTPException(422, "bad thread_id")
    provider = (
        "{ name = \"Oreshnik\", base_url = " + toml_string(base_url or turn.base_url)
        + f", env_key = \"{KEY_ENV}\", wire_api = \"responses\" }}"
    )
    argv = [CODEX_BIN, "exec"]
    if turn.thread_id:
        argv.append("resume")
    argv += [
        "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
        "-c", f"model={toml_string(turn.model)}",
        "-c", 'model_provider="oreshnik"',
        "-c", f"model_providers.oreshnik={provider}",
    ]
    if turn.function_tools_only:
        # Checked with the real Codex 0.169 against a request-logging endpoint: by default it sends function tools plus a `web_search`
        # tool and a `namespace` tool (multi_agent_v1); with these only `function` tools are left (exec_command, write_stdin, view_image…).
        # `goals` and `image_generation` are switched off too, to keep the tool list short for the simpler models behind such gateways.
        argv += ["-c", 'web_search="disabled"', "--disable", "multi_agent", "--disable", "goals", "--disable", "image_generation"]
    if turn.thread_id:
        argv.append(turn.thread_id)
    argv += ["--", turn.prompt]  # after "--": a prompt starting with "-" can never be read as an option
    return argv


def build_env(turn: Turn, home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update({"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "TERM": "dumb", "NO_COLOR": "1", KEY_ENV: turn.api_key})
    return env


def _is_final_event(line: str) -> bool:
    """Codex's own end-of-turn events: after one of them there is nothing left for the process to say."""
    try:
        return json.loads(line).get("type") in ("turn.completed", "turn.failed")
    except (ValueError, AttributeError):
        return False


async def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


async def _stream(key: str, turn: Turn, argv: list[str], env: dict[str, str], workdir: Path):
    compat_token: str | None = None
    if turn.normalize_responses:
        # Codex talks to the compat proxy of this very process (loopback), which forwards to the real endpoint.
        compat_token = secrets.token_urlsafe(16)
        _compat[compat_token] = turn.base_url.rstrip("/")
        argv = build_argv(turn, f"http://127.0.0.1:{PORT}/compat/{compat_token}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024,
        )
    except BaseException:
        _compat.pop(compat_token or "", None)
        raise
    _running[key] = proc
    stderr_tail = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr
        while chunk := await proc.stderr.read(4096):
            stderr_tail.extend(chunk)
            del stderr_tail[:-MAX_STDERR_TAIL]

    stderr_task = asyncio.create_task(drain_stderr())
    timed_out = False
    lingered = False       # Codex said the turn is over but did not exit: it was ended after a short grace
    final_at: float | None = None
    try:
        assert proc.stdout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + turn.timeout_seconds
        while True:
            read = asyncio.ensure_future(proc.stdout.readline())
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    wait = min(PING_SECONDS, remaining)
                    if final_at is not None:
                        left = final_at + EXIT_GRACE_SECONDS - loop.time()
                        if left <= 0:
                            lingered = True
                            break
                        wait = min(wait, left)
                    done, _ = await asyncio.wait({read}, timeout=wait)
                    if done:
                        break
                    # A long command prints nothing for minutes: tell the panel this turn is alive.
                    yield '{"type":"agentd.ping"}\n'
            finally:
                if not read.done():
                    read.cancel()
            if lingered:
                break
            try:
                raw = read.result()
            except ValueError:  # a single line over the limit: nothing sensible to do with the rest
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("{"):
                yield line + "\n"
                if final_at is None and _is_final_event(line):
                    final_at = loop.time()
    except TimeoutError:
        timed_out = True
    finally:
        # Reached on a normal end, a timeout, and when the panel hangs up mid-turn: never leave the agent running.
        if proc.returncode is None:
            await _kill_group(proc, signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), STOP_GRACE_SECONDS)
            except TimeoutError:
                await _kill_group(proc, signal.SIGKILL)
                await proc.wait()
        await stderr_task
        _running.pop(key, None)
        _compat.pop(compat_token or "", None)
    tail = stderr_tail.decode("utf-8", "replace").replace(turn.api_key, "[key]")
    # A process we had to end after it reported the turn over did its job: that is not a failure, whatever its signal exit code is.
    code = 0 if lingered else proc.returncode
    yield json.dumps({"type": "agentd.exit", "code": code, "timed_out": timed_out, "lingered": lingered, "stderr": tail}) + "\n"


# ── the compat proxy: a gateway's answers, made digestible for Codex ─────────────────────────────
#
# Reproduced with the real Codex 0.169 against an endpoint whose final `response.completed` event carries `input_tokens_details` without
# `cached_tokens`: Codex fails to parse it ("stream disconnected before completion: failed to parse ResponseCompleted: missing field
# `cached_tokens`"), repeats the whole request, and after its retries fails the turn. The proxy changes nothing but the `usage` numbers of
# the final events, filling in what is missing with zeros; everything else passes through byte for byte.

_compat: dict[str, str] = {}  # per-turn unguessable token -> the upstream base URL of that turn (never the key: the caller sends it)
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
    for details, field in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        value = usage.get(details)
        value = dict(value) if isinstance(value, dict) else {}
        value[field] = _count(value.get(field))
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


@app.post("/turn", dependencies=[Depends(require_token)])
async def turn(body: Turn) -> StreamingResponse:
    if body.session_key in _running:
        raise HTTPException(409, "a turn of this session is already running")
    argv = build_argv(body)
    home = session_dir(body.session_key)
    workdir = home / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)  # Codex refuses to start when CODEX_HOME does not exist
    return StreamingResponse(_stream(body.session_key, body, argv, build_env(body, home), workdir), media_type="application/x-ndjson")


@app.post("/stop", dependencies=[Depends(require_token)])
async def stop(body: Stop) -> dict:
    proc = _running.get(body.session_key)
    if proc is None or proc.returncode is not None:
        return {"stopped": False}
    await _kill_group(proc, signal.SIGINT)
    return {"stopped": True}


@app.delete("/sessions/{session_key}", dependencies=[Depends(require_token)])
async def delete_session(session_key: str) -> dict:
    if session_key in _running:
        raise HTTPException(409, "a turn of this session is running")
    shutil.rmtree(session_dir(session_key), ignore_errors=True)
    return {"deleted": True}


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
    return {"ok": True, "codex": ok, "codex_detail": detail}
