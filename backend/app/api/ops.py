"""«Операции»: a plain-words goal turned into a shell script by an LLM and run as root over SSH on existing
runners — admin-only. Unlike every scan or schedule, there is no approval step before the script runs; see
`app/services/ops.py` and docs/ARCHITECTURE.md for why, and for the one thing that *is* still enforced
(the script is always stored and shown, and a narrow safety guard refuses a handful of outright destructive ones).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_admin
from app.core.db import get_db
from app.models.llm_profile import LLMProfile
from app.models.ops import OpsHostStatus, OpsRun, OpsRunHost, OpsRunStatus
from app.models.runner import Runner, RunnerKind
from app.models.user import User
from app.schemas.ops import OpsRunCreate, OpsRunHostOut, OpsRunOut
from app.services.scan_factory import ScanRequestError, check_runner_usable
from app.worker.tasks import ops_run as ops_run_task

log = logging.getLogger("oreshnik.ops_api")

router = APIRouter(prefix="/api/ops", tags=["ops"])


def _out(run: OpsRun) -> OpsRunOut:
    return OpsRunOut(
        id=run.id,
        user_id=run.user_id,
        owner_email=run.user.email,
        name=run.name,
        goal=run.goal,
        llm_profile_id=run.llm_profile_id,
        llm_profile_name=run.llm_profile.name if run.llm_profile else None,
        status=run.status,
        script=run.script,
        script_error=run.script_error,
        blocked_reason=run.blocked_reason,
        cancel_requested=run.cancel_requested,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        hosts=[OpsRunHostOut.model_validate(h) for h in run.hosts],
    )


def _with_relations(db: Session, run_id: int) -> OpsRun | None:
    return db.scalar(
        select(OpsRun)
        .where(OpsRun.id == run_id)
        .options(selectinload(OpsRun.hosts), selectinload(OpsRun.user), selectinload(OpsRun.llm_profile))
    )


def _get_or_404(db: Session, run_id: int) -> OpsRun:
    run = _with_relations(db, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return run


@router.get("/runners", response_model=list[dict])
def targetable_runners(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    """SSH runners usable as an operations target — the same pool scans already use (by design: see the
    architecture notes), filtered to what `check_runner_usable` would not immediately refuse."""
    rows = db.scalars(select(Runner).where(Runner.kind == RunnerKind.ssh).order_by(Runner.name))
    out = []
    for r in rows:
        try:
            check_runner_usable(r)
        except ScanRequestError:
            continue
        out.append({"id": r.id, "name": r.name})
    return out


@router.post("", response_model=OpsRunOut, status_code=status.HTTP_201_CREATED)
def create_run(body: OpsRunCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> OpsRunOut:
    profile = db.scalar(select(LLMProfile).where(LLMProfile.id == body.llm_profile_id, LLMProfile.user_id == admin.id))
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LLM-профиль не найден")

    runners = list(db.scalars(select(Runner).where(Runner.id.in_(body.runner_ids))))
    found = {r.id for r in runners}
    missing = [i for i in body.runner_ids if i not in found]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Раннер(ы) не найдены: {missing}")
    for r in runners:
        if r.kind != RunnerKind.ssh:
            raise HTTPException(422, f"Раннер «{r.name}» не SSH — операции выполняются только на SSH-раннерах")
        try:
            check_runner_usable(r)
        except ScanRequestError as exc:
            raise HTTPException(exc.status_code, f"Раннер «{r.name}»: {exc.detail}")

    run = OpsRun(
        user_id=admin.id,
        name=body.name.strip() or body.goal[:100],
        goal=body.goal,
        llm_profile_id=profile.id,
        status=OpsRunStatus.drafting,
    )
    db.add(run)
    db.flush()  # need run.id for the host rows below
    # Order follows the request, and matches `runners` only by id — a runner given twice was already deduplicated
    # by the schema, so this is a 1:1, order-preserving lookup.
    by_id = {r.id: r for r in runners}
    for runner_id in body.runner_ids:
        db.add(OpsRunHost(ops_run_id=run.id, runner_id=runner_id, runner_name=by_id[runner_id].name))
    db.commit()

    try:
        ops_run_task.delay(run.id)
    except Exception as exc:  # broker down
        log.error("could not enqueue ops run %s: %s", run.id, exc)
        run.status = OpsRunStatus.failed
        run.script_error = "Очередь задач недоступна — задача не запущена"
        for host in run.hosts:
            host.status = OpsHostStatus.failed
        db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Очередь задач недоступна, задача не запущена")

    db.expire_all()  # an eager/fast worker may already have finished the run
    return _out(_get_or_404(db, run.id))


@router.get("", response_model=list[OpsRunOut])
def list_runs(admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[OpsRunOut]:
    runs = db.scalars(
        select(OpsRun)
        .order_by(OpsRun.id.desc())
        .limit(200)
        .options(selectinload(OpsRun.hosts), selectinload(OpsRun.user), selectinload(OpsRun.llm_profile))
    )
    return [_out(r) for r in runs]


@router.get("/{run_id}", response_model=OpsRunOut)
def get_run(run_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> OpsRunOut:
    return _out(_get_or_404(db, run_id))


@router.post("/{run_id}/cancel", response_model=OpsRunOut)
def cancel_run(run_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> OpsRunOut:
    """Stop before the next host — the one already running is not interrupted mid-script."""
    run = _get_or_404(db, run_id)
    if run.status in (OpsRunStatus.done, OpsRunStatus.failed):
        raise HTTPException(status.HTTP_409_CONFLICT, "Задача уже завершена")
    run.cancel_requested = True
    db.commit()
    return _out(_get_or_404(db, run_id))
