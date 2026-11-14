"""«Агент»: a chat with Codex CLI, run by the isolated `agent` container (agentd/main.py).

The panel keeps the sessions and the event history; the agent container runs the agent and streams what happens back: events to keep
(a finished message, command, file change, a question to the user, the turn's diff and plan, usage) and snapshots of what is still being
written (`agent_live`, replaced as it grows and dropped when the item is finished). The profile's API key is sent to the container with each
turn and nowhere else.
"""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt
from app.models.agent import AgentEvent, AgentLive, AgentSession, AgentSessionStatus
from app.models.llm_profile import LLMProfile, Provider

log = logging.getLogger("oreshnik.agent")

# Codex talks the OpenAI *Responses* API. These providers have that API; Anthropic's native API is a different protocol.
RESPONSES_BASE_URLS = {Provider.openai: "https://api.openai.com/v1", Provider.grok: "https://api.x.ai/v1"}

# What one stored event may weigh: a command can print megabytes and the history must stay cheap to load and send.
MAX_STRING = 20_000
MAX_EVENTS_PER_SESSION = 20_000
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
APPROVAL_MODES = ("auto", "ask")
WEB_SEARCH_MODES = ("auto", "on", "off")
LIVE_KEYS_DIFF, LIVE_KEYS_PLAN = "__diff__", "__plan__"


class AgentError(Exception):
    """A human-readable reason a session or a turn cannot go on (safe to show to the operator)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def endpoint_for(profile: LLMProfile, model: str | None = None) -> tuple[str, str]:
    """(base_url, model) Codex should use for this profile (with `model`, one of its models, instead of its default), or
    `AgentError` when it cannot use it."""
    model = model or profile.model
    if profile.provider == Provider.claude:
        raise AgentError(
            "Агент (Codex) говорит на Responses API OpenAI. Профиль Claude его не поддерживает: "
            "возьмите профиль OpenAI/Grok или custom с OpenAI-совместимым Responses-шлюзом", 422,
        )
    if profile.provider == Provider.custom:
        if not profile.base_url:
            raise AgentError("У custom-профиля нет base_url", 422)
        return profile.base_url, model
    return RESPONSES_BASE_URLS[profile.provider], model


def function_tools_only(profile: LLMProfile) -> bool:
    """Whether the agent should offer its model plain function tools only. A gateway (a `custom` profile, or Grok's own API, which
    is not OpenAI's) often refuses Codex's other tool types; OpenAI's own API takes them all. `AGENT_TOOLS` overrides this."""
    if settings.agent_tools == "functions":
        return True
    if settings.agent_tools == "full":
        return False
    return profile.provider != Provider.openai


def normalize_responses(profile: LLMProfile) -> bool:
    """Whether the agent's requests go through agentd's compat proxy, which completes a gateway's incomplete `usage`
    (without it Codex fails to parse the final event and repeats the request). Not for OpenAI's own API."""
    return settings.agent_response_fix and profile.provider != Provider.openai


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


def live_put(db: Session, session_id: int, key: str, payload: dict) -> None:
    """Replace the snapshot of an unfinished item (one row per item; `rev` goes up so a reader sends only what changed)."""
    stmt = pg_insert(AgentLive).values(session_id=session_id, key=key[:120], payload=trim(payload), rev=1, updated_at=_now())
    db.execute(stmt.on_conflict_do_update(
        index_elements=[AgentLive.session_id, AgentLive.key],
        set_={"payload": stmt.excluded.payload, "rev": AgentLive.rev + 1, "updated_at": _now()},
    ))


def live_delete(db: Session, session_id: int, key: str | None = None) -> None:
    query = delete(AgentLive).where(AgentLive.session_id == session_id)
    if key is not None:
        query = query.where(AgentLive.key == key[:120])
    db.execute(query)


# ── talking to the agent container ───────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.agent_token}"}


def agent_client(timeout: float | httpx.Timeout = 10.0) -> httpx.Client:
    return httpx.Client(base_url=settings.agent_url, headers=_headers(), timeout=timeout)


def agent_async_client(timeout: float | httpx.Timeout = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.agent_url, headers=_headers(), timeout=timeout)


def agent_health() -> dict:
    """What the agent container says about itself, or {"ok": False, "detail": why not}."""
    if not settings.agent_token:
        return {"ok": False, "detail": "AGENT_TOKEN не задан: агент выключен"}
    try:
        with agent_client(20.0) as client:  # the first check starts Codex once
            data = client.get("/health").json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "detail": f"Контейнер агента недоступен: {exc.__class__.__name__}"}
    if not data.get("codex"):
        why = str(data.get("codex_detail") or "").strip()
        return {"ok": False, "detail": f"Codex в контейнере агента не запускается: {why}" if why else "В контейнере агента нет Codex"}
    return {"ok": True, "detail": "Агент на связи"}


