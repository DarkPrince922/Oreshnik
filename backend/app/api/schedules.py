from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.config import settings
from app.core.db import get_db
from app.models.runner import Runner
from app.models.schedule import Schedule
from app.models.user import Role, User
from app.schemas.schedule import (
    ScheduleCreate,
    ScheduleOut,
    SchedulePreview,
    ScheduleRunOut,
    ScheduleUpdate,
)
from app.services import schedule as svc
from app.services.scan_factory import ScanRequestError, resolve_request
from app.worker.tasks import scan_run

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

# Visibility follows scans: an operator sees and manages their own schedules, an admin everyone's. Viewers get none:
# a schedule carries the scan instruction, which may contain test credentials.


def _visible(user: User):
    return Schedule.user_id == user.id if user.role == Role.operator else true()


def _get(db: Session, user: User, schedule_id: int) -> Schedule:
    schedule = db.scalar(select(Schedule).where(Schedule.id == schedule_id, _visible(user)))
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Расписание не найдено")
    return schedule


def _out(s: Schedule) -> ScheduleOut:
    return ScheduleOut(
        id=s.id,
        user_id=s.user_id,
        owner_email=s.user.email,
        name=s.name,
        cron=s.cron,
        timezone=s.timezone,
        enabled=s.enabled,
        targets=s.targets,
        scan_mode=s.scan_mode,
        instruction=s.instruction,
        max_budget=s.max_budget,
        runner_id=s.runner_id,
        runner_name=s.runner.name if s.runner else None,
        runner_kind=s.runner.kind.value if s.runner else None,
        llm_profile_id=s.llm_profile_id,
        llm_profile_name=s.llm_profile.name if s.llm_profile else None,
        llm_model=s.llm_model or (s.llm_profile.model if s.llm_profile else None),
        next_run_at=s.next_run_at,
        next_runs=svc.upcoming(s.cron, s.timezone, 3) if s.enabled else [],
        last_run_at=s.last_run_at,
        last_scan_id=s.last_scan_id,
        last_result=s.last_result,
        last_detail=s.last_detail,
        disabled_reason=s.disabled_reason,
        created_at=s.created_at,
    )


def _validate(db: Session, owner: User, *, cron, tz, targets, runner_id, llm_profile_id, max_budget, llm_model=None) -> Runner:
    """Everything that must hold for a schedule to be saved; returns the resolved runner."""
    try:
        cron = svc.normalize_cron(cron)
        svc.get_zone(tz)
        resolved = resolve_request(db, owner, targets=targets, runner_id=runner_id, llm_profile_id=llm_profile_id, llm_model=llm_model)
        svc.check_definition(cron, tz, resolved.runner.kind, max_budget)
    except svc.ScheduleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    except ScanRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return resolved.runner


@router.get("/preview", response_model=SchedulePreview)
def preview(
    cron: str = Query(max_length=100),
    timezone: str = Query(default="UTC", max_length=64),
    count: int = Query(default=5, ge=1, le=10),
    _: User = Depends(require_operator),
) -> SchedulePreview:
    """Used by the form: is this cron valid, and when would it fire?"""
    try:
        expr = svc.normalize_cron(cron)
        times = svc.upcoming(expr, timezone, count)
        return SchedulePreview(valid=True, next_runs=times, min_interval_minutes=svc.min_gap(expr, timezone).total_seconds() / 60)
    except svc.ScheduleError as exc:
        return SchedulePreview(valid=False, error=str(exc))


@router.get("", response_model=list[ScheduleOut])
def list_schedules(user: User = Depends(require_operator), db: Session = Depends(get_db)) -> list[ScheduleOut]:
    rows = db.scalars(select(Schedule).where(_visible(user)).order_by(Schedule.id)).all()
    return [_out(s) for s in rows]


