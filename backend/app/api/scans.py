import asyncio
import fcntl
import json
import logging
from datetime import datetime, timezone
from typing import Literal

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select, true, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, require_operator
from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.security import decode_access_token
from app.models.llm_profile import LLMProfile
from app.models.runner import Runner, RunnerKind
from app.models.scan import ACTIVE_STATUSES, FINAL_STATUSES, Finding, Scan, ScanStatus
from app.models.schedule import Schedule
from app.models.user import Role, User
from app.schemas.scan import (
    ActivityPage,
    FindingBrief,
    FindingOut,
    LogPage,
    ReportOut,
    ScanContinue,
    ScanCreate,
    ScanList,
    ScanOut,
)
from app.services.activity import ActivityTailer
from app.services.artifacts import SEVERITY_ORDER
from app.services.scan_files import (
    activity_path,
    agents_path,
    find_run_dir,
    log_path,
    read_log_lines,
    remove_scan_dir,
    scan_dir,
    tail_from,
)
from app.services.resume import ResumeError, check_resumable
from app.services.scan_factory import ScanRequestError, create_queued_scan
from app.worker.tasks import scan_run

log = logging.getLogger("oreshnik.scans")

router = APIRouter(prefix="/api/scans", tags=["scans"])

# Visibility: admins and viewers see every scan (viewers are read-only); operators see their own.


def _visible(user: User):
    return Scan.user_id == user.id if user.role == Role.operator else true()


def _get_visible(db: Session, user: User, scan_id: int) -> Scan:
    scan = db.scalar(select(Scan).where(Scan.id == scan_id, _visible(user)))
    if scan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Скан не найден")
    return scan


def _severity_counts(db: Session, scan_ids: list[int]) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = {i: {} for i in scan_ids}
    if not scan_ids:
        return counts
    rows = db.execute(
        select(Finding.scan_id, Finding.severity, func.count())
        .where(Finding.scan_id.in_(scan_ids))
        .group_by(Finding.scan_id, Finding.severity)
    )
    for scan_id, severity, n in rows:
        counts[scan_id][severity] = n
    return counts


def _continuations(db: Session, viewer: User, scan_ids: list[int]) -> dict[int, list[int]]:
    """Scans that continue each of `scan_ids`, limited to the ones the viewer may see."""
    out: dict[int, list[int]] = {i: [] for i in scan_ids}
    if not scan_ids:
        return out
    rows = db.execute(
        select(Scan.parent_scan_id, Scan.id).where(Scan.parent_scan_id.in_(scan_ids), _visible(viewer)).order_by(Scan.id)
    )
    for parent_id, child_id in rows:
        out[parent_id].append(child_id)
    return out


def _schedule_names(db: Session, scans: list[Scan]) -> dict[int, str]:
    ids = {s.schedule_id for s in scans if s.schedule_id is not None}
    if not ids:
        return {}
    return dict(db.execute(select(Schedule.id, Schedule.name).where(Schedule.id.in_(ids))).all())


def _out(
    scan: Scan,
    viewer: User,
    counts: dict[str, int],
    continuations: list[int] | None = None,
    schedule_name: str | None = None,
) -> ScanOut:
    return ScanOut(
        id=scan.id,
        user_id=scan.user_id,
        owner_email=scan.user.email,
        runner_id=scan.runner_id,
        runner_name=scan.runner.name if scan.runner else None,
        runner_kind=scan.runner.kind.value if scan.runner else None,
        llm_profile_id=scan.llm_profile_id,
        llm_profile_name=scan.llm_profile.name if scan.llm_profile else None,
        targets=scan.targets,
        scan_mode=scan.scan_mode,
        instruction=None if viewer.role == Role.viewer else scan.instruction,
        max_budget=scan.max_budget,
        status=scan.status,
        status_detail=scan.status_detail,
        exit_code=scan.exit_code,
        run_name=scan.run_name,
        run_status=scan.run_status,
        cost=scan.cost,
        total_tokens=scan.total_tokens,
        created_at=scan.created_at,
        started_at=scan.started_at,
        finished_at=scan.finished_at,
        severity_counts=counts,
        findings_total=sum(counts.values()),
        schedule_id=scan.schedule_id,
        schedule_name=schedule_name,
        parent_scan_id=scan.parent_scan_id,
        cost_base=scan.cost_base,
        continuations=continuations or [],
    )