def _call(method: str, path: str, *, json_body: dict | None = None, timeout: float = 20.0) -> dict:
    """One call to the agent container; `AgentError` (with the HTTP status it answered) when it cannot be done."""
    try:
        with agent_client(timeout) as client:
            response = client.request(method, path, json=json_body)
    except httpx.HTTPError as exc:
        raise AgentError(f"Нет связи с контейнером агента ({exc.__class__.__name__})") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise AgentError(str(detail or f"Контейнер агента ответил {response.status_code}")[:400], response.status_code)
    try:
        return response.json()
    except ValueError:
        return {}


def stop_turn(key: str) -> None:
    try:
        _call("POST", "/stop", json_body={"session_key": key}, timeout=15.0)
    except AgentError as exc:
        log.warning("agent stop failed: %s", exc)


def steer_turn(key: str, text: str) -> None:
    _call("POST", "/steer", json_body={"session_key": key, "text": text})


def answer_approval(key: str, request_id: str, decision: str, answers: dict | None = None) -> None:
    _call("POST", "/approve", json_body={"session_key": key, "request_id": request_id, "decision": decision, "answers": answers})


def fork_files(key: str, new_key: str) -> None:
    """A copy of the session's files and conversation in the container, under a new key."""
    _call("POST", f"/sessions/{key}/fork", json_body={"new_key": new_key}, timeout=300.0)


def forget_session(key: str) -> None:
    try:
        _call("DELETE", f"/sessions/{key}", timeout=30.0)
    except AgentError as exc:
        log.warning("agent cleanup failed: %s", exc)


def stream_turn(
    *, key: str, prompt: str, thread_id: str | None, base_url: str, model: str, api_key: str, function_tools_only: bool = False,
    normalize_responses: bool = False, action: str = "message", instructions: str | None = None, effort: str | None = None,
    approval_mode: str = "auto", review_target: dict | None = None, revert_before: str | None = None, applied_instructions: str | None = None,
    web_search: str = "auto", goals: bool = False, goal_budget: int | None = None,
) -> Iterator[dict]:
    """The agent's events for one action, as dicts. Ends with agentd's own `{"type": "agentd.exit", ...}` line."""
    body = {
        "session_key": key, "action": action, "prompt": prompt, "thread_id": thread_id, "model": model, "base_url": base_url,
        "api_key": api_key, "timeout_seconds": settings.agent_turn_timeout_seconds,
        "function_tools_only": function_tools_only, "normalize_responses": normalize_responses,
        "instructions": instructions, "applied_instructions": applied_instructions, "effort": effort, "approval_mode": approval_mode,
        "review_target": review_target, "revert_before": revert_before,
        "web_search": web_search, "goals": goals, "goal_budget": goal_budget,
    }
    timeout = httpx.Timeout(60.0, read=settings.agent_turn_timeout_seconds + 60.0)
    try:
        with agent_client(timeout) as client, client.stream("POST", "/turn", json=body) as response:
            if response.status_code != 200:
                response.read()
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = None
                raise AgentError(f"Контейнер агента ответил {response.status_code}: {str(detail or response.text)[:300]}", response.status_code)
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


