"""«Личности»: the user's own personas, plus the shared ones an administrator made. Viewers have no agents, so no personas either."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.db import get_db
from app.models.agent import AgentSession
from app.models.persona import Persona
from app.models.user import Role, User
from app.schemas.persona import PersonaCreate, PersonaOut, PersonaUpdate

router = APIRouter(prefix="/api/personas", tags=["personas"])

MAX_PER_USER = 100


def visible(user: User):
    """What a user can see and pick: their own and the shared ones (never another user's private persona, an admin's included)."""
    return or_(Persona.user_id == user.id, Persona.shared.is_(True))


def _editable(user: User, persona: Persona) -> bool:
    return persona.user_id == user.id if not persona.shared else user.role == Role.admin


def _out(db: Session, user: User, persona: Persona) -> PersonaOut:
    used = db.scalar(select(func.count()).select_from(AgentSession).where(AgentSession.persona_id == persona.id, AgentSession.user_id == user.id))
    return PersonaOut(
        id=persona.id, name=persona.name, description=persona.description, instructions=persona.instructions, scope=persona.scope,
        shared=persona.shared, is_default=persona.is_default and persona.user_id == user.id, mine=persona.user_id == user.id,
        editable=_editable(user, persona), sessions=used or 0, created_at=persona.created_at, updated_at=persona.updated_at,
    )


def _get(db: Session, user: User, persona_id: int, *, edit: bool = False) -> Persona:
    persona = db.scalar(select(Persona).where(Persona.id == persona_id, visible(user)))
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Личность не найдена")
    if edit and not _editable(user, persona):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Общую личность меняет только администратор")
    return persona


def _clear_other_defaults(db: Session, user: User, keep: int) -> None:
    db.execute(update(Persona).where(Persona.user_id == user.id, Persona.id != keep).values(is_default=False))


@router.get("", response_model=list[PersonaOut])
def list_personas(user: User = Depends(require_operator), db: Session = Depends(get_db)) -> list[PersonaOut]:
    rows = db.scalars(select(Persona).where(visible(user)).order_by(Persona.shared, Persona.name))
    return [_out(db, user, p) for p in rows]


@router.post("", response_model=PersonaOut, status_code=status.HTTP_201_CREATED)
def create_persona(body: PersonaCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> PersonaOut:
    if body.shared and user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Общие личности создаёт только администратор")
    if db.scalar(select(func.count()).select_from(Persona).where(Persona.user_id == user.id)) >= MAX_PER_USER:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Не больше {MAX_PER_USER} личностей")
    persona = Persona(
        user_id=user.id, shared=body.shared, name=body.name, description=body.description.strip(), instructions=body.instructions,
        scope=body.scope, is_default=body.is_default and not body.shared,
    )
    db.add(persona)
    db.flush()
    if persona.is_default:
        _clear_other_defaults(db, user, persona.id)
    db.commit()
    db.refresh(persona)
    return _out(db, user, persona)


@router.patch("/{persona_id}", response_model=PersonaOut)
def update_persona(persona_id: int, body: PersonaUpdate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> PersonaOut:
    persona = _get(db, user, persona_id, edit=True)
    for field in ("name", "instructions", "scope"):
        value = getattr(body, field)
        if value is not None:
            setattr(persona, field, value)
    if body.description is not None:
        persona.description = body.description.strip()
    if body.is_default is not None and not persona.shared:
        persona.is_default = body.is_default
        if body.is_default:
            _clear_other_defaults(db, user, persona.id)
    db.commit()
    db.refresh(persona)
    return _out(db, user, persona)


@router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_persona(persona_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    persona = _get(db, user, persona_id, edit=True)
    db.delete(persona)  # sessions that ran with it keep going, without it (ON DELETE SET NULL)
    db.commit()


def pick_for_session(db: Session, user: User, persona_id: int | None, where: str, use_default: bool = False) -> Persona | None:
    """The persona a new session of «Код»/«Агент» runs with: the one asked for (it must be visible and allowed there). None means none —
    «без личности» picked on purpose; the user's own default only with `use_default` (the request did not mention a persona at all)."""
    if persona_id is not None:
        persona = db.scalar(select(Persona).where(Persona.id == persona_id, visible(user)))
        if persona is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Личность не найдена")
        if persona.scope not in ("both", where):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Эта личность не для этого раздела")
        return persona
    if not use_default:
        return None
    return db.scalar(select(Persona).where(Persona.user_id == user.id, Persona.is_default.is_(True), Persona.scope.in_(("both", where))))