def _out_one(db: Session, scan: Scan, viewer: User) -> ScanOut:
    return _out(
        scan, viewer, _severity_counts(db, [scan.id])[scan.id], _continuations(db, viewer, [scan.id])[scan.id],
        _schedule_names(db, [scan]).get(scan.schedule_id),
    )


# ── create / list / read ─────────────────────────────────────────────────


@router.post("", response_model=ScanOut, status_code=status.HTTP_201_CREATED)
def create_scan(
    body: ScanCreate, user: User = Depends(require_operator), db: Session = Depends(get_db)
) -> ScanOut:
    try:
        scan = create_queued_scan(
            db,
            user,
            targets=body.targets,
            scan_mode=body.scan_mode,
            instruction=body.instruction,
            max_budget=body.max_budget,
            runner_id=body.runner_id,
            llm_profile_id=body.llm_profile_id,
        )
    except ScanRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return _enqueue(db, scan, user)


def _enqueue(db: Session, scan: Scan, user: User) -> ScanOut:
    try:
        scan_run.delay(scan.id)
    except Exception as exc:  # broker down
        log.error("could not enqueue scan %s: %s", scan.id, exc)
        scan.status = ScanStatus.failed
        scan.status_detail = "Очередь задач недоступна"
        scan.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Очередь задач недоступна, скан не запущен")

    db.expire_all()  # an eager/fast worker may already have finished the scan
    return _out_one(db, db.get(Scan, scan.id), user)


@router.post("/{scan_id}/continue", response_model=ScanOut, status_code=status.HTTP_201_CREATED)
def continue_scan(
    scan_id: int, body: ScanContinue, user: User = Depends(require_operator), db: Session = Depends(get_db)
) -> ScanOut:
    """Continue a finished (or stopped) scan with new instructions — `strix --resume`.

    Creates a NEW scan that works on a copy of the parent's run: Strix keeps every agent's history and
    the findings so far, and hands the new instruction to the root agent. The parent stays untouched.
    """
    parent = _get_visible(db, user, scan_id)
    if parent.status not in FINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Скан ещё выполняется — продолжить можно только завершённый")
    try:
        check_resumable(parent.id, parent.run_name)
    except ResumeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    runner = parent.runner
    if runner is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Раннер этого скана удалён")
    if runner.kind == RunnerKind.ssh:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "SSH-раннеры пока не поддерживаются")

    # A scan that finished on its own has nothing left to do without a new instruction; a stopped or failed
    # one can simply be picked up again.
    if parent.status in (ScanStatus.succeeded, ScanStatus.vulns_found) and not body.instruction:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Опишите, что агентам делать дальше")

    # The key is personal: use one of YOUR profiles (the parent's only if it is yours).
    profile: LLMProfile | None = None
    if body.llm_profile_id is not None:
        profile = db.scalar(select(LLMProfile).where(LLMProfile.id == body.llm_profile_id, LLMProfile.user_id == user.id))
        if profile is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "LLM-профиль не найден")
    elif runner.kind != RunnerKind.mock:
        if parent.llm_profile is not None and parent.llm_profile.user_id == user.id:
            profile = parent.llm_profile
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Выберите свой LLM-профиль")

    # Strix compares the budget with the cost of the WHOLE run, so a limit at or below what was already
    # spent would stop the continuation immediately.
    if body.max_budget is not None and parent.cost is not None and body.max_budget <= parent.cost:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Лимит бюджета общий для всего прогона, а уже потрачено ${parent.cost:.2f}. Укажите больше или оставьте пустым.",
        )

    # Strix names the sandbox containers after the run, so one run must never be active twice.
    busy = db.scalar(
        select(Scan.id)
        .where(Scan.status.in_(ACTIVE_STATUSES), or_(Scan.resume_run_name == parent.run_name, Scan.run_name == parent.run_name))
        .limit(1)
    )
    if busy is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Этот прогон уже продолжается в скане #{busy}")

    active = db.scalar(
        select(func.count()).select_from(Scan).where(Scan.user_id == user.id, Scan.status.in_(ACTIVE_STATUSES))
    )
    if active >= settings.max_active_scans_per_user:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Уже запущено сканов: {active}. Лимит — {settings.max_active_scans_per_user}; дождитесь завершения или отмените лишние.",
        )

    child = Scan(
        user_id=user.id,
        runner_id=runner.id,
        llm_profile_id=profile.id if profile else None,
        targets=list(parent.targets),
        scan_mode=parent.scan_mode,
        instruction=body.instruction,
        max_budget=body.max_budget,
        status=ScanStatus.queued,
        parent_scan_id=parent.id,
        resume_run_name=parent.run_name,
        cost_base=parent.cost or 0.0,
    )
    db.add(child)
    db.commit()
    return _enqueue(db, child, user)