def run_turn(db_factory: Callable[[], Session], session_id: int, prompt: str, action: str = "message", extra: dict | None = None) -> None:
    """Run one action of a session to its end, storing what is worth keeping as it arrives. Always leaves the session idle."""
    extra = extra or {}
    with db_factory() as db:
        session = db.get(AgentSession, session_id)
        if session is None:
            return
        key, thread_id, chosen_model = session.key, session.thread_id, session.llm_model
        instructions, effort, approval_mode = session.instructions, session.reasoning_effort, session.approval_mode or "auto"
        applied_instructions = session.thread_instructions
        web_search = session.web_search or "auto"
        # The goal feature of Codex is on only for a session that uses it: otherwise the model could set itself a goal and work unasked.
        goals = session.goal is not None or action in ("goal", "goal_clear")
        profile = db.get(LLMProfile, session.llm_profile_id) if session.llm_profile_id else None

    error: str | None = None
    try:
        if profile is None:
            raise AgentError("Профиль LLM удалён: выберите другой")
        base_url, model = endpoint_for(profile, chosen_model)
        api_key = decrypt(profile.api_key_enc)
        for event in stream_turn(
            key=key, prompt=prompt, thread_id=thread_id, base_url=base_url, model=model, api_key=api_key, action=action,
            function_tools_only=function_tools_only(profile), normalize_responses=normalize_responses(profile),
            instructions=instructions, applied_instructions=applied_instructions, effort=effort, approval_mode=approval_mode,
            review_target=extra.get("review_target"), revert_before=extra.get("revert_before"),
            web_search=web_search, goals=goals, goal_budget=extra.get("goal_budget"),
        ):
            error = _handle_event(db_factory, session_id, redact(event, api_key), error, extra)
    except AgentError as exc:
        error = str(exc)
    except Exception as exc:  # a bug must not leave the session "running" forever
        log.exception("agent turn of session %s crashed", session_id)
        error = f"Внутренняя ошибка панели: {exc.__class__.__name__}"

    with db_factory() as db:
        live_delete(db, session_id)
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


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _handle_event(db_factory: Callable[[], Session], session_id: int, event: dict, error: str | None, extra: dict) -> str | None:
    """Store one event of the agent's stream the right way; returns the error message so far (None while all is well)."""
    etype = str(event.get("type", ""))
    with db_factory() as db:
        if etype == "agentd.ping":
            db.execute(update(AgentSession).where(AgentSession.id == session_id).values(heartbeat_at=_now()))
        elif etype == "agentd.exit":
            if event.get("timed_out"):
                error = error or f"Ход прерван: прошло больше {settings.agent_turn_timeout_seconds} с"
            elif event.get("code") not in (0, None) and error is None:
                stderr = str(event.get("stderr", "")).strip()
                error = stderr[-500:] or f"Агент завершился с кодом {event.get('code')}"
        elif etype == "thread.instructions":
            db.get(AgentSession, session_id).thread_instructions = (event.get("instructions") or None)
        elif etype == "thread.started":
            if isinstance(event.get("thread_id"), str):
                db.get(AgentSession, session_id).thread_id = event["thread_id"]
        elif etype == "live" and isinstance(event.get("item"), dict) and event["item"].get("id"):
            live_put(db, session_id, str(event["item"]["id"]), event["item"])
        elif etype == "live.diff":
            live_put(db, session_id, LIVE_KEYS_DIFF, {"diff": event.get("diff", "")})
        elif etype == "live.plan":
            live_put(db, session_id, LIVE_KEYS_PLAN, {"steps": event.get("steps", []), "explanation": event.get("explanation")})
        elif etype == "item.completed" and isinstance(event.get("item"), dict):
            if event["item"].get("id"):
                live_delete(db, session_id, str(event["item"]["id"]))
            append_event(db, session_id, "item.completed", event)
        elif etype == "turn.diff":
            live_delete(db, session_id, LIVE_KEYS_DIFF)
            append_event(db, session_id, "turn.diff", {"diff": event.get("diff", "")})
        elif etype == "turn.plan":
            live_delete(db, session_id, LIVE_KEYS_PLAN)
            append_event(db, session_id, "turn.plan", {"steps": event.get("steps", []), "explanation": event.get("explanation")})
        elif etype == "turn.completed":
            if event.get("status") == "failed" and error is None:
                error = str(event.get("error") or "Агент сообщил об ошибке")[:500]
            append_event(db, session_id, "turn.completed", {k: event.get(k) for k in ("turn_id", "status", "error", "usage", "context")})
            session = db.get(AgentSession, session_id)
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            session.tokens_in = (session.tokens_in or 0) + _int(usage.get("input_tokens"))
            session.tokens_out = (session.tokens_out or 0) + _int(usage.get("output_tokens"))
            context = event.get("context")
            if isinstance(context, dict) and isinstance(context.get("used"), int):
                session.context_used = context["used"]
                session.context_window = context["window"] if isinstance(context.get("window"), int) else None
                session.context_window_known = context.get("window_known") is not False
        elif etype == "turn.started":
            append_event(db, session_id, "turn.started", {"turn_id": event.get("turn_id")})
        elif etype in ("approval.requested", "approval.resolved"):
            append_event(db, session_id, etype, {k: v for k, v in event.items() if k != "type"})
        elif etype == "goal.updated" and isinstance(event.get("goal"), dict):
            session = db.get(AgentSession, session_id)
            before = session.goal or {}
            session.goal = event["goal"]
            # Codex reports the goal after every turn (its counters move): the feed gets a line only when something the user cares about changed.
            if (before.get("status"), before.get("objective"), before.get("token_budget")) != tuple(event["goal"].get(k) for k in ("status", "objective", "token_budget")):
                append_event(db, session_id, "goal.updated", {"goal": event["goal"]})
        elif etype == "goal.cleared":
            session = db.get(AgentSession, session_id)
            if session.goal is not None:
                session.goal = None
                append_event(db, session_id, "goal.cleared", {})
        elif etype == "thread.reverted":
            append_event(db, session_id, "thread.reverted", {"turn_id": extra.get("revert_before")})
        elif etype == "agentd.error":
            error = error or str(event.get("message") or "Агент сообщил об ошибке")[:500]
        db.commit()
    return error
