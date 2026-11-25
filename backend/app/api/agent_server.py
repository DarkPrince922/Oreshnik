"""«Агент»: the user's own OpenHands server. Each user has at most one and sees only their own; an administrator decides who may
have one at all (`users.agent_access`; administrators always may). On it the agent runs as root, so it must be a dedicated machine."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.crypto import encrypt
from app.core.db import get_db
from app.models.agent_server import AgentServer
from app.models.user import Role, User
from app.schemas.agent_server import AgentServerIn, AgentServerOut, InstallLog, TrustKey
from app.services import openhands
from app.services.ssh import SshError, fingerprint

log = logging.getLogger("oreshnik.agent_server")

router = APIRouter(prefix="/api/agent-server", tags=["agent-server"])

INSTALL_STALE = timedelta(minutes=60)  # an installation that has not ended by then was lost with its worker


def require_agent_user(user: User = Depends(require_operator)) -> User:
    if user.role != Role.admin and not user.agent_access:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Раздел «Агент» вам не включён — попросите администратора")
    return user


def _fp(line: str | None) -> str | None:
    if not line:
        return None
    try:
        return fingerprint(line)
    except SshError:
        return None


def installing(server: AgentServer) -> bool:
    started = server.install_started_at
    return server.status == "installing" and started is not None and datetime.now(timezone.utc) - started < INSTALL_STALE


def _out(server: AgentServer) -> AgentServerOut:
    return AgentServerOut(
        ssh_host=server.ssh_host, ssh_port=server.ssh_port, ssh_user=server.ssh_user, has_key=bool(server.ssh_key_enc),
        has_password=bool(server.ssh_password_enc), host_key_fingerprint=_fp(server.ssh_host_key),
        pending_fingerprint=_fp(server.ssh_host_key_pending), status=server.status, status_detail=server.status_detail,
        openhands_version=server.openhands_version, installing=installing(server), checked_at=server.checked_at,
    )


def _own(db: Session, user: User) -> AgentServer | None:
    return db.scalar(select(AgentServer).where(AgentServer.user_id == user.id))


def _own_or_404(db: Session, user: User) -> AgentServer:
    server = _own(db, user)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сервер ещё не добавлен")
    return server


@router.get("", response_model=AgentServerOut | None)
def get_server(user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> AgentServerOut | None:
    server = _own(db, user)
    return _out(server) if server else None


@router.put("", response_model=AgentServerOut)
def save_server(body: AgentServerIn, user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> AgentServerOut:
    """Add the server, or change how it is reached. Moving to another address forgets the confirmed host key."""
    server = _own(db, user)
    if server is not None and installing(server):
        raise HTTPException(status.HTTP_409_CONFLICT, "Идёт установка — дождитесь её окончания")
    if server is None:
        server = AgentServer(user_id=user.id, ssh_host=body.ssh_host, ssh_port=body.ssh_port, ssh_user=body.ssh_user)
        db.add(server)
    elif (server.ssh_host, server.ssh_port) != (body.ssh_host, body.ssh_port):
        server.ssh_host_key = server.ssh_host_key_pending = None
        server.api_key_enc = None
        server.openhands_version = None
    server.ssh_host, server.ssh_port, server.ssh_user = body.ssh_host, body.ssh_port, body.ssh_user
    if body.clear_key:
        server.ssh_key_enc = None
    if body.clear_password:
        server.ssh_password_enc = None
    if body.ssh_private_key and body.ssh_private_key.strip():
        server.ssh_key_enc = encrypt(body.ssh_private_key.strip() + "\n")
    if body.ssh_password:
        server.ssh_password_enc = encrypt(body.ssh_password)
    if not (server.ssh_key_enc or server.ssh_password_enc):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Нужен SSH-ключ или пароль")
    server.status = "new" if not server.ssh_host_key else "offline"
    server.status_detail = "Сохранено — нажмите «Проверить»"
    db.commit()
    db.refresh(server)
    return _out(server)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> None:
    """Forget the server in the panel. Nothing is removed from the server itself."""
    server = _own_or_404(db, user)
    if installing(server):
        raise HTTPException(status.HTTP_409_CONFLICT, "Идёт установка — дождитесь её окончания")
    db.delete(server)
    db.commit()


@router.post("/check", response_model=AgentServerOut)
async def check_server(user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> AgentServerOut:
    server = _own_or_404(db, user)
    if installing(server):
        return _out(server)
    result = await run_in_threadpool(openhands.check, server)
    server.status, server.status_detail = result.status, result.detail
    server.checked_at = datetime.now(timezone.utc)
    if result.pending_host_key is not None:
        server.ssh_host_key_pending = result.pending_host_key
    if result.state is not None and result.state.canvas_version:
        server.openhands_version = result.state.canvas_version
    db.commit()
    db.refresh(server)
    return _out(server)


@router.post("/trust-host-key", response_model=AgentServerOut)
async def trust_host_key(body: TrustKey, user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> AgentServerOut:
    """Pin the key the panel has seen. The user compared its fingerprint with the server's own and sends it back."""
    server = _own_or_404(db, user)
    if not server.ssh_host_key_pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "Нет ключа для подтверждения — сначала нажмите «Проверить»")
    if server.ssh_host_key and server.ssh_host_key != server.ssh_host_key_pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сервер показывает другой ключ, чем подтверждённый. Если его переустанавливали — сначала сбросьте ключ.")
    if body.fingerprint.strip() != _fp(server.ssh_host_key_pending):
        raise HTTPException(status.HTTP_409_CONFLICT, "Отпечаток не совпадает с тем, что показывает сервер — проверьте заново и сверьте")
    server.ssh_host_key, server.ssh_host_key_pending = server.ssh_host_key_pending, None
    db.commit()
    return await check_server(user, db)


@router.post("/forget-host-key", response_model=AgentServerOut)
def forget_host_key(user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> AgentServerOut:
    server = _own_or_404(db, user)
    if installing(server):
        raise HTTPException(status.HTTP_409_CONFLICT, "Идёт установка — дождитесь её окончания")
    server.ssh_host_key = server.ssh_host_key_pending = None
    server.status, server.status_detail = "new", "Ключ сервера сброшен: нажмите «Проверить» и подтвердите новый отпечаток"
    db.commit()
    db.refresh(server)
    return _out(server)


@router.post("/install", response_model=AgentServerOut)
def install(user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> AgentServerOut:
    """Install (or repair, or update) OpenHands on the server. Runs on the worker; the log is at /install-log."""
    from app.worker.tasks import agent_server_install

    server = _own_or_404(db, user)
    if not server.ssh_host_key:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала подтвердите ключ сервера")
    if installing(server):
        raise HTTPException(status.HTTP_409_CONFLICT, "Установка уже идёт")
    server.status, server.status_detail = "installing", "Идёт установка…"
    server.install_log = ""
    server.install_started_at = datetime.now(timezone.utc)
    db.commit()
    try:
        agent_server_install.delay(server.id)
    except Exception as exc:  # broker down
        log.error("could not enqueue the OpenHands installation for user %s: %s", user.id, exc)
        server.status, server.status_detail, server.install_started_at = "offline", "Очередь задач недоступна — установка не начата", None
        db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Очередь задач недоступна, установка не начата")
    db.expire_all()
    return _out(_own_or_404(db, user))


@router.get("/install-log", response_model=InstallLog)
def install_log(user: User = Depends(require_agent_user), db: Session = Depends(get_db)) -> InstallLog:
    server = _own_or_404(db, user)
    return InstallLog(running=installing(server), log=(server.install_log or "")[-100_000:])
