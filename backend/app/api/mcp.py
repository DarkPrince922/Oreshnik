"""«MCP-серверы»: the user's own MCP servers plus the shared ones an administrator added."""

import json
import shlex
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.crypto import decrypt, encrypt
from app.core.db import get_db
from app.models.mcp_server import McpServer
from app.models.user import Role, User
from app.schemas.mcp import McpCheck, McpCreate, McpOut, McpUpdate, Secret

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

MAX_PER_USER = 50


def visible(user: User):
    return or_(McpServer.user_id == user.id, McpServer.shared.is_(True))


def _editable(user: User, server: McpServer) -> bool:
    return server.user_id == user.id if not server.shared else user.role == Role.admin


def _secrets(enc: str | None) -> dict[str, str]:
    return json.loads(decrypt(enc)) if enc else {}


def _store(values: dict[str, str]) -> str | None:
    return encrypt(json.dumps(values)) if values else None


def _merge(items: list[Secret], old: dict[str, str], what: str) -> dict[str, str]:
    """The new set of variables/headers: a value given replaces, none given keeps the stored one (a new name needs a value)."""
    out = {}
    for item in items:
        if item.value is not None:
            out[item.name] = item.value
        elif item.name in old:
            out[item.name] = old[item.name]
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{what} {item.name}: нужно значение")
    return out


def _own_names(db: Session, user: User) -> set[str]:
    return set(db.scalars(select(McpServer.name).where(McpServer.user_id == user.id, McpServer.shared.is_(False))))


def _out(user: User, server: McpServer, own: set[str]) -> McpOut:
    return McpOut(
        id=server.id, name=server.name, description=server.description, transport="url" if server.url else "command",
        command=server.command, url=server.url, env_names=sorted(_secrets(server.env_enc)), header_names=sorted(_secrets(server.headers_enc)),
        has_token=bool(server.token_enc), scope=server.scope, enabled=server.enabled, shared=server.shared,
        mine=server.user_id == user.id and not server.shared, editable=_editable(user, server), overridden=server.shared and server.name in own,
        startup_timeout_sec=server.startup_timeout_sec, tool_timeout_sec=server.tool_timeout_sec,
        last_check=McpCheck(**server.last_check) if server.last_check else None, created_at=server.created_at, updated_at=server.updated_at,
    )


def _get(db: Session, user: User, server_id: int, *, edit: bool = False) -> McpServer:
    server = db.scalar(select(McpServer).where(McpServer.id == server_id, visible(user)))
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP-сервер не найден")
    if edit and not _editable(user, server):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Общий MCP-сервер меняет только администратор")
    return server


def _taken(db: Session, user: User, name: str, shared: bool, not_id: int | None = None) -> bool:
    q = select(McpServer.id).where(McpServer.name == name, McpServer.shared.is_(shared))
    if not shared:
        q = q.where(McpServer.user_id == user.id)
    if not_id is not None:
        q = q.where(McpServer.id != not_id)
    return db.scalar(q) is not None


@router.get("", response_model=list[McpOut])
def list_servers(user: User = Depends(require_operator), db: Session = Depends(get_db)) -> list[McpOut]:
    own = _own_names(db, user)
    return [_out(user, s, own) for s in db.scalars(select(McpServer).where(visible(user)).order_by(McpServer.shared, McpServer.name))]


