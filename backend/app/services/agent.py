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
    """Whether to offer the model plain function tools only from the start. The default is No: send the full Codex tool set (sub-agents,
    web search, …) to every profile, exactly like the stock Codex CLI — many gateways (DeepSeek and others) handle it. A gateway that
    does not answers "This endpoint supports function tools only", and `run_turn` then downgrades that one session for good and asks
    again. `AGENT_TOOLS` forces it: "functions" = always plain functions (a gateway known not to take the rest), "full" = always the
    full set with no auto-downgrade."""
    return settings.agent_tools == "functions"


RETRYABLE_ACTIONS = ("message", "review", "compact", "goal")
FUNCTIONS_ONLY_NOTE = ("Шлюз модели отклонил не-функциональные инструменты (под-агенты Codex). Сессия переключена на обычные функции — "
                       "повторяю ход. Веб-поиск остаётся как в настройках сессии.")


def refuses_tools(error: str | None) -> bool:
    """Whether the model's gateway refused the request for offering tools other than plain functions."""
    return bool(error) and "function tools only" in error.lower()


def event_refuses_tools(event: dict) -> bool:
    """Whether this stream event is the gateway's "function tools only" refusal. It surfaces as a feed error item (Codex retries it
    on its own and then ends the turn with a generic "stream disconnected", which carries no such text), as agentd's own error, or on
    the failed turn."""
    etype = event.get("type")
    if etype == "item.completed":
        item = event.get("item") or {}
        return item.get("type") == "error" and refuses_tools(str(item.get("message") or ""))
    if etype == "turn.completed":
        return refuses_tools(str(event.get("error") or ""))
    if etype == "agentd.error":
        return refuses_tools(str(event.get("message") or ""))
    return False


def _cancelled(db_factory: Callable[[], Session], session_id: int) -> bool:
    with db_factory() as db:
        session = db.get(AgentSession, session_id)
        return session is None or session.cancel_requested


def skills_payload(rows) -> tuple[list[dict], str]:
    """The user's skills as a turn hands them to agentd, or AgentError when together they are too big to send with every turn."""
    from app.services import skills as skill_svc

    out, digest = skill_svc.payload(rows)
    if sum(len(c) for s in out for c in s["files"].values()) * 3 // 4 > skill_svc.MAX_SENT_BYTES:
        raise AgentError(f"Включённые навыки вместе больше {skill_svc.MAX_SENT_BYTES // 1_000_000} МБ: выключите лишние в разделе «Навыки»", 422)
    return out, digest


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
        return {"ok": False, "detail": "AGENT_TOKEN не задан: «Код» выключен"}
    try:
        with agent_client(20.0) as client:  # the first check starts Codex once
            data = client.get("/health").json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "detail": f"Контейнер агента недоступен: {exc.__class__.__name__}"}
    if not data.get("codex"):
        why = str(data.get("codex_detail") or "").strip()
        return {"ok": False, "detail": f"Codex в контейнере агента не запускается: {why}" if why else "В контейнере агента нет Codex"}
    return {"ok": True, "detail": "на связи"}


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


def check_mcp(server: dict, timeout: float = 90.0) -> dict:
    """Start one MCP server in the agent container and list its tools: {ok, tools: [{name, description}], error}."""
    return _call("POST", "/mcp/check", json_body={"server": server}, timeout=timeout)


def forget_session(key: str) -> None:
    try:
        _call("DELETE", f"/sessions/{key}", timeout=30.0)
    except AgentError as exc:
        log.warning("agent cleanup failed: %s", exc)


