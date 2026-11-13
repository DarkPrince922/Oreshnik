"""«Агент»: a chat with Codex CLI, run by the isolated `agent` container (agentd/main.py).

The panel keeps the sessions and the event history; the agent container only runs one turn at a time and streams the
agent's JSONL events back. The profile's API key is sent to the container with each turn and nowhere else.
"""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt
from app.models.agent import AgentEvent, AgentSession
from app.models.llm_profile import LLMProfile, Provider

log = logging.getLogger("oreshnik.agent")

# Codex talks the OpenAI *Responses* API. These providers have that API; Anthropic's native API is a different protocol.
RESPONSES_BASE_URLS = {Provider.openai: "https://api.openai.com/v1", Provider.grok: "https://api.x.ai/v1"}

# What one stored event may weigh: a command can print megabytes and the history must stay cheap to load and send.
MAX_STRING = 20_000
MAX_EVENTS_PER_SESSION = 20_000


class AgentError(Exception):
    """A human-readable reason a session or a turn cannot go on (safe to show to the operator)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def endpoint_for(profile: LLMProfile) -> tuple[str, str]:
    """(base_url, model) Codex should use for this profile, or `AgentError` when it cannot use it."""
    if profile.provider == Provider.claude:
        raise AgentError(
            "Агент (Codex) говорит на Responses API OpenAI. Профиль Claude его не поддерживает: "
            "возьмите профиль OpenAI/Grok или custom с OpenAI-совместимым Responses-шлюзом"
        )
    if profile.provider == Provider.custom:
        if not profile.base_url:
            raise AgentError("У custom-профиля нет base_url")
        return profile.base_url, profile.model
    return RESPONSES_BASE_URLS[profile.provider], profile.model


def trim(value, limit: int = MAX_STRING):
    """The event with every long string cut (a marker says so) — recursively, so a nested command output is covered too."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"\n[обрезано: ещё {len(value) - limit} симв.]"
    if isinstance(value, list):
        return [trim(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: trim(v, limit) for k, v in value.items()}
    return value


def redact(event: dict, secret: str) -> dict:
    """The event without the API key, should the agent ever print it (e.g. `env`): the history is stored and shown."""
    if len(secret) < 8:  # a too-short "key" would mangle ordinary text; real keys are far longer
        return event
    return json.loads(json.dumps(event).replace(json.dumps(secret)[1:-1], "[ключ скрыт]"))


def append_event(db: Session, session_id: int, kind: str, payload: dict) -> int | None:
    """Store one event with the next sequence number; None (nothing stored) once the session's history is full."""
    seq = db.scalar(
        update(AgentSession)
        .where(AgentSession.id == session_id, AgentSession.next_seq < MAX_EVENTS_PER_SESSION)
        .values(next_seq=AgentSession.next_seq + 1, heartbeat_at=_now())
        .returning(AgentSession.next_seq - 1)
    )
    if seq is None:
        return None
    db.add(AgentEvent(session_id=session_id, seq=seq, kind=kind[:40], payload=trim(payload)))
    db.flush()
    return seq


# ── talking to the agent container ───────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.agent_token}"}


def agent_client(timeout: float | httpx.Timeout = 10.0) -> httpx.Client:
    return httpx.Client(base_url=settings.agent_url, headers=_headers(), timeout=timeout)


def agent_health() -> dict:
    """What the agent container says about itself, or {"ok": False, "detail": why not}."""
    if not settings.agent_token:
        return {"ok": False, "detail": "AGENT_TOKEN не задан: агент выключен"}
    try:
        with agent_client(3.0) as client:
            data = client.get("/health").json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "detail": f"Контейнер агента недоступен: {exc.__class__.__name__}"}
    if not data.get("codex"):
        return {"ok": False, "detail": "В контейнере агента нет Codex"}
    return {"ok": True, "detail": "Агент на связи"}


