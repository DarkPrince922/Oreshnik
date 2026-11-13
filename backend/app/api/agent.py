"""«Агент»: chat with a coding agent (Codex CLI) that runs in the isolated agent container.

Sessions are private to their owner (an admin does not see other people's either): an agent runs commands with the
owner's own LLM key and its history may contain anything the owner pasted into it. Operators and admins use it; viewers don't.
"""

import asyncio
import json
import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.security import decode_access_token
from app.models.agent import AgentEvent, AgentSession, AgentSessionStatus
from app.models.llm_profile import LLMProfile
from app.models.user import Role, User
from app.schemas.agent import AgentEventOut, AgentMessage, AgentSessionCreate, AgentSessionOut
from app.services import agent as agent_service
from app.services.agent import AgentError
from app.worker.tasks import agent_turn as agent_turn_task

log = logging.getLogger("oreshnik.agent_api")

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _out(db: Session, session: AgentSession) -> AgentSessionOut:
    profile = db.get(LLMProfile, session.llm_profile_id) if session.llm_profile_id else None
    return AgentSessionOut(
        id=session.id, title=session.title, llm_profile_id=session.llm_profile_id,
        llm_profile_name=profile.name if profile else None, status=session.status, turn_count=session.turn_count,
        last_error=session.last_error, cancel_requested=session.cancel_requested, event_count=session.next_seq,
        created_at=session.created_at, updated_at=session.updated_at,
    )


def _own_or_404(db: Session, user: User, session_id: int) -> AgentSession:
    session = db.scalar(select(AgentSession).where(AgentSession.id == session_id, AgentSession.user_id == user.id))
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    return session


def _usable_profile(db: Session, user: User, profile_id: int) -> LLMProfile:
    profile = db.scalar(select(LLMProfile).where(LLMProfile.id == profile_id, LLMProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Профиль не найден")
    try:
        agent_service.endpoint_for(profile)
    except AgentError as exc:
        raise HTTPException(422, str(exc)) from exc
    return profile


@router.get("/health")
def health(_: User = Depends(require_operator)) -> dict:
    return agent_service.agent_health()


@router.get("/sessions", response_model=list[AgentSessionOut])
def list_sessions(user: User = Depends(require_operator), db: Session = Depends(get_db)) -> list[AgentSessionOut]:
    rows = db.scalars(select(AgentSession).where(AgentSession.user_id == user.id).order_by(AgentSession.updated_at.desc()))
    return [_out(db, s) for s in rows]


@router.post("/sessions", response_model=AgentSessionOut, status_code=status.HTTP_201_CREATED)
def create_session(body: AgentSessionCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    if not settings.agent_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Агент выключен: на сервере не задан AGENT_TOKEN")
    profile = _usable_profile(db, user, body.llm_profile_id)
    count = db.scalar(select(func.count()).select_from(AgentSession).where(AgentSession.user_id == user.id))
    if count >= settings.max_agent_sessions_per_user:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Не больше {settings.max_agent_sessions_per_user} сессий: удалите ненужные")
    session = AgentSession(user_id=user.id, title=body.title.strip() or "Новая сессия", llm_profile_id=profile.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return _out(db, session)


@router.get("/sessions/{session_id}", response_model=AgentSessionOut)
def get_session(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    return _out(db, _own_or_404(db, user, session_id))


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    session = _own_or_404(db, user, session_id)
    if session.status == AgentSessionStatus.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала остановите ход")
    key = session.key
    db.delete(session)
    db.commit()
    agent_service.forget_session(key)  # best effort: its working directory in the agent container


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
    if not settings.agent_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Агент выключен: на сервере не задан AGENT_TOKEN")
    if session.llm_profile_id is None:
        raise HTTPException(422, "Профиль LLM удалён: создайте новую сессию")
    _usable_profile(db, user, session.llm_profile_id)
    # One turn at a time: the claim is a single conditional UPDATE, so two clicks cannot both win.
    claimed = db.scalar(
        update(AgentSession)
        .where(AgentSession.id == session.id, AgentSession.status == AgentSessionStatus.idle)
        .values(status=AgentSessionStatus.running, cancel_requested=False, last_error=None, heartbeat_at=agent_service._now())
        .returning(AgentSession.id)
    )
    if claimed is None:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Агент ещё отвечает на прошлое сообщение")
    if agent_service.append_event(db, session.id, "user", {"text": body.text}) is None:
        session.status = AgentSessionStatus.idle
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "История сессии заполнена: начните новую")
    if session.turn_count == 0 and session.title == "Новая сессия":
        session.title = body.text.strip().splitlines()[0][:80]
    db.commit()
    db.refresh(session)
    agent_turn_task.delay(session.id, body.text)
    return _out(db, session)


@router.post("/sessions/{session_id}/stop", response_model=AgentSessionOut)
def stop(session_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> AgentSessionOut:
    session = _own_or_404(db, user, session_id)
    if session.status == AgentSessionStatus.running:
        session.cancel_requested = True
        db.commit()
        agent_service.stop_turn(session.key)
        db.refresh(session)
    return _out(db, session)


# ── live events over WebSocket ───────────────────────────────────────────
#
# Same shape as the scan log socket. The client's FIRST frame authenticates and says where to resume:
#   {"token": "<jwt>", "from": <next seq it has not seen>}
# Server frames: {"type":"status","status":...} | {"type":"event","seq":N,"kind":...,"payload":{...}}
# Closing codes: 4401 not authenticated, 4404 no such session. The socket stays open between turns.

_WS_POLL_SECONDS = 0.3


def _ws_state(token: object, session_id: int, after: int) -> tuple[AgentSessionStatus, list[dict]] | None:
    """(status, events after `after`) for the session, or None when the token or the session is not valid."""
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
        return session.status, [{"type": "event", "seq": e.seq, "kind": e.kind, "payload": e.payload} for e in rows]


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
    state = await run_in_threadpool(_ws_state, token, session_id, after)
    if state is None:
        await ws.close(code=4401)
        return
    disconnect = asyncio.create_task(ws.receive_text())  # completes when the client goes away
    last_status = None
    try:
        while True:
            state = await run_in_threadpool(_ws_state, token, session_id, after)
            if state is None:  # session deleted, user deactivated or demoted
                await ws.close(code=4404)
                return
            current, events = state
            if current != last_status:
                await ws.send_json({"type": "status", "status": current.value})
                last_status = current
            for event in events:
                await ws.send_json(event)
                after = event["seq"]
            if disconnect.done():
                return
            if not events:
                await asyncio.sleep(_WS_POLL_SECONDS)
    except Exception as exc:  # client gone mid-send
        log.debug("agent session %s websocket ended: %s", session_id, exc)
    finally:
        disconnect.cancel()
