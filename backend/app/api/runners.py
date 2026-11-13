import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.core.crypto import encrypt
from app.core.db import get_db
from app.models.runner import Runner, RunnerKind, RunnerStatus
from app.models.user import Role, User
from app.schemas.runner import RunnerCreate, RunnerOut, RunnerUpdate
from app.worker.tasks import runner_check

log = logging.getLogger("oreshnik.runners")

router = APIRouter(prefix="/api/runners", tags=["runners"])

CHECK_TIMEOUT_SECONDS = 15


def _out(runner: Runner, viewer: User) -> RunnerOut:
    out = RunnerOut(
        id=runner.id,
        name=runner.name,
        kind=runner.kind,
        status=runner.status,
        status_detail=runner.status_detail,
        last_seen_at=runner.last_seen_at,
        created_at=runner.created_at,
    )
    if viewer.role == Role.admin:  # connection details are admin-only
        out.ssh_host = runner.ssh_host
        out.ssh_port = runner.ssh_port
        out.ssh_user = runner.ssh_user
        out.has_ssh_key = bool(runner.ssh_key_enc)
    return out


def _get_or_404(db: Session, runner_id: int) -> Runner:
    runner = db.get(Runner, runner_id)
    if runner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Раннер не найден")
    return runner


@router.get("", response_model=list[RunnerOut])
def list_runners(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[RunnerOut]:
    return [_out(r, user) for r in db.scalars(select(Runner).order_by(Runner.id))]


@router.post("", response_model=RunnerOut, status_code=status.HTTP_201_CREATED)
def create_runner(
    body: RunnerCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> RunnerOut:
    runner = Runner(
        name=body.name,
        kind=body.kind,
        ssh_host=body.ssh_host,
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user,
        ssh_key_enc=encrypt(body.ssh_private_key) if body.ssh_private_key else None,
        status=RunnerStatus.offline,
        status_detail="Ещё не проверялся",
    )
    db.add(runner)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Раннер с таким именем уже существует")
    db.refresh(runner)
    return _out(runner, admin)


@router.patch("/{runner_id}", response_model=RunnerOut)
def update_runner(
    runner_id: int,
    body: RunnerUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RunnerOut:
    runner = _get_or_404(db, runner_id)
    if body.name is not None:
        runner.name = body.name
    if runner.kind == RunnerKind.ssh:
        if body.ssh_host is not None:
            runner.ssh_host = body.ssh_host
        if body.ssh_port is not None:
            runner.ssh_port = body.ssh_port
        if body.ssh_user is not None:
            runner.ssh_user = body.ssh_user
        if body.ssh_private_key:
            runner.ssh_key_enc = encrypt(body.ssh_private_key)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Раннер с таким именем уже существует")
    db.refresh(runner)
    return _out(runner, admin)


@router.delete("/{runner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_runner(
    runner_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> None:
    db.delete(_get_or_404(db, runner_id))
    db.commit()


@router.post("/{runner_id}/check", response_model=RunnerOut)
def check_runner_now(
    runner_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> RunnerOut:
    """Run the health check on the worker and return the refreshed runner."""
    runner = _get_or_404(db, runner_id)
    try:
        runner_check.delay(runner.id).get(timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # worker down, broker down, or timeout
        log.warning("runner check for %s did not complete: %s", runner.id, exc)
        db.refresh(runner)
        runner.status = RunnerStatus.offline
        runner.status_detail = "Воркер не отвечает — проверьте, что он запущен"
        db.commit()
    db.refresh(runner)
    return _out(runner, admin)