def stop_turn(key: str) -> None:
    try:
        with agent_client(5.0) as client:
            client.post("/stop", json={"session_key": key})
    except httpx.HTTPError as exc:
        log.warning("agent stop failed: %s", exc)


def forget_session(key: str) -> None:
    try:
        with agent_client(10.0) as client:
            client.delete(f"/sessions/{key}")
    except httpx.HTTPError as exc:
        log.warning("agent cleanup failed: %s", exc)


def stream_turn(*, key: str, prompt: str, thread_id: str | None, base_url: str, model: str, api_key: str) -> Iterator[dict]:
    """The agent's events for one turn, as dicts. Ends with agentd's own `{"type": "agentd.exit", ...}` line."""
    body = {
        "session_key": key, "prompt": prompt, "thread_id": thread_id, "model": model, "base_url": base_url,
        "api_key": api_key, "timeout_seconds": settings.agent_turn_timeout_seconds,
    }
    timeout = httpx.Timeout(10.0, read=settings.agent_turn_timeout_seconds + 60.0)
    try:
        with agent_client(timeout) as client, client.stream("POST", "/turn", json=body) as response:
            if response.status_code != 200:
                response.read()
                raise AgentError(f"Контейнер агента ответил {response.status_code}: {response.text[:300]}")
            for line in response.iter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except httpx.HTTPError as exc:
        raise AgentError(f"Нет связи с контейнером агента ({exc.__class__.__name__})") from exc


def run_turn(db_factory: Callable[[], Session], session_id: int, prompt: str) -> None:
    """Run one turn of a session to its end, storing every event as it arrives. Always leaves the session idle."""
    from app.models.agent import AgentSessionStatus

    with db_factory() as db:
        session = db.get(AgentSession, session_id)
        if session is None:
            return
        key, thread_id = session.key, session.thread_id
        profile = db.get(LLMProfile, session.llm_profile_id) if session.llm_profile_id else None

    error: str | None = None
    try:
        if profile is None:
            raise AgentError("Профиль LLM удалён: выберите другой")
        base_url, model = endpoint_for(profile)
        api_key = decrypt(profile.api_key_enc)
        for event in stream_turn(key=key, prompt=prompt, thread_id=thread_id, base_url=base_url, model=model, api_key=api_key):
            etype = str(event.get("type", ""))
            if etype == "agentd.ping":
                with db_factory() as db:
                    db.execute(update(AgentSession).where(AgentSession.id == session_id).values(heartbeat_at=_now()))
                    db.commit()
                continue
            with db_factory() as db:
                if etype == "agentd.exit":
                    if event.get("timed_out"):
                        error = f"Ход прерван: прошло больше {settings.agent_turn_timeout_seconds} с"
                    elif event.get("code") not in (0, None) and error is None:
                        stderr = str(event.get("stderr", "")).strip()
                        error = stderr[-500:] or f"Агент завершился с кодом {event.get('code')}"
                    continue
                if etype == "thread.started" and isinstance(event.get("thread_id"), str):
                    db.get(AgentSession, session_id).thread_id = event["thread_id"]
                if etype in ("turn.failed", "error") and error is None:
                    message = event.get("message") or (event.get("error") or {}).get("message")
                    error = str(message or "Агент сообщил об ошибке")[:500]
                append_event(db, session_id, etype, redact(event, api_key))
                db.commit()
    except AgentError as exc:
        error = str(exc)
    except Exception as exc:  # a bug must not leave the session "running" forever
        log.exception("agent turn of session %s crashed", session_id)
        error = f"Внутренняя ошибка панели: {exc.__class__.__name__}"

    with db_factory() as db:
        session = db.get(AgentSession, session_id)
        if session.cancel_requested:
            append_event(db, session_id, "turn.stopped", {"message": "Остановлено оператором"})
            error = None
        elif error is not None:
            append_event(db, session_id, "turn.error", {"message": error})
        session.status = AgentSessionStatus.idle
        session.last_error = error
        session.cancel_requested = False
        session.turn_count += 1
        db.commit()
