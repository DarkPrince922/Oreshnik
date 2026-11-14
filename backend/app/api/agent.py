"""«Агент»: chat with a coding agent (Codex CLI) that runs in the isolated agent container.

Sessions are private to their owner (an admin does not see other people's either): an agent runs commands with the owner's own LLM key and
its history may contain anything the owner pasted into it. Operators and admins use it; viewers don't.
"""

import asyncio
import json
import logging

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select, true, update
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import require_operator
from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.security import decode_access_token
from app.models.agent import AgentEvent, AgentLive, AgentSession, AgentSessionStatus, new_session_key
from app.models.llm_profile import LLMProfile
from app.models.scan import Finding, Scan
from app.models.user import Role, User
from app.schemas.agent import (
    AgentAction, AgentApproval, AgentClone, AgentEventOut, AgentFromScan, AgentFromScanOut, AgentListenerOpen, AgentListenerOut,
    AgentListenerRange, AgentMessage, AgentSessionCreate, AgentSessionOut, AgentSessionUpdate,
)
from app.services import agent as agent_service
from app.services import agent_listener
from app.services.agent import AgentError
from app.services.llm import validate_model
from app.services.scan_files import find_run_dir, scan_dir
from app.worker.tasks import agent_turn as agent_turn_task

log = logging.getLogger("oreshnik.agent_api")

router = APIRouter(prefix="/api/agent", tags=["agent"])

NEW_TITLE = "Новая сессия"
PROXIED_STATUSES = {400, 404, 409, 413, 422, 502, 503, 504}


def _out(db: Session, session: AgentSession) -> AgentSessionOut:
    profile = db.get(LLMProfile, session.llm_profile_id) if session.llm_profile_id else None
    listener = agent_listener.status_for(session.key)
    return AgentSessionOut(
        id=session.id, title=session.title, llm_profile_id=session.llm_profile_id,
        llm_profile_name=profile.name if profile else None,
        llm_model=session.llm_model or (profile.model if profile else None),
        instructions=session.instructions, reasoning_effort=session.reasoning_effort, approval_mode=session.approval_mode or "auto",
        web_search=session.web_search or "auto", goal=session.goal, context_used=session.context_used, context_window=session.context_window,
        context_window_known=session.context_window_known is not False, tokens_in=session.tokens_in or 0, tokens_out=session.tokens_out or 0,
        status=session.status, turn_count=session.turn_count,
        last_error=session.last_error, cancel_requested=session.cancel_requested, event_count=session.next_seq,
        listener_port=listener.port if listener else None,
        created_at=session.created_at, updated_at=session.updated_at,
    )


def _own_or_404(db: Session, user: User, session_id: int) -> AgentSession:
    session = db.scalar(select(AgentSession).where(AgentSession.id == session_id, AgentSession.user_id == user.id))
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    return session