@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
def create_schedule(body: ScheduleCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> ScheduleOut:
    count = db.scalar(select(func.count()).select_from(Schedule).where(Schedule.user_id == user.id))
    if count >= settings.max_schedules_per_user:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Достигнут лимит расписаний: {settings.max_schedules_per_user}")
    runner = _validate(
        db, user, cron=body.cron, tz=body.timezone, targets=body.targets, runner_id=body.runner_id,
        llm_profile_id=body.llm_profile_id, max_budget=body.max_budget, llm_model=body.llm_model or None,
    )
    cron = svc.normalize_cron(body.cron)
    resolved = resolve_request(db, user, targets=body.targets, runner_id=body.runner_id, llm_profile_id=body.llm_profile_id, llm_model=body.llm_model or None)
    schedule = Schedule(
        user_id=user.id,
        name=body.name,
        cron=cron,
        timezone=body.timezone,
        enabled=body.enabled,
        targets=resolved.targets,
        scan_mode=body.scan_mode,
        instruction=body.instruction,
        max_budget=body.max_budget,
        runner_id=runner.id,  # always explicit: a deleted runner must never fall back to the default one
        llm_profile_id=resolved.profile.id if resolved.profile else None,
        llm_model=resolved.model,
        next_run_at=svc.next_fire(cron, body.timezone) if body.enabled else None,
    )
    db.add(schedule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Расписание с таким именем уже существует")
    db.refresh(schedule)
    return _out(schedule)


@router.get("/{schedule_id}", response_model=ScheduleOut)
def get_schedule(schedule_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> ScheduleOut:
    return _out(_get(db, user, schedule_id))


@router.patch("/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    schedule_id: int, body: ScheduleUpdate, user: User = Depends(require_operator), db: Session = Depends(get_db)
) -> ScheduleOut:
    schedule = _get(db, user, schedule_id)
    sent = body.model_fields_set

    def pick(field: str, current):
        return getattr(body, field) if field in sent else current

    name = body.name if body.name is not None else schedule.name
    try:
        cron = svc.normalize_cron(body.cron) if body.cron is not None else schedule.cron
    except svc.ScheduleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    tz = body.timezone if body.timezone is not None else schedule.timezone
    enabled = body.enabled if body.enabled is not None else schedule.enabled
    targets = body.targets if body.targets is not None else schedule.targets
    scan_mode = body.scan_mode if body.scan_mode is not None else schedule.scan_mode
    instruction = pick("instruction", schedule.instruction)
    max_budget = pick("max_budget", schedule.max_budget)
    runner_id = body.runner_id if body.runner_id is not None else schedule.runner_id
    profile_id = pick("llm_profile_id", schedule.llm_profile_id)
    # A model belongs to a profile: moving to another profile without choosing a model means that profile's default.
    llm_model = (pick("llm_model", schedule.llm_model) or None) if ("llm_model" in sent or profile_id == schedule.llm_profile_id) else None
    if runner_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Раннер расписания удалён — выберите другой")

    # Validated against the OWNER (an admin editing someone's schedule cannot attach their own profile to it).
    runner = _validate(
        db, schedule.user, cron=cron, tz=tz, targets=targets, runner_id=runner_id,
        llm_profile_id=profile_id, max_budget=max_budget, llm_model=llm_model,
    )

    timing_changed = cron != schedule.cron or tz != schedule.timezone
    re_enabled = enabled and not schedule.enabled
    schedule.name, schedule.cron, schedule.timezone, schedule.enabled = name, cron, tz, enabled
    schedule.targets = resolve_request(db, schedule.user, targets=targets, runner_id=runner.id, llm_profile_id=profile_id, llm_model=llm_model).targets
    schedule.scan_mode, schedule.instruction, schedule.max_budget = scan_mode, instruction, max_budget
    schedule.runner_id, schedule.llm_profile_id, schedule.llm_model = runner.id, profile_id, llm_model
    if not enabled:
        schedule.next_run_at = None
    elif timing_changed or re_enabled or schedule.next_run_at is None:
        schedule.next_run_at = svc.next_fire(cron, tz)
    if re_enabled:
        schedule.disabled_reason = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Расписание с таким именем уже существует")
    db.refresh(schedule)
    return _out(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(schedule_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    db.delete(_get(db, user, schedule_id))  # its scans stay (their schedule_id is cleared)
    db.commit()


@router.post("/{schedule_id}/run", response_model=ScheduleRunOut, status_code=status.HTTP_201_CREATED)
def run_now(schedule_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> ScheduleRunOut:
    """Start one scan from the template right now (works for a switched-off schedule too)."""
    schedule = _get(db, user, schedule_id)
    try:
        scan = svc.start(db, schedule, lambda scan_id: scan_run.delay(scan_id))
    except ScanRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return ScheduleRunOut(scan_id=scan.id)