@router.get("", response_model=ScanList)
def list_scans(
    status_filter: ScanStatus | None = Query(default=None, alias="status"),
    schedule_id: int | None = Query(default=None, description="only scans started by this schedule"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScanList:
    where = [_visible(user)]
    if status_filter is not None:
        where.append(Scan.status == status_filter)
    if schedule_id is not None:
        where.append(Scan.schedule_id == schedule_id)
    total = db.scalar(select(func.count()).select_from(Scan).where(*where))
    scans = db.scalars(select(Scan).where(*where).order_by(Scan.id.desc()).limit(limit).offset(offset)).all()
    ids = [s.id for s in scans]
    counts, children = _severity_counts(db, ids), _continuations(db, user, ids)
    names = _schedule_names(db, list(scans))
    return ScanList(
        items=[_out(s, user, counts[s.id], children[s.id], names.get(s.schedule_id)) for s in scans],
        total=total, limit=limit, offset=offset,
    )


@router.get("/{scan_id}", response_model=ScanOut)
def get_scan(scan_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ScanOut:
    return _out_one(db, _get_visible(db, user, scan_id), user)


# ── control ──────────────────────────────────────────────────────────────


@router.post("/{scan_id}/cancel", response_model=ScanOut)
def cancel_scan(scan_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> ScanOut:
    scan = _get_visible(db, user, scan_id)
    # Conditional updates: a scan that a worker is starting right now can't slip past a cancel.
    if db.execute(
        update(Scan)
        .where(Scan.id == scan_id, Scan.status == ScanStatus.queued)
        .values(status=ScanStatus.cancelled, finished_at=datetime.now(timezone.utc), status_detail="Отменён до запуска")
    ).rowcount == 0:
        if db.execute(
            update(Scan).where(Scan.id == scan_id, Scan.status == ScanStatus.running).values(status=ScanStatus.cancelling)
        ).rowcount == 0 and scan.status != ScanStatus.cancelling:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "Скан уже завершён")
    db.commit()
    db.expire_all()
    return _out_one(db, db.get(Scan, scan_id), user)


@router.delete("/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scan(scan_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)) -> None:
    scan = _get_visible(db, user, scan_id)
    if scan.status in ACTIVE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Скан ещё выполняется — сначала отмените его")
    waiting = db.scalar(
        select(Scan.id).where(Scan.parent_scan_id == scan_id, Scan.status.in_(ACTIVE_STATUSES)).limit(1)
    )
    if waiting is not None:  # it copies this scan's run directory when it starts
        raise HTTPException(status.HTTP_409_CONFLICT, f"Скан #{waiting} продолжает этот скан — дождитесь его завершения")
    db.delete(scan)
    db.commit()
    remove_scan_dir(scan_id)


# ── results ──────────────────────────────────────────────────────────────


@router.get("/{scan_id}/log", response_model=LogPage)
def get_log(
    scan_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LogPage:
    scan = _get_visible(db, user, scan_id)
    lines, next_offset = read_log_lines(scan_id, offset, limit)
    return LogPage(lines=lines, next_offset=next_offset, finished=scan.status in FINAL_STATUSES)


ACTIVITY_PAGE_BYTES = 1_000_000


@router.get("/{scan_id}/activity", response_model=ActivityPage)
def get_activity(
    scan_id: int,
    cursor: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActivityPage:
    """What the agents did: commands, tool output, messages, and the agent roster.

    Raw agent transcripts are the most sensitive artifact of a scan (they contain every command,
    including any credentials the instruction handed to the agents), so — unlike findings — they are
    not shown to the read-only `viewer` role.
    """
    scan = _get_visible(db, user, scan_id)
    if user.role == Role.viewer:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "История действий агентов недоступна для роли viewer")
    if scan.status in FINAL_STATUSES:
        _backfill_activity(scan_id)
    lines, next_cursor = tail_from(activity_path(scan_id), cursor, ACTIVITY_PAGE_BYTES)
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a corrupt line must not hide the rest
    return ActivityPage(
        events=events,
        next_cursor=next_cursor,
        agents=read_roster_snapshot(scan_id),
        finished=scan.status in FINAL_STATUSES,
    )


def _backfill_activity(scan_id: int) -> None:
    """Build activity.jsonl from Strix's agents.db for a FINISHED scan that has none.

    Covers scans that ran before this feature existed or under an older worker. Only done for finished
    scans: nothing is writing to the database any more, so reading it from the API process is safe.
    An exclusive file lock keeps concurrent requests from writing the same events twice.
    """
    if activity_path(scan_id).exists() or find_run_dir(scan_dir(scan_id)) is None:
        return
    lock_path = scan_dir(scan_id) / ".activity.lock"
    try:
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not activity_path(scan_id).exists():  # another request may have finished it while we waited
                ActivityTailer(scan_id).poll()
    except OSError as exc:
        log.warning("could not backfill activity for scan %s: %s", scan_id, exc)


def read_roster_snapshot(scan_id: int) -> list[dict]:
    path = agents_path(scan_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


@router.get("/{scan_id}/findings", response_model=list[FindingBrief])
def list_findings(scan_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Finding]:
    _get_visible(db, user, scan_id)
    rows = db.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
    return sorted(rows, key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.id))


@router.get("/{scan_id}/findings/{finding_id}", response_model=FindingOut)
def get_finding(
    scan_id: int, finding_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Finding:
    _get_visible(db, user, scan_id)
    finding = db.scalar(select(Finding).where(Finding.id == finding_id, Finding.scan_id == scan_id))
    if finding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Находка не найдена")
    return finding


@router.get("/{scan_id}/report", response_model=ReportOut)
def get_report(scan_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ReportOut:
    _get_visible(db, user, scan_id)
    run_dir = find_run_dir(scan_dir(scan_id))
    report = run_dir / "penetration_test_report.md" if run_dir else None
    if report is None or not report.is_file():
        return ReportOut(markdown=None)
    return ReportOut(markdown=report.read_text(encoding="utf-8", errors="replace"))


_EXPORTS = {
    "sarif": ("findings.sarif", "application/sarif+json"),
    "md": ("penetration_test_report.md", "text/markdown; charset=utf-8"),
}


@router.get("/{scan_id}/export/{fmt}")
def export_scan(
    scan_id: int,
    fmt: Literal["json", "sarif", "md"],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_visible(db, user, scan_id)
    base = f"oreshnik-scan-{scan_id}"
    # Always served as an attachment: the content is attacker-influenced and must not render in-browser.
    if fmt == "json":
        rows = db.scalars(select(Finding).where(Finding.scan_id == scan_id).order_by(Finding.id)).all()
        return JSONResponse(
            [f.data for f in rows],
            headers={"Content-Disposition": f'attachment; filename="{base}-vulnerabilities.json"', "X-Content-Type-Options": "nosniff"},
        )
    filename, media_type = _EXPORTS[fmt]
    run_dir = find_run_dir(scan_dir(scan_id))
    path = run_dir / filename if run_dir else None
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл ещё не создан")
    return FileResponse(path, media_type=media_type, filename=f"{base}-{filename}", headers={"X-Content-Type-Options": "nosniff"})


# ── live log over WebSocket ──────────────────────────────────────────────
#
# Protocol (JSON text frames). The client's FIRST frame authenticates (so the token never
# appears in a URL or access log):   {"token": "<jwt>", "from": 0}
# Server frames:  {"type":"status","status":...} | {"type":"log","seq":N,"line":"..."} | {"type":"end","status":...}
# Closing codes:  4401 not authenticated, 4404 scan not found.

_WS_POLL_SECONDS = 0.3


def _ws_authorize(token: object, scan_id: int) -> bool:
    if not isinstance(token, str):
        return False
    try:
        user_id = int(decode_access_token(token)["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return False
    with SessionLocal() as db:
        user = db.get(User, user_id)
        return bool(user and user.is_active)


def _ws_scan_status(token: str, scan_id: int) -> ScanStatus | None:
    with SessionLocal() as db:
        user = db.get(User, int(decode_access_token(token)["sub"]))
        if user is None or not user.is_active:
            return None
        return db.scalar(select(Scan.status).where(Scan.id == scan_id, _visible(user)))


@router.websocket("/{scan_id}/ws")
async def scan_log_socket(ws: WebSocket, scan_id: int) -> None:
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        token = first.get("token") if isinstance(first, dict) else None
        skip = int(first.get("from", 0)) if isinstance(first, dict) else 0
    except Exception:
        await ws.close(code=4401)
        return
    if not await run_in_threadpool(_ws_authorize, token, scan_id):
        await ws.close(code=4401)
        return
    current = await run_in_threadpool(_ws_scan_status, token, scan_id)
    if current is None:
        await ws.close(code=4404)
        return

    path = log_path(scan_id)
    position, seq, last_status = 0, 0, current
    disconnect = asyncio.create_task(ws.receive_text())  # completes when the client goes away
    try:
        # State first, so a client can render "running"/"finished" before the log replay starts.
        await ws.send_json({"type": "status", "status": current.value})
        while True:
            lines, position = await run_in_threadpool(tail_from, path, position)
            for line in lines:
                if seq >= skip:
                    await ws.send_json({"type": "log", "seq": seq, "line": line})
                seq += 1

            current = await run_in_threadpool(_ws_scan_status, token, scan_id)
            if current is None:  # scan deleted or access revoked
                await ws.close(code=4404)
                return
            if current != last_status:
                await ws.send_json({"type": "status", "status": current.value})
                last_status = current

            if current in FINAL_STATUSES:
                # The worker closes the log before it publishes the final status, so this drain is complete.
                lines, position = await run_in_threadpool(tail_from, path, position)
                for line in lines:
                    if seq >= skip:
                        await ws.send_json({"type": "log", "seq": seq, "line": line})
                    seq += 1
                await ws.send_json({"type": "end", "status": current.value})
                await ws.close()
                return

            if disconnect.done():
                return
            await asyncio.sleep(_WS_POLL_SECONDS)
    except Exception as exc:  # client gone mid-send
        log.debug("scan %s websocket ended: %s", scan_id, exc)
    finally:
        disconnect.cancel()
