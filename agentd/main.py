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

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR", "/data/agent"))
TOKEN = os.environ.get("AGENT_TOKEN", "")
PING_SECONDS = float(os.environ.get("AGENT_PING_SECONDS", "15"))
STOP_GRACE_SECONDS = float(os.environ.get("AGENT_STOP_GRACE_SECONDS", "10"))
MAX_STDERR_TAIL = 4000
KEY_ENV = "ORESHNIK_AGENT_API_KEY"

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


class Stop(BaseModel):
    session_key: str


def session_dir(key: str) -> Path:
    if not _SESSION_KEY_RE.match(key):
        raise HTTPException(422, "bad session_key")
    return DATA_DIR / key


def toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def build_argv(turn: Turn) -> list[str]:
    if not _MODEL_RE.match(turn.model):
        raise HTTPException(422, "bad model")
    if turn.thread_id is not None and not _THREAD_RE.match(turn.thread_id):
        raise HTTPException(422, "bad thread_id")
    provider = (
        "{ name = \"Oreshnik\", base_url = " + toml_string(turn.base_url)
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
    if turn.thread_id:
        argv.append(turn.thread_id)
    argv += ["--", turn.prompt]  # after "--": a prompt starting with "-" can never be read as an option
    return argv


def build_env(turn: Turn, home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update({"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "TERM": "dumb", "NO_COLOR": "1", KEY_ENV: turn.api_key})
    return env


async def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


async def _stream(key: str, turn: Turn, argv: list[str], env: dict[str, str], workdir: Path):
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024,
    )
    _running[key] = proc
    stderr_tail = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr
        while chunk := await proc.stderr.read(4096):
            stderr_tail.extend(chunk)
            del stderr_tail[:-MAX_STDERR_TAIL]

    stderr_task = asyncio.create_task(drain_stderr())
    timed_out = False
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
                    done, _ = await asyncio.wait({read}, timeout=min(PING_SECONDS, remaining))
                    if done:
                        break
                    # A long command prints nothing for minutes: tell the panel this turn is alive.
                    yield '{"type":"agentd.ping"}\n'
            finally:
                if not read.done():
                    read.cancel()
            try:
                raw = read.result()
            except ValueError:  # a single line over the limit: nothing sensible to do with the rest
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("{"):
                yield line + "\n"
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
    tail = stderr_tail.decode("utf-8", "replace").replace(turn.api_key, "[key]")
    yield json.dumps({"type": "agentd.exit", "code": proc.returncode, "timed_out": timed_out, "stderr": tail}) + "\n"


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


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "codex": shutil.which(CODEX_BIN) is not None}
