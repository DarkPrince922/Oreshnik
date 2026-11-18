"""«Навыки»: the user's own Agent Skills plus the shared ones an administrator made. Viewers have no agents, so no skills either."""

import base64
import binascii

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.db import get_db
from app.models.skill import Skill
from app.models.user import Role, User
from app.schemas.skill import SkillCreate, SkillFile, SkillFilePreview, SkillImport, SkillOut, SkillUpdate
from app.services import skills as svc

router = APIRouter(prefix="/api/skills", tags=["skills"])

MAX_PER_USER = 500


def visible(user: User):
    return or_(Skill.user_id == user.id, Skill.shared.is_(True))


def _editable(user: User, skill: Skill) -> bool:
    return skill.user_id == user.id if not skill.shared else user.role == Role.admin


def _out(user: User, skill: Skill, own_names: set[str], notes: list[str] | None = None) -> SkillOut:
    return SkillOut(
        id=skill.id, name=skill.name, description=skill.description, skill_md=skill.skill_md,
        files=[SkillFile(path=p, size=len(base64.b64decode(c))) for p, c in sorted((skill.files or {}).items())],
        scope=skill.scope, shared=skill.shared, enabled=skill.enabled, mine=skill.user_id == user.id and not skill.shared,
        editable=_editable(user, skill), overridden=skill.shared and skill.name in own_names, notes=notes or [],
        created_at=skill.created_at, updated_at=skill.updated_at,
    )


def _own_names(db: Session, user: User) -> set[str]:
    return set(db.scalars(select(Skill.name).where(Skill.user_id == user.id, Skill.shared.is_(False))))


def _get(db: Session, user: User, skill_id: int, *, edit: bool = False) -> Skill:
    skill = db.scalar(select(Skill).where(Skill.id == skill_id, visible(user)))
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Навык не найден")
    if edit and not _editable(user, skill):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Общий навык меняет только администратор")
    return skill


def _twin(db: Session, user: User, name: str, shared: bool, not_id: int | None = None) -> Skill | None:
    """The skill that already has this name in the same list (the user's own, or the shared ones)."""
    q = select(Skill).where(Skill.name == name, Skill.shared.is_(shared))
    if not shared:
        q = q.where(Skill.user_id == user.id)
    if not_id is not None:
        q = q.where(Skill.id != not_id)
    return db.scalar(q)


def _parse(text: str, fallback: str = "") -> svc.ParsedSkill:
    try:
        return svc.parse_skill_md(text, fallback)
    except svc.SkillError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _check_quota(db: Session, user: User) -> None:
    if db.scalar(select(func.count()).select_from(Skill).where(Skill.user_id == user.id)) >= MAX_PER_USER:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Не больше {MAX_PER_USER} навыков")


def _check_shared(user: User, shared: bool) -> None:
    if shared and user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Общие навыки создаёт только администратор")


@router.get("", response_model=list[SkillOut])
def list_skills(user: User = Depends(require_operator), db: Session = Depends(get_db)) -> list[SkillOut]:
    own = _own_names(db, user)
    rows = db.scalars(select(Skill).where(visible(user)).order_by(Skill.shared, Skill.name))
    return [_out(user, s, own) for s in rows]