def _usable_profile(db: Session, user: User, profile_id: int, model: str | None = None) -> LLMProfile:
    profile = db.scalar(select(LLMProfile).where(LLMProfile.id == profile_id, LLMProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Профиль не найден")
    try:
        validate_model(profile, model)
        agent_service.endpoint_for(profile)
    except (AgentError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return profile


def _require_enabled() -> None:
    if not settings.agent_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Агент выключен: на сервере не задан AGENT_TOKEN")


def _agent_http_error(exc: AgentError) -> HTTPException:
    return HTTPException(exc.status if exc.status in PROXIED_STATUSES else status.HTTP_502_BAD_GATEWAY, str(exc))


def _claim(db: Session, session: AgentSession) -> None:
    """Take the one place for a turn: a single conditional UPDATE, so two clicks cannot both win."""
    claimed = db.scalar(
        update(AgentSession)
        .where(AgentSession.id == session.id, AgentSession.status == AgentSessionStatus.idle)
        .values(status=AgentSessionStatus.running, cancel_requested=False, last_error=None, heartbeat_at=agent_service._now())
        .returning(AgentSession.id)
    )
    if claimed is None:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Агент ещё отвечает на прошлое сообщение")


def _check_profile_alive(db: Session, user: User, session: AgentSession) -> None:
    if session.llm_profile_id is None:
        raise HTTPException(422, "Профиль LLM удалён: создайте новую сессию")
    _usable_profile(db, user, session.llm_profile_id)


@router.get("/health")
def health(_: User = Depends(require_operator)) -> dict:
    return agent_service.agent_health()


@router.get("/sessions", response_model=list[AgentSessionOut])
def list_sessions(user: User = Depends(require_operator), db: Session = Depends(get_db)) -> list[AgentSessionOut]:
    rows = db.scalars(select(AgentSession).where(AgentSession.user_id == user.id).order_by(AgentSession.updated_at.desc()))
    return [_out(db, s) for s in rows]


def _new_session(db: Session, user: User, profile: LLMProfile, body: AgentSessionCreate, title: str | None = None) -> AgentSession:
    count = db.scalar(select(func.count()).select_from(AgentSession).where(AgentSession.user_id == user.id))
    if count >= settings.max_agent_sessions_per_user:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Не больше {settings.max_agent_sessions_per_user} сессий: удалите ненужные")
    session = AgentSession(
        user_id=user.id, title=title or body.title.strip() or NEW_TITLE, llm_profile_id=profile.id, llm_model=validate_model(profile, body.llm_model),
        instructions=(body.instructions or "").strip() or None, reasoning_effort=body.reasoning_effort, approval_mode=body.approval_mode,
        web_search=body.web_search,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.post("/sessions", response_model=AgentSessionOut, status_code=status.HTTP_201_CREATED)
def create_session(body: AgentSessionCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    _require_enabled()
    profile = _usable_profile(db, user, body.llm_profile_id, body.llm_model)
    return _out(db, _new_session(db, user, profile, body))


@router.get("/sessions/{session_id}", response_model=AgentSessionOut)
def get_session(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    return _out(db, _own_or_404(db, user, session_id))


@router.patch("/sessions/{session_id}", response_model=AgentSessionOut)
def update_session(session_id: int, body: AgentSessionUpdate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    """Settings of the session; they apply from the next message (a running turn goes on as it was started)."""
    session = _own_or_404(db, user, session_id)
    sent = body.model_fields_set
    if body.title is not None:
        session.title = body.title.strip() or session.title
    if "instructions" in sent:
        session.instructions = (body.instructions or "").strip() or None
    if "reasoning_effort" in sent:
        session.reasoning_effort = body.reasoning_effort
    if body.approval_mode is not None:
        session.approval_mode = body.approval_mode
    if body.web_search is not None:
        session.web_search = body.web_search
    db.commit()
    db.refresh(session)
    return _out(db, session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    session = _own_or_404(db, user, session_id)
    if session.status == AgentSessionStatus.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала остановите ход")
    key = session.key
    db.delete(session)
    db.commit()
    agent_service.forget_session(key)  # best effort: its working directory in the agent container
    await agent_listener.close_for_session(key)


@router.get("/sessions/{session_id}/events", response_model=list[AgentEventOut])
def list_events(
    session_id: int,
    after: int = Query(default=-1, ge=-1),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> list[AgentEvent]:
    _own_or_404(db, user, session_id)
    return list(db.scalars(
        select(AgentEvent).where(AgentEvent.session_id == session_id, AgentEvent.seq > after).order_by(AgentEvent.seq).limit(5000)
    ))


@router.post("/sessions/{session_id}/messages", response_model=AgentSessionOut, status_code=status.HTTP_202_ACCEPTED)
def send_message(session_id: int, body: AgentMessage, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    session = _own_or_404(db, user, session_id)
    _require_enabled()
    _check_profile_alive(db, user, session)
    _claim(db, session)
    if agent_service.append_event(db, session.id, "user", {"text": body.text}) is None:
        session.status = AgentSessionStatus.idle
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "История сессии заполнена: начните новую")
    if session.turn_count == 0 and session.title == NEW_TITLE:
        session.title = body.text.strip().splitlines()[0][:80]
    db.commit()
    db.refresh(session)
    agent_turn_task.delay(session.id, body.text)
    return _out(db, session)


@router.post("/sessions/{session_id}/steer", response_model=AgentSessionOut)
def steer(session_id: int, body: AgentMessage, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    """Add an instruction to the turn that is running now: the agent takes it into account without starting over."""
    session = _own_or_404(db, user, session_id)
    if session.status != AgentSessionStatus.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сейчас нет хода, который можно дополнить: отправьте обычное сообщение")
    try:
        agent_service.steer_turn(session.key, body.text)
    except AgentError as exc:
        raise _agent_http_error(exc)
    agent_service.append_event(db, session.id, "user", {"text": body.text, "steer": True})
    db.commit()
    db.refresh(session)
    return _out(db, session)


@router.post("/sessions/{session_id}/approvals/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def answer_approval(session_id: int, request_id: str, body: AgentApproval, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    """The user's answer to something the agent asked: approve a command or a file change, decline it, or answer its question."""
    session = _own_or_404(db, user, session_id)
    try:
        agent_service.answer_approval(session.key, request_id[:64], body.decision, body.answers)
    except AgentError as exc:
        raise _agent_http_error(exc)


def _last_turn_id(db: Session, session_id: int) -> str | None:
    """The newest turn of the conversation that has not been undone."""
    rows = db.execute(
        select(AgentEvent.kind, AgentEvent.payload)
        .where(AgentEvent.session_id == session_id, AgentEvent.kind.in_(("turn.started", "thread.reverted")))
        .order_by(AgentEvent.seq)
    ).all()
    turns: list[str] = []
    for kind, payload in rows:
        turn_id = (payload or {}).get("turn_id")
        if kind == "turn.started" and turn_id:
            turns.append(turn_id)
        elif kind == "thread.reverted" and turn_id in turns:
            turns = turns[: turns.index(turn_id)]  # the undone turn and everything after it
    return turns[-1] if turns else None


@router.post("/sessions/{session_id}/actions", response_model=AgentSessionOut, status_code=status.HTTP_202_ACCEPTED)
def run_action(session_id: int, body: AgentAction, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    """Compress the conversation, review the changes, run a command yourself in the session, or undo the last turn (the conversation, not the files)."""
    session = _own_or_404(db, user, session_id)
    _require_enabled()
    _check_profile_alive(db, user, session)
    extra: dict = {}
    note: dict = {"action": body.action}
    prompt = ""
    if body.action == "shell":
        if not body.command:
            raise HTTPException(422, "Нужна команда")
        prompt = note["command"] = body.command
    elif body.action == "review":
        extra["review_target"] = body.target or {"type": "uncommittedChanges"}
        note["target"] = extra["review_target"]
    elif body.action == "goal":
        # The agent then works by itself, turn after turn, until the goal is reached or its token budget is spent — so a budget is not optional.
        if body.objective:
            if body.token_budget is None:
                raise HTTPException(422, "Для цели нужен бюджет токенов")
            if body.token_budget > settings.agent_goal_max_tokens:
                raise HTTPException(422, f"Бюджет цели не больше {settings.agent_goal_max_tokens} токенов (см. «Настройки»)")
            prompt = note["objective"] = body.objective
            extra["goal_budget"] = note["token_budget"] = body.token_budget
        else:
            if not session.goal:
                raise HTTPException(422, "Напишите цель")
            if session.goal.get("status") == "complete":
                raise HTTPException(status.HTTP_409_CONFLICT, "Цель уже достигнута: поставьте новую")
            if body.token_budget is not None:
                if body.token_budget > settings.agent_goal_max_tokens:
                    raise HTTPException(422, f"Бюджет цели не больше {settings.agent_goal_max_tokens} токенов (см. «Настройки»)")
                extra["goal_budget"] = note["token_budget"] = body.token_budget
            note["resume"] = True
    elif body.action == "goal_clear":
        if not session.goal:
            raise HTTPException(status.HTTP_409_CONFLICT, "У сессии нет цели")
    elif body.action == "revert":
        turn_id = _last_turn_id(db, session.id)
        if turn_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Нечего откатывать")
        extra["revert_before"] = turn_id
    _claim(db, session)
    if agent_service.append_event(db, session.id, "action", note) is None:
        session.status = AgentSessionStatus.idle
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "История сессии заполнена: начните новую")
    db.commit()
    db.refresh(session)
    agent_turn_task.delay(session.id, prompt, body.action, extra)
    return _out(db, session)


@router.post("/sessions/{session_id}/fork", response_model=AgentSessionOut, status_code=status.HTTP_201_CREATED)
def fork_session(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    """A copy of the session — its conversation and its files — to take in another direction while this one stays as it is."""
    source = _own_or_404(db, user, session_id)
    _require_enabled()
    if source.status == AgentSessionStatus.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Дождитесь конца хода")
    count = db.scalar(select(func.count()).select_from(AgentSession).where(AgentSession.user_id == user.id))
    if count >= settings.max_agent_sessions_per_user:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Не больше {settings.max_agent_sessions_per_user} сессий: удалите ненужные")
    copy = AgentSession(
        key=new_session_key(), user_id=user.id, title=f"{source.title} (копия)"[:200], llm_profile_id=source.llm_profile_id, llm_model=source.llm_model,
        thread_id=source.thread_id, instructions=source.instructions, thread_instructions=source.thread_instructions, reasoning_effort=source.reasoning_effort, approval_mode=source.approval_mode,
        turn_count=source.turn_count, next_seq=source.next_seq, web_search=source.web_search, goal=source.goal,
        context_used=source.context_used, context_window=source.context_window, context_window_known=source.context_window_known,
    )
    try:
        agent_service.fork_files(source.key, copy.key)
    except AgentError as exc:
        raise _agent_http_error(exc)
    db.add(copy)
    db.flush()
    for event in db.scalars(select(AgentEvent).where(AgentEvent.session_id == source.id).order_by(AgentEvent.seq)):
        db.add(AgentEvent(session_id=copy.id, seq=event.seq, kind=event.kind, payload=event.payload, created_at=event.created_at))
    db.commit()
    db.refresh(copy)
    return _out(db, copy)


@router.post("/sessions/{session_id}/stop", response_model=AgentSessionOut)
def stop(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    session = _own_or_404(db, user, session_id)
    if session.status == AgentSessionStatus.running:
        session.cancel_requested = True
        db.commit()
        agent_service.stop_turn(session.key)
        db.refresh(session)
    return _out(db, session)


# ── listener: a port the panel proxies, on request, to the same port inside the agent container ──────────────────────
#
# See docs/ARCHITECTURE.md §27. Not tied to a turn: the operator (or the agent itself, via a shell command inside the
# container) opens it, works with it, then closes it — independent of whether a message is in flight.

@router.get("/listener-range", response_model=AgentListenerRange)
def listener_range(_: User = Depends(require_operator)) -> AgentListenerRange:
    rng = agent_listener.port_range()
    return AgentListenerRange(available=rng is not None, min=rng[0] if rng else None, max=rng[1] if rng else None)


def _listener_out(listener: agent_listener.Listener | None) -> AgentListenerOut:
    if listener is None:
        return AgentListenerOut(port=None)
    return AgentListenerOut(port=listener.port, opened_at=listener.opened_at, last_active=listener.last_active)


@router.get("/sessions/{session_id}/listener", response_model=AgentListenerOut)
def get_listener(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentListenerOut:
    session = _own_or_404(db, user, session_id)
    return _listener_out(agent_listener.status_for(session.key))


@router.post("/sessions/{session_id}/listener", response_model=AgentListenerOut)
async def open_listener(session_id: int, body: AgentListenerOpen, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentListenerOut:
    """Start proxying `body.port` on the panel's host to the same port inside this session's agent container. The agent
    must bind that port itself (e.g. with a shell command) for anything to come through; this call only opens the pipe."""
    session = _own_or_404(db, user, session_id)
    _require_enabled()
    try:
        listener = await agent_listener.open_listener(port=body.port, session_key=session.key, session_id=session.id, user_id=user.id)
    except AgentError as exc:
        raise _agent_http_error(exc)
    return _listener_out(listener)


@router.delete("/sessions/{session_id}/listener", status_code=status.HTTP_204_NO_CONTENT)
async def close_listener(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    session = _own_or_404(db, user, session_id)
    await agent_listener.close_for_session(session.key)


# ── the session's files ───────────────────────────────────────────────────────────────────────
#
# The files live in the agent container; the panel only checks the session is the caller's and passes the request on.

def _session_key(user_id: int, session_id: int) -> str:
    with SessionLocal() as db:
        key = db.scalar(select(AgentSession.key).where(AgentSession.id == session_id, AgentSession.user_id == user_id))
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    return key


async def _files_json(user: User, session_id: int, method: str, suffix: str, **kwargs) -> Response:
    _require_enabled()
    key = await run_in_threadpool(_session_key, user.id, session_id)
    try:
        async with agent_service.agent_async_client(60.0) as client:
            reply = await client.request(method, f"/sessions/{key}/{suffix}", **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Нет связи с контейнером агента ({exc.__class__.__name__})")
    if reply.status_code >= 400:
        try:
            detail = reply.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(reply.status_code if reply.status_code in PROXIED_STATUSES else 502, str(detail or "Ошибка контейнера агента"))
    return Response(reply.content, media_type="application/json")


@router.get("/sessions/{session_id}/files")
async def files_list(session_id: int, path: str = "", user: User = Depends(require_operator)) -> Response:
    return await _files_json(user, session_id, "GET", "fs", params={"path": path})


@router.get("/sessions/{session_id}/files/content")
async def files_content(session_id: int, path: str, user: User = Depends(require_operator)) -> Response:
    return await _files_json(user, session_id, "GET", "file", params={"path": path})


@router.post("/sessions/{session_id}/files/clone")
async def files_clone(session_id: int, body: AgentClone, user: User = Depends(require_operator)) -> Response:
    """`git clone --depth 1` of an https repository into the session's folder (the agent then works on it)."""
    return await _files_json(user, session_id, "POST", "clone", json={"url": body.url})


@router.put("/sessions/{session_id}/files")
async def files_upload(session_id: int, path: str, request: Request, user: User = Depends(require_operator)) -> Response:
    """Upload a file (the request body is the file) to `path` in the session's folder."""
    _require_enabled()
    key = await run_in_threadpool(_session_key, user.id, session_id)
    try:
        async with agent_service.agent_async_client(httpx.Timeout(30.0, read=300.0, write=300.0)) as client:
            reply = await client.put(f"/sessions/{key}/file", params={"path": path}, content=request.stream())
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Нет связи с контейнером агента ({exc.__class__.__name__})")
    if reply.status_code >= 400:
        try:
            detail = reply.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(reply.status_code if reply.status_code in PROXIED_STATUSES else 502, str(detail or "Не удалось загрузить файл"))
    return Response(reply.content, media_type="application/json")


async def _download(user: User, session_id: int, suffix: str, params: dict) -> StreamingResponse:
    _require_enabled()
    key = await run_in_threadpool(_session_key, user.id, session_id)
    client = agent_service.agent_async_client(httpx.Timeout(30.0, read=600.0))
    try:
        reply = await client.send(client.build_request("GET", f"/sessions/{key}/{suffix}", params=params), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Нет связи с контейнером агента ({exc.__class__.__name__})")
    if reply.status_code >= 400:
        body = await reply.aread()
        await reply.aclose()
        await client.aclose()
        try:
            detail = json.loads(body).get("detail")
        except ValueError:
            detail = None
        raise HTTPException(reply.status_code if reply.status_code in PROXIED_STATUSES else 502, str(detail or "Ошибка контейнера агента"))

    async def cleanup() -> None:
        await reply.aclose()
        await client.aclose()

    headers = {k: v for k, v in reply.headers.items() if k.lower() in ("content-disposition", "content-type", "content-length")}
    headers["X-Content-Type-Options"] = "nosniff"
    return StreamingResponse(reply.aiter_raw(), headers=headers, background=BackgroundTask(cleanup))


@router.get("/sessions/{session_id}/files/download")
async def files_download(session_id: int, path: str, user: User = Depends(require_operator)) -> StreamingResponse:
    return await _download(user, session_id, "file", {"path": path, "download": "true"})


@router.get("/sessions/{session_id}/files/archive")
async def files_archive(session_id: int, user: User = Depends(require_operator)) -> StreamingResponse:
    return await _download(user, session_id, "archive", {})


# ── «Разобрать в агенте»: a scan's results as the starting point of a session ──────────────────────

MAX_SCAN_FILES, MAX_SCAN_FILE_BYTES, MAX_SCAN_TOTAL_BYTES = 200, 5_000_000, 30_000_000
SCAN_ARTIFACTS = ("penetration_test_report.md", "vulnerabilities.json", "findings.sarif")


def _scan_files(db: Session, scan: Scan) -> list[tuple[str, bytes]]:
    """What the agent gets to read about a scan, as (path in the session's folder, content): the findings the panel has stored,
    what Strix wrote to disk (report, JSON, SARIF, one file per vulnerability), and a short README."""
    files: list[tuple[str, bytes]] = []
    findings = list(db.scalars(select(Finding).where(Finding.scan_id == scan.id).order_by(Finding.id)))
    summary = [
        f"# Скан #{scan.id}", "", f"- Цели: {', '.join(scan.targets)}", f"- Режим: {scan.scan_mode.value}", f"- Статус: {scan.status.value}",
        f"- Найдено: {len(findings)}", "", "## Файлы", "",
        "- `findings.json` — находки, как их сохранила панель", "- `report.md`, `vulnerabilities.json`, `findings.sarif`, `vulnerabilities/` — то, что записал Strix (если есть)", "",
    ]
    if scan.instruction:
        summary += ["## Инструкция скана", "", scan.instruction, ""]
    files.append(("scan/README.md", "\n".join(summary).encode()))
    files.append(("scan/findings.json", json.dumps(
        [{"id": f.vuln_id, "title": f.title, "severity": f.severity, "target": f.target, "endpoint": f.endpoint, "method": f.method, "cve": f.cve, "cwe": f.cwe,
          "cvss": f.cvss, "details": f.data} for f in findings], ensure_ascii=False, indent=2).encode()))
    run_dir = find_run_dir(scan_dir(scan.id))
    if run_dir is not None:
        for name in SCAN_ARTIFACTS:
            path = run_dir / name
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_SCAN_FILE_BYTES:
                files.append((f"scan/{'report.md' if name.endswith('report.md') else name}", path.read_bytes()))
        folder = run_dir / "vulnerabilities"
        if folder.is_dir():
            for path in sorted(folder.glob("*.md"))[: MAX_SCAN_FILES - len(files)]:
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_SCAN_FILE_BYTES:
                    files.append((f"scan/vulnerabilities/{path.name}", path.read_bytes()))
    total = 0
    kept = []
    for relative, data in files:
        total += len(data)
        if total > MAX_SCAN_TOTAL_BYTES:
            break
        kept.append((relative, data))
    return kept


def _scan_draft(scan: Scan, findings: int) -> str:
    return (
        f"Разбери результаты скана #{scan.id} ({', '.join(scan.targets)}): файлы лежат в папке scan/ (начни с scan/README.md).\n\n"
        f"Находок: {findings}. Для каждой: проверь, подтверждается ли она по описанию, оцени реальную опасность и приоритет, предложи конкретное "
        "исправление. В конце — краткая сводка: что исправлять в первую очередь. Если исходный код цели есть в рабочей папке, подготовь правки."
    )


@router.post("/from-scan/{scan_id}", response_model=AgentFromScanOut, status_code=status.HTTP_201_CREATED)
async def from_scan(scan_id: int, body: AgentFromScan, user: User = Depends(require_operator)) -> AgentFromScanOut:
    """A new session with the scan's findings and report already in its folder, and a proposed first message."""
    _require_enabled()

    def create() -> tuple[AgentSessionOut, str, list[tuple[str, bytes]], str]:
        with SessionLocal() as db:
            scan = db.scalar(select(Scan).where(Scan.id == scan_id, Scan.user_id == user.id if user.role == Role.operator else true()))
            if scan is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Скан не найден")
            profile = _usable_profile(db, user, body.llm_profile_id, body.llm_model)
            files = _scan_files(db, scan)
            count = db.scalar(select(func.count()).select_from(Finding).where(Finding.scan_id == scan.id))
            target = scan.targets[0] if scan.targets else ""
            session = _new_session(
                db, user, profile, AgentSessionCreate(llm_profile_id=profile.id, llm_model=body.llm_model, title=f"Скан #{scan.id}: {target}"[:200]),
            )
            return _out(db, session), session.key, files, _scan_draft(scan, count)

    out, key, files, draft = await run_in_threadpool(create)
    try:
        async with agent_service.agent_async_client(httpx.Timeout(30.0, read=120.0, write=120.0)) as client:
            for relative, data in files:
                reply = await client.put(f"/sessions/{key}/file", params={"path": relative}, content=data)
                if reply.status_code >= 400:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Не удалось передать файл {relative} агенту")
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Нет связи с контейнером агента ({exc.__class__.__name__})")
    return AgentFromScanOut(session=out, draft=draft, files=len(files))


# ── live events over WebSocket ───────────────────────────────────────────
#
# Same shape as the scan log socket. The client's FIRST frame authenticates and says where to resume:
#   {"token": "<jwt>", "from": <next seq it has not seen>}
# Server frames:
#   {"type":"status","status":...}
#   {"type":"event","seq":N,"kind":...,"payload":{...}}         what is kept (history)
#   {"type":"live","key":...,"payload":{...}}                    an unfinished item as written so far (replaces the previous one of that key)
#   {"type":"live_end","key":...}                                that item is finished (its event has come, or it is gone)
# Closing codes: 4401 not authenticated, 4404 no such session. The socket stays open between turns.

_WS_POLL_SECONDS = 0.25


def _ws_state(token: object, session_id: int, after: int, seen: dict[str, int]) -> tuple[AgentSessionStatus, list[dict]] | None:
    """(status, new frames) for the session, or None when the token or the session is not valid. `seen` is the revision of each live
    item already sent; it is updated here."""
    if not isinstance(token, str):
        return None
    try:
        user_id = int(decode_access_token(token)["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active or user.role not in (Role.admin, Role.operator):
            return None
        session = db.scalar(select(AgentSession).where(AgentSession.id == session_id, AgentSession.user_id == user.id))
        if session is None:
            return None
        rows = db.scalars(
            select(AgentEvent).where(AgentEvent.session_id == session_id, AgentEvent.seq > after).order_by(AgentEvent.seq).limit(500)
        )
        frames: list[dict] = [{"type": "event", "seq": e.seq, "kind": e.kind, "payload": e.payload} for e in rows]
        live = {row.key: row for row in db.scalars(select(AgentLive).where(AgentLive.session_id == session_id))}
        for key, row in live.items():
            if seen.get(key) != row.rev:
                seen[key] = row.rev
                frames.append({"type": "live", "key": key, "payload": row.payload})
        for key in [k for k in seen if k not in live]:
            del seen[key]
            frames.append({"type": "live_end", "key": key})
        return session.status, frames


@router.websocket("/sessions/{session_id}/ws")
async def events_socket(ws: WebSocket, session_id: int) -> None:
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        token = first.get("token") if isinstance(first, dict) else None
        after = int(first.get("from", 0)) - 1 if isinstance(first, dict) else -1
    except Exception:
        await ws.close(code=4401)
        return
    seen: dict[str, int] = {}
    state = await run_in_threadpool(_ws_state, token, session_id, after, seen)
    if state is None:
        await ws.close(code=4401)
        return
    seen.clear()  # the check above only proved the token; the first real read below sends the live items
    disconnect = asyncio.create_task(ws.receive_text())  # completes when the client goes away
    last_status = None
    try:
        while True:
            state = await run_in_threadpool(_ws_state, token, session_id, after, seen)
            if state is None:  # session deleted, user deactivated or demoted
                await ws.close(code=4404)
                return
            current, frames = state
            if current != last_status:
                await ws.send_json({"type": "status", "status": current.value})
                last_status = current
            for frame in frames:
                await ws.send_json(frame)
                if frame["type"] == "event":
                    after = frame["seq"]
            if disconnect.done():
                return
            if not frames:
                await asyncio.sleep(_WS_POLL_SECONDS)
    except Exception as exc:  # client gone mid-send
        log.debug("agent session %s websocket ended: %s", session_id, exc)
    finally:
        disconnect.cancel()
