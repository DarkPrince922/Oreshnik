import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.core.crypto import encrypt
from app.core.db import get_db
from app.models.runner import Runner, RunnerKind, RunnerStatus
from app.models.user import Role, User
from app.schemas.runner import ProvisionLog, RunnerCreate, RunnerOut, RunnerUpdate, TrustHostKey
from app.services.ssh import SshError, fingerprint, key_algorithm
from app.services.ssh_runner import is_provisioning
from app.worker.tasks import runner_check, runner_provision

log = logging.getLogger("oreshnik.runners")

router = APIRouter(prefix="/api/runners", tags=["runners"])

CHECK_TIMEOUT_SECONDS = 15
SSH_CHECK_TIMEOUT_SECONDS = 60  # connecting, logging in and probing a remote host takes longer than looking at a local path


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
        out.ssh_host_key_fingerprint = _fingerprint(runner.ssh_host_key)
        out.ssh_host_key_pending_fingerprint = _fingerprint(runner.ssh_host_key_pending)
        out.ssh_host_key_algorithm = key_algorithm(runner.ssh_host_key or runner.ssh_host_key_pending or "") or None
        out.has_provision_log = bool(runner.provision_log)
    return out


def _fingerprint(line: str | None) -> str | None:
    if not line:
        return None
    try:
        return fingerprint(line)
    except SshError:
        return None


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
        if is_provisioning(runner):
            raise HTTPException(status.HTTP_409_CONFLICT, "Идёт установка — дождитесь её окончания")
        moved = (body.ssh_host is not None and body.ssh_host != runner.ssh_host) or (
            body.ssh_port is not None and body.ssh_port != runner.ssh_port
        )
        if body.ssh_host is not None:
            runner.ssh_host = body.ssh_host
        if body.ssh_port is not None:
            runner.ssh_port = body.ssh_port
        if body.ssh_user is not None:
            runner.ssh_user = body.ssh_user
        if body.ssh_private_key:
            runner.ssh_key_enc = encrypt(body.ssh_private_key)
        if moved:
            # A different address is a different machine: what was confirmed for the old one means nothing here.
            runner.ssh_host_key = runner.ssh_host_key_pending = None
            runner.status = RunnerStatus.offline
            runner.status_detail = "Адрес изменён: ключ хоста нужно подтвердить заново"
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
        timeout = SSH_CHECK_TIMEOUT_SECONDS if runner.kind == RunnerKind.ssh else CHECK_TIMEOUT_SECONDS
        runner_check.delay(runner.id).get(timeout=timeout)
    except Exception as exc:  # worker down, broker down, or timeout
        log.warning("runner check for %s did not complete: %s", runner.id, exc)
        db.refresh(runner)
        runner.status = RunnerStatus.offline
        runner.status_detail = "Воркер не отвечает — проверьте, что он запущен"
        db.commit()
    db.refresh(runner)
    return _out(runner, admin)


def _ssh_runner(db: Session, runner_id: int) -> Runner:
    runner = _get_or_404(db, runner_id)
    if runner.kind != RunnerKind.ssh:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Это действие только для SSH-раннеров")
    return runner


@router.post("/{runner_id}/trust-host-key", response_model=RunnerOut)
def trust_host_key(
    runner_id: int, body: TrustHostKey, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> RunnerOut:
    """Pin the host key the panel has seen. The administrator must have compared its fingerprint with the server's own, and
    sends it back: if the host has shown a different key in the meantime, nothing is pinned."""
    runner = _ssh_runner(db, runner_id)
    if not runner.ssh_host_key_pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "Нет ключа для подтверждения — сначала проверьте раннер")
    if runner.ssh_host_key and runner.ssh_host_key != runner.ssh_host_key_pending:
        # A key is already pinned and the host now shows another one: that is what an attack looks like, and also what a
        # reinstalled server looks like. Make the administrator say so explicitly instead of one click away.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Хост показывает другой ключ, чем подтверждённый. Если сервер переустанавливали — сначала сбросьте старый ключ.",
        )
    shown = _fingerprint(runner.ssh_host_key_pending)
    if body.fingerprint.strip() != shown:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Отпечаток не совпадает с тем, что показывает хост сейчас — проверьте раннер заново и сверьте"
        )
    runner.ssh_host_key = runner.ssh_host_key_pending
    runner.ssh_host_key_pending = None
    runner.status = RunnerStatus.offline
    runner.status_detail = "Ключ хоста подтверждён — выполняется проверка"
    db.commit()
    return check_runner_now(runner_id, admin, db)


@router.post("/{runner_id}/forget-host-key", response_model=RunnerOut)
def forget_host_key(runner_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> RunnerOut:
    """Drop the pinned key (the server was reinstalled, say). The next check shows the new fingerprint for confirmation."""
    runner = _ssh_runner(db, runner_id)
    if is_provisioning(runner):
        raise HTTPException(status.HTTP_409_CONFLICT, "Идёт установка — дождитесь её окончания")
    runner.ssh_host_key = runner.ssh_host_key_pending = None
    runner.status = RunnerStatus.offline
    runner.status_detail = "Ключ хоста сброшен: проверьте раннер и подтвердите новый отпечаток"
    db.commit()
    db.refresh(runner)
    return _out(runner, admin)


@router.post("/{runner_id}/provision", response_model=RunnerOut)
def provision_runner(runner_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> RunnerOut:
    """Install what the host is missing (Docker, Strix, …) over SSH. Runs on the worker; progress is in `provision-log`.
    Only for a host whose key an administrator has confirmed — the installers run with the host's privileges."""
    runner = _ssh_runner(db, runner_id)
    if not runner.ssh_host_key:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала подтвердите ключ хоста")
    if is_provisioning(runner):
        raise HTTPException(status.HTTP_409_CONFLICT, "Установка уже идёт")
    runner.status = RunnerStatus.provisioning
    runner.status_detail = "Идёт установка…"
    runner.provision_log = ""
    runner.provision_started_at = datetime.now(timezone.utc)
    db.commit()
    try:
        runner_provision.delay(runner.id)
    except Exception as exc:  # broker down
        log.error("could not enqueue the installation for runner %s: %s", runner.id, exc)
        runner.status = RunnerStatus.offline
        runner.status_detail = "Очередь задач недоступна — установка не начата"
        runner.provision_started_at = None
        db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Очередь задач недоступна, установка не начата")
    db.expire_all()
    return _out(_get_or_404(db, runner_id), admin)


@router.get("/{runner_id}/provision-log", response_model=ProvisionLog)
def provision_log(runner_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> ProvisionLog:
    runner = _ssh_runner(db, runner_id)
    return ProvisionLog(running=is_provisioning(runner), log=(runner.provision_log or "")[-100_000:])