@router.post("", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
def create_skill(body: SkillCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> SkillOut:
    _check_shared(user, body.shared)
    _check_quota(db, user)
    parsed = _parse(body.skill_md)
    if _twin(db, user, parsed.name, body.shared):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Навык {parsed.name} уже есть: измените его или выберите другое имя")
    skill = Skill(user_id=user.id, shared=body.shared, name=parsed.name, description=parsed.description, skill_md=parsed.skill_md,
                  files={}, scope=body.scope, enabled=body.enabled)
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return _out(user, skill, _own_names(db, user), parsed.notes)


@router.patch("/{skill_id}", response_model=SkillOut)
def update_skill(skill_id: int, body: SkillUpdate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> SkillOut:
    skill = _get(db, user, skill_id, edit=True)
    notes: list[str] = []
    if body.skill_md is not None:
        parsed = _parse(body.skill_md, skill.name)
        if parsed.name != skill.name and _twin(db, user, parsed.name, skill.shared, skill.id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"Навык {parsed.name} уже есть: выберите другое имя")
        try:
            svc.check_files(parsed.skill_md, skill.files or {})
        except svc.SkillError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
        skill.name, skill.description, skill.skill_md, notes = parsed.name, parsed.description, parsed.skill_md, parsed.notes
    if body.scope is not None:
        skill.scope = body.scope
    if body.enabled is not None:
        skill.enabled = body.enabled
    db.commit()
    db.refresh(skill)
    return _out(user, skill, _own_names(db, user), notes)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    db.delete(_get(db, user, skill_id, edit=True))
    db.commit()


@router.post("/import", response_model=SkillImport)
async def import_skills(
    file: UploadFile = File(...), shared: bool = Form(False), user: User = Depends(require_operator), db: Session = Depends(get_db),
) -> SkillImport:
    """A SKILL.md (or any .md) or a .zip with one or many skill folders — from Claude Code, OpenHands, Codex or a GitHub repository.
    A skill whose name is already in the list replaces it (its files too)."""
    _check_shared(user, shared)
    data = await file.read(svc.MAX_ZIP_BYTES + 1)
    filename = file.filename or ""
    skipped: list[str] = []
    try:
        if filename.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
            found, skipped = svc.read_archive(data)
        else:
            stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            found = [svc.parse_skill_md(data.decode("utf-8", "replace"), "" if stem.upper() == "SKILL" else stem)]
    except svc.SkillError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    saved: list[tuple[Skill, list[str]]] = []
    for parsed in found:
        skill = _twin(db, user, parsed.name, shared)
        if skill is None:
            _check_quota(db, user)
            skill = Skill(user_id=user.id, shared=shared, name=parsed.name, scope="both", enabled=True)
            db.add(skill)
            notes = parsed.notes
        else:
            notes = [*parsed.notes, "заменил навык с тем же именем"]
        skill.description, skill.skill_md, skill.files = parsed.description, parsed.skill_md, parsed.files
        db.flush()
        saved.append((skill, notes))
    db.commit()
    own = _own_names(db, user)
    return SkillImport(skills=[_out(user, s, own, n) for s, n in saved], skipped=skipped)


@router.get("/{skill_id}/export")
def export_skill(skill_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> Response:
    skill = _get(db, user, skill_id)
    data = svc.build_archive(skill.name, skill.skill_md, skill.files or {})
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{skill.name}.zip"'})


def _path(path: str) -> str:
    try:
        return svc.clean_path(path)
    except svc.SkillError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.put("/{skill_id}/files", response_model=SkillOut)
async def put_file(skill_id: int, request: Request, path: str = Query(...), user: User = Depends(require_operator), db: Session = Depends(get_db)) -> SkillOut:
    """Add or replace one file of the skill (a script, a reference, a template); the body is the file as it is."""
    skill = _get(db, user, skill_id, edit=True)
    path = _path(path)
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > svc.MAX_FILE:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Файл больше {svc.MAX_FILE // 1_000_000} МБ")
    files = {**(skill.files or {}), path: base64.b64encode(data).decode()}
    try:
        svc.check_files(skill.skill_md, files)
    except svc.SkillError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    skill.files = files
    db.commit()
    db.refresh(skill)
    return _out(user, skill, _own_names(db, user))


@router.delete("/{skill_id}/files", response_model=SkillOut)
def delete_file(skill_id: int, path: str = Query(...), user: User = Depends(require_operator), db: Session = Depends(get_db)) -> SkillOut:
    skill = _get(db, user, skill_id, edit=True)
    files = dict(skill.files or {})
    if files.pop(_path(path), None) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Нет такого файла")
    skill.files = files
    db.commit()
    db.refresh(skill)
    return _out(user, skill, _own_names(db, user))


@router.get("/{skill_id}/files/content", response_model=SkillFilePreview)
def read_file(skill_id: int, path: str = Query(...), user: User = Depends(require_operator), db: Session = Depends(get_db)) -> SkillFilePreview:
    skill = _get(db, user, skill_id)
    content = (skill.files or {}).get(_path(path))
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Нет такого файла")
    try:
        raw = base64.b64decode(content)
        text = raw.decode("utf-8") if b"\x00" not in raw[:8000] else None
    except (binascii.Error, UnicodeDecodeError):
        raw, text = base64.b64decode(content), None
    return SkillFilePreview(path=path, size=len(raw), text=text[:200_000] if text is not None else None)


def for_agent(db: Session, user_id: int, where: str, names: list[str] | None = None) -> list[Skill]:
    """The skills an agent of this user gets: the shared ones and the user's own, meant for `where` ("code"/"agent") — every enabled
    one, or, when the session's persona names its skills (`names`), exactly those, enabled or not. The user's own skill replaces a
    shared one of the same name."""
    q = select(Skill).where(or_(Skill.user_id == user_id, Skill.shared.is_(True)), Skill.scope.in_(("both", where)))
    q = q.where(Skill.enabled.is_(True)) if names is None else q.where(Skill.name.in_(names or [""]))
    rows = db.scalars(q)
    chosen: dict[str, Skill] = {}
    for skill in sorted(rows, key=lambda s: (not s.shared, s.id)):   # shared first, so the user's own of the same name wins
        if skill.shared or skill.user_id == user_id:
            chosen[skill.name] = skill
    return [chosen[n] for n in sorted(chosen)]