@router.post("", response_model=McpOut, status_code=status.HTTP_201_CREATED)
def create_server(body: McpCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> McpOut:
    if body.shared and user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Общие MCP-серверы добавляет только администратор")
    if db.scalar(select(func.count()).select_from(McpServer).where(McpServer.user_id == user.id)) >= MAX_PER_USER:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Не больше {MAX_PER_USER} MCP-серверов")
    if _taken(db, user, body.name, body.shared):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Сервер {body.name} уже есть")
    is_url = body.transport == "url"
    server = McpServer(
        user_id=user.id, shared=body.shared, name=body.name, description=body.description.strip(),
        command=None if is_url else body.command, url=body.url if is_url else None,
        env_enc=None if is_url else _store(_merge(body.env, {}, "Переменная")),
        headers_enc=_store(_merge(body.headers, {}, "Заголовок")) if is_url else None,
        token_enc=encrypt(body.bearer_token) if is_url and body.bearer_token else None,
        scope=body.scope, enabled=body.enabled, startup_timeout_sec=body.startup_timeout_sec, tool_timeout_sec=body.tool_timeout_sec,
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return _out(user, server, _own_names(db, user))


@router.patch("/{server_id}", response_model=McpOut)
def update_server(server_id: int, body: McpUpdate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> McpOut:
    server = _get(db, user, server_id, edit=True)
    if body.name is not None and body.name != server.name:
        if _taken(db, user, body.name, server.shared, server.id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"Сервер {body.name} уже есть")
        server.name = body.name
    if body.description is not None:
        server.description = body.description.strip()
    transport = body.transport or ("url" if server.url else "command")
    if transport == "url":
        url = body.url or server.url
        if not url:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "укажите адрес сервера")
        server.url, server.command, server.env_enc = url, None, None
        if body.headers is not None:
            server.headers_enc = _store(_merge(body.headers, _secrets(server.headers_enc), "Заголовок"))
        if body.bearer_token is not None:
            server.token_enc = encrypt(body.bearer_token) if body.bearer_token else None
    else:
        command = body.command or server.command
        if not command:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "укажите команду запуска")
        server.command, server.url, server.headers_enc, server.token_enc = command, None, None, None
        if body.env is not None:
            server.env_enc = _store(_merge(body.env, _secrets(server.env_enc), "Переменная"))
    for field in ("scope", "enabled", "startup_timeout_sec", "tool_timeout_sec"):
        value = getattr(body, field)
        if value is not None:
            setattr(server, field, value)
    server.last_check = None if body.model_fields_set - {"enabled", "description", "scope"} else server.last_check
    db.commit()
    db.refresh(server)
    return _out(user, server, _own_names(db, user))


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(server_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    db.delete(_get(db, user, server_id, edit=True))
    db.commit()


@router.post("/{server_id}/check", response_model=McpOut)
def check_server(server_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> McpOut:
    """Start the server in the agent container the way a session would and list its tools."""
    from app.services.agent import AgentError, check_mcp

    server = _get(db, user, server_id)
    try:
        result = check_mcp(agent_payload(server), timeout=server.startup_timeout_sec + 60)
    except AgentError as exc:
        result = {"ok": False, "tools": [], "error": str(exc)}
    server.last_check = {**result, "at": datetime.now(timezone.utc).isoformat()}
    db.commit()
    db.refresh(server)
    return _out(user, server, _own_names(db, user))


def agent_payload(server: McpServer) -> dict:
    """The server as agentd takes it (secrets decrypted: this goes to the agent container only)."""
    out: dict = {"name": server.name, "startup_timeout_sec": server.startup_timeout_sec, "tool_timeout_sec": server.tool_timeout_sec}
    if server.url:
        out.update(url=server.url, headers=_secrets(server.headers_enc), bearer_token=decrypt(server.token_enc) if server.token_enc else None)
    else:
        parts = shlex.split(server.command or "")
        out.update(command=parts[0], args=parts[1:], env=_secrets(server.env_enc))
    return out


def for_agent(db: Session, user_id: int, where: str) -> list[dict]:
    """The MCP servers an agent of this user gets: shared and own, enabled, meant for `where`; the user's own replaces a shared one of
    the same name."""
    from app.services.agent import AgentError

    rows = db.scalars(select(McpServer).where(or_(McpServer.user_id == user_id, McpServer.shared.is_(True)), McpServer.enabled.is_(True),
                                              McpServer.scope.in_(("both", where))))
    chosen: dict[str, McpServer] = {}
    for server in sorted(rows, key=lambda s: (not s.shared, s.id)):
        if server.shared or server.user_id == user_id:
            chosen[server.name] = server
    payload = [agent_payload(chosen[n]) for n in sorted(chosen)]
    seen: dict[str, tuple[str, str]] = {}
    for item in payload:
        for key, value in (item.get("env") or {}).items():
            if key in seen and seen[key][1] != value:
                raise AgentError(f"MCP-серверы {seen[key][0]} и {item['name']} задают переменную {key} по-разному: так их не запустить вместе", 422)
            seen[key] = (item["name"], value)
    return payload