def stream_turn(
    *, key: str, prompt: str, thread_id: str | None, base_url: str, model: str, api_key: str, function_tools_only: bool = False,
    normalize_responses: bool = False, action: str = "message", instructions: str | None = None, effort: str | None = None,
    approval_mode: str = "auto", review_target: dict | None = None, revert_before: str | None = None, applied_instructions: str | None = None,
    web_search: str = "auto", goals: bool = False, goal_budget: int | None = None, skills: list[dict] | None = None, skills_digest: str = "",
    mcp_servers: list[dict] | None = None, listener_token: str | None = None, panel_url: str | None = None,
) -> Iterator[dict]:
    """The agent's events for one action, as dicts. Ends with agentd's own `{"type": "agentd.exit", ...}` line."""
    body = {
        "session_key": key, "action": action, "prompt": prompt, "thread_id": thread_id, "model": model, "base_url": base_url,
        "api_key": api_key, "timeout_seconds": settings.agent_turn_timeout_seconds,
        "function_tools_only": function_tools_only, "normalize_responses": normalize_responses,
        "instructions": instructions, "applied_instructions": applied_instructions, "effort": effort, "approval_mode": approval_mode,
        "review_target": review_target, "revert_before": revert_before,
        "web_search": web_search, "goals": goals, "goal_budget": goal_budget,
        "skills": skills or [], "skills_digest": skills_digest, "mcp_servers": mcp_servers or [],
        "listener_token": listener_token, "panel_url": panel_url,
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


def listener_note() -> str:
    """What the agent is told about reaching it from outside: which ports the panel forwards into its container, or that none are.

    Without it the agent serves on any port it likes (8000, 3000, ...) and tells the user to open `localhost:<port>` — which is the
    user's own machine, not the container, so nothing opens.
    """
    from app.services.agent_listener import port_range

    rng = port_range()
    if rng is None:
        return (
            "[Панель: доступ к этому контейнеру снаружи]\n"
            "У контейнера нет опубликованных портов, и проброс портов выключен: то, что ты запускаешь здесь (веб-сервер, листенер), "
            "пользователь открыть в браузере НЕ может — `localhost` у него это его собственный компьютер. Не предлагай открыть "
            "http://localhost:<порт>. Результат показывай текстом или файлами: пользователь скачивает их кнопкой «Файлы»."
        )
    lo, hi = rng
    ports = str(lo) if lo == hi else f"{lo}–{hi}"
    head = (
        "[Панель: доступ к этому контейнеру снаружи]\n"
        f"У контейнера нет опубликованных портов. Панель умеет пробрасывать ОДИН TCP-порт из диапазона {ports} со своего хоста на такой же "
        "порт внутри этого контейнера. Поэтому всё, что должно быть доступно снаружи (веб-страница, приёмник обратного соединения, "
        f"вебхук), запускай ТОЛЬКО на порту из {ports} и на адресе 0.0.0.0 — любой другой порт (8000, 3000, 8080 ...) снаружи недоступен. "
        "Сервер/слушатель запускай так, чтобы он пережил завершение команды (например `nohup ... &` или `setsid`), и проверь, что он слушает. "
    )
    if settings.agent_listener_self:
        return head + (
            f"Проброс порта наружу ты открываешь САМ командой `oreshnik-listen <порт>` (порт из {ports}) — выполни её после того, как "
            "слушатель внутри поднят. Она вернёт подтверждение; порт закрывается командой `oreshnik-listen close <порт>` или сам по простою. "
            "Внешний адрес для пейлоада/пользователя — http://<адрес панели или ВДС>:<порт>/."
        )
    return head + (
        "Пользователю скажи два шага: 1) в настройках сессии открыть листенер на этом порту; 2) открыть "
        "http://<адрес панели>:<порт>/ (если панель на его же компьютере — http://localhost:<порт>/). "
        "Порт открывает панель по действию пользователя, сам ты его открыть не можешь."
    )


def effective_instructions(own: str | None, persona: str | None = None) -> str:
    """The instructions a session's conversation gets: its persona (who the agent is), the session's own, then the panel's note about
    ports. A change of any of them reaches a conversation that has begun inside its next message (see agentd's instructions_notice)."""
    return "\n\n".join(part for part in ((persona or "").strip(), (own or "").strip(), listener_note()) if part)


def run_turn(db_factory: Callable[[], Session], session_id: int, prompt: str, action: str = "message", extra: dict | None = None) -> None:
    """Run one action of a session to its end, storing what is worth keeping as it arrives. Always leaves the session idle."""
    extra = extra or {}
    with db_factory() as db:
        session = db.get(AgentSession, session_id)
        if session is None:
            return
        key, thread_id, chosen_model = session.key, session.thread_id, session.llm_model
        user_id = session.user_id
        from app.models.persona import Persona

        persona = db.get(Persona, session.persona_id) if session.persona_id else None
        instructions = effective_instructions(session.instructions, persona.instructions if persona else None)
        effort, approval_mode = session.reasoning_effort, session.approval_mode or "auto"
        applied_instructions = session.thread_instructions
        web_search = session.web_search or "auto"
        forced_functions = session.functions_only
        # The goal feature of Codex is on only for a session that uses it: otherwise the model could set itself a goal and work unasked.
        goals = session.goal is not None or action in ("goal", "goal_clear")
        profile = db.get(LLMProfile, session.llm_profile_id) if session.llm_profile_id else None
        from app.api.skills import for_agent

        from app.api import mcp as mcp_api

        skills_error: AgentError | None = None
        try:
            skills, skills_digest = skills_payload(for_agent(db, session.user_id, "code", persona.skills if persona else None))
            mcp_servers = mcp_api.for_agent(db, session.user_id, "code")
        except AgentError as exc:
            skills, skills_digest, mcp_servers, skills_error = [], "", [], exc

    error: str | None = None
    refused = False
    try:
        if profile is None:
            raise AgentError("Профиль LLM удалён: выберите другой")
        if skills_error is not None:
            raise skills_error
        base_url, model = endpoint_for(profile, chosen_model)
        api_key = decrypt(profile.api_key_enc)
        # When self-listeners are on and a port range is configured, hand the agent a short-lived, session-scoped token so it can
        # open its own listener (reverse-shell catcher, webhook) without a manual UI step. Still bounded to this session and the range.
        listener_token = panel_url = None
        from app.services.agent_listener import port_range as _lport_range

        if settings.agent_listener_self and _lport_range() is not None:
            from app.core.security import create_listener_token

            listener_token = create_listener_token(session_id, key, user_id, settings.agent_turn_timeout_seconds + 120)
            panel_url = settings.panel_internal_url
        for attempt in (1, 2):
            only_functions = forced_functions or function_tools_only(profile)
            error = None
            refused = False
            for event in stream_turn(
                key=key, prompt=prompt, thread_id=thread_id, base_url=base_url, model=model, api_key=api_key, action=action,
                function_tools_only=only_functions, normalize_responses=normalize_responses(profile),
                instructions=instructions, applied_instructions=applied_instructions, effort=effort, approval_mode=approval_mode,
                review_target=extra.get("review_target"), revert_before=extra.get("revert_before"),
                web_search=web_search, goals=goals, goal_budget=extra.get("goal_budget"),
                skills=skills, skills_digest=skills_digest, mcp_servers=mcp_servers,
                listener_token=listener_token, panel_url=panel_url,
            ):
                # The gateway's "function tools only" refusal arrives as a feed error item (Codex retries it internally and then fails
                # the turn with a different, generic message), so it has to be caught on the event, not only on the final turn error.
                refused = refused or event_refuses_tools(event)
                error = _handle_event(db_factory, session_id, redact(event, api_key), error, extra)
            refused = refused or refuses_tools(error)
            # A gateway that takes plain function tools only refuses the turn when Codex also offers it web search or its sub-agents
            # (the session's web search set to «Включён», «Какие инструменты видит модель» = full, or an OpenAI profile pointed at a
            # gateway). The session is switched to plain functions for good and the turn is asked again, once.
            # «full» means the admin vouches the gateway takes the full tool set — do not second-guess it with a downgrade.
            if attempt == 1 and not forced_functions and refused and settings.agent_tools != "full" and action in RETRYABLE_ACTIONS and not _cancelled(db_factory, session_id):
                forced_functions = True
                with db_factory() as db:
                    session = db.get(AgentSession, session_id)
                    session.functions_only = True
                    thread_id, applied_instructions = session.thread_id, session.thread_instructions  # the first try may have begun the thread
                    append_event(db, session_id, "turn.notice", {"message": FUNCTIONS_ONLY_NOTE})
                    db.commit()
                continue
            break
    except AgentError as exc:
        error = str(exc)
    except Exception as exc:  # a bug must not leave the session "running" forever
        log.exception("agent turn of session %s crashed", session_id)
        error = f"Внутренняя ошибка панели: {exc.__class__.__name__}"
    if error and refused:
        # Even after dropping the sub-agents the gateway refused: say what to do rather than leave the raw gateway error. (Web search
        # is kept — many gateways, e.g. DeepSeek via Bing, support it; it is the sub-agent/namespace tools they reject.)
        error = ("Шлюз модели отклонил запрос даже с обычными функциями. В «Настройках» панели поставьте «Какие инструменты видит "
                 "модель» → «auto», а если и так не проходит — «functions». Веб-поиск трогать не нужно.")

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
        elif etype == "mcp.status" and event.get("status") == "failed":
            append_event(db, session_id, "mcp.status", {k: event.get(k) for k in ("name", "status", "error")})
        elif etype == "agentd.error":
            error = error or str(event.get("message") or "Агент сообщил об ошибке")[:500]
        db.commit()
    return error
