import logging
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.engine import RunResult, ScanSpec, get_runner
from app.engine.docker_cleanup import cleanup_containers, run_label
from app.models.runner import Runner, RunnerKind, RunnerStatus
from app.models.scan import Finding, Scan, ScanStatus
from app.models.schedule import Schedule
from app.services.activity import ActivityTailer
from app.services.artifacts import ParsedRun, parse_run_dir, read_live_usage
from app.services.llm import build_strix_env
from app.services.resume import ResumeError, check_resumable, stage_run
from app.services.runner_health import check_runner
from app.services.scan_files import LogWriter, find_run_dir, scan_dir
from app.services.scan_outcome import decide
from app.services.schedule import fire_scheduled, next_fire
from app.worker.celery_app import celery

log = logging.getLogger("oreshnik.worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── runner health ────────────────────────────────────────────────────────


@celery.task(name="oreshnik.runner_check")
def runner_check(runner_id: int) -> dict:
    """Health-check a runner from the worker's point of view and persist the result."""
    with SessionLocal() as db:
        runner = db.get(Runner, runner_id)
        if runner is None:
            return {"status": "error", "detail": "runner not found"}
        status, detail = check_runner(runner)
        runner.status = status
        runner.status_detail = detail
        if status == RunnerStatus.online:
            runner.last_seen_at = _utcnow()
        db.commit()
        return {"status": status.value, "detail": detail}


# ── scan execution ───────────────────────────────────────────────────────


class _Preflight(Exception):
    """A problem found before the run starts; the message is shown to the user."""


@dataclass
class _Snapshot:
    scan_id: int
    runner_kind: RunnerKind | None
    targets: list[str]
    scan_mode: str
    instruction: str | None
    max_budget: float | None
    has_profile: bool
    env: dict[str, str]
    env_error: str | None
    parent_scan_id: int | None = None
    resume_run: str | None = None
    # Continuing THIS scan (not a copy of another): which time it is, counting from 1; 0 for a first run or a fork.
    resume_count: int = 0


def _claim(scan_id: int) -> _Snapshot | None:
    """Atomically move queued -> running; None if the scan is gone or was cancelled meanwhile."""
    with SessionLocal() as db:
        claimed = db.execute(
            update(Scan)
            .where(Scan.id == scan_id, Scan.status == ScanStatus.queued)
            .values(status=ScanStatus.running, started_at=_utcnow(), heartbeat_at=_utcnow())
        )
        db.commit()
        if claimed.rowcount == 0:
            return None
        scan = db.get(Scan, scan_id)
        env: dict[str, str] = {}
        env_error = None
        if scan.llm_profile is not None:
            try:
                env = build_strix_env(scan.llm_profile)  # decrypts the key; only lives in this process
            except Exception as exc:
                env_error = f"Не удалось подготовить LLM-профиль ({exc.__class__.__name__})"
        # The original instruction was handed to the agents when the scan first started; a resume carries only what
        # was asked for THIS time (nothing, if the user just picked the scan up again).
        instruction = scan.instruction
        if scan.resume_count > 0:
            history = scan.resume_history or []
            instruction = (history[-1].get("instruction") if history else None) or None
        return _Snapshot(
            scan_id=scan.id,
            runner_kind=scan.runner.kind if scan.runner else None,
            targets=list(scan.targets),
            scan_mode=scan.scan_mode.value,
            instruction=instruction,
            max_budget=scan.max_budget,
            has_profile=scan.llm_profile is not None,
            env=env,
            env_error=env_error,
            parent_scan_id=scan.parent_scan_id,
            resume_run=scan.resume_run_name,
            resume_count=scan.resume_count,
        )


class _CancelProbe:
    """Polled by the runner several times a second; hits the DB at most every 2 s.

    Doubles as the heartbeat the reaper relies on to detect a lost worker.
    """

    INTERVAL = 2.0  # seconds; lowered in tests

    def __init__(self, scan_id: int):
        self.scan_id = scan_id
        self._flag = False
        self._last = 0.0
        self._run_dir: Path | None = None

    def _live_usage(self) -> tuple[float | None, int | None]:
        if self._run_dir is None:  # Strix creates its run directory a little after it starts
            self._run_dir = find_run_dir(scan_dir(self.scan_id))
        return read_live_usage(self._run_dir)

    def __call__(self) -> bool:
        now = time.monotonic()
        if self._flag or now - self._last < self.INTERVAL:
            return self._flag
        self._last = now
        try:
            with SessionLocal() as db:
                status = db.execute(select(Scan.status).where(Scan.id == self.scan_id)).scalar_one_or_none()
                # The same poll carries the live spend: Strix rewrites run.json after every model cycle.
                values: dict = {"heartbeat_at": _utcnow()}
                cost, tokens = self._live_usage()
                if cost is not None:
                    values["cost"] = cost
                if tokens is not None:
                    values["total_tokens"] = tokens
                db.execute(update(Scan).where(Scan.id == self.scan_id).values(**values))
                db.commit()
        except Exception as exc:  # transient DB trouble must not kill a running scan
            log.warning("cancel/heartbeat probe failed for scan %s: %s", self.scan_id, exc)
            return self._flag
        self._flag = status is None or status in (ScanStatus.cancelling, ScanStatus.cancelled)
        return self._flag


def _finalize(scan_id: int, status: ScanStatus, detail: str | None, result: RunResult, parsed: ParsedRun) -> None:
    with SessionLocal() as db:
        scan = db.get(Scan, scan_id)
        if scan is None:
            return
        # Only replace the findings when the run directory was really read: a scan that is continued in place already
        # holds its results, and a run that could not be parsed must not wipe them.
        if parsed.run_name is not None:
            db.execute(delete(Finding).where(Finding.scan_id == scan_id))
        for f in parsed.findings:
            db.add(
                Finding(
                    scan_id=scan_id,
                    vuln_id=f.vuln_id,
                    title=f.title,
                    severity=f.severity,
                    target=f.target,
                    endpoint=f.endpoint,
                    method=f.method,
                    cve=f.cve,
                    cwe=f.cwe,
                    cvss=f.cvss,
                    found_at=f.found_at,
                    data=f.data,
                )
            )
        scan.status = status
        scan.status_detail = detail
        scan.exit_code = result.exit_code
        scan.run_name = parsed.run_name or scan.run_name
        scan.run_status = parsed.run_status if parsed.run_name is not None else scan.run_status
        # cumulative across resumes: keep what is known if the final numbers could not be read
        scan.cost = parsed.cost if parsed.cost is not None else scan.cost
        scan.total_tokens = parsed.total_tokens if parsed.total_tokens is not None else scan.total_tokens
        scan.finished_at = _utcnow()
        db.commit()


@celery.task(name="oreshnik.scan_run")
def scan_run(scan_id: int) -> str:
    snap = _claim(scan_id)
    if snap is None:
        return "skipped"

    work_dir = scan_dir(scan_id)
    writer = LogWriter(scan_id)
    writer.add_secrets([snap.env.get("LLM_API_KEY", "")])
    # Mirrors what the Strix agents do (commands, messages, sub-agents) into activity.jsonl for the panel.
    tailer = ActivityTailer(scan_id, secrets=[snap.env.get("LLM_API_KEY", "")])
    result = RunResult()
    early_error: str | None = None
    parsed = ParsedRun()
    run_json_before: int | None = None
    try:
        if snap.runner_kind is None:
            raise _Preflight("Раннер был удалён до запуска скана")
        try:
            engine = get_runner(snap.runner_kind)
        except NotImplementedError as exc:
            raise _Preflight(str(exc))
        if snap.runner_kind != RunnerKind.mock:
            if snap.env_error:
                raise _Preflight(snap.env_error)
            if not snap.has_profile:
                raise _Preflight("Для реального запуска нужен LLM-профиль (не выбран или удалён)")

        if snap.resume_run and snap.resume_count > 0:
            # Continuing THIS scan: its own run directory is already here, nothing to copy. The log and the activity
            # feed carry on after what they already hold.
            try:
                check_resumable(scan_id, snap.resume_run)
            except ResumeError as exc:
                raise _Preflight(str(exc))
            run_dir = work_dir / "strix_runs" / snap.resume_run
            writer.write(f"[oreshnik] ── Продолжение скана (раз: {snap.resume_count}): прогон {snap.resume_run} ──")
            tailer.continue_in_place(run_dir)
            run_json_before = (run_dir / "run.json").stat().st_mtime_ns
        elif snap.resume_run:
            # A continuation as a NEW scan: give Strix the parent's run directory (a copy) and start the activity feed
            # after the history that is already in it.
            if snap.parent_scan_id is None:
                raise _Preflight("Родительский скан был удалён — продолжить нельзя")
            try:
                staged = stage_run(snap.parent_scan_id, snap.resume_run, work_dir)
            except ResumeError as exc:
                raise _Preflight(str(exc))
            writer.write(f"[oreshnik] Продолжение скана #{snap.parent_scan_id}: прогон {snap.resume_run} скопирован в рабочую папку")
            tailer.skip_existing(staged)
            run_json_before = (staged / "run.json").stat().st_mtime_ns

        timeout = settings.scan_timeout_minutes * 60 if settings.scan_timeout_minutes > 0 else None
        spec = ScanSpec(
            scan_id=scan_id,
            targets=snap.targets,
            scan_mode=snap.scan_mode,
            instruction=snap.instruction,
            max_budget=snap.max_budget,
            work_dir=work_dir,
            run_label=run_label(scan_id),
            timeout_seconds=timeout,
            resume_run=snap.resume_run,
            env=snap.env,
        )
        tailer.start()
        result = engine.run(spec, writer.write, _CancelProbe(scan_id))
    except _Preflight as exc:
        early_error = str(exc)
    except Exception as exc:
        log.exception("scan %s crashed in the engine", scan_id)
        early_error = f"Внутренняя ошибка движка ({exc.__class__.__name__})"
    finally:
        tailer.stop()  # final flush, before the scan can be reported as finished
        try:
            parsed = parse_run_dir(find_run_dir(work_dir))
        except Exception:
            log.exception("scan %s: could not parse run artifacts", scan_id)
        if early_error:
            writer.write(f"[oreshnik] {early_error}")
        for warning in parsed.warnings:
            writer.write(f"[oreshnik] {warning}")
        last_line = writer.last_line
        writer.close()

    # A continued run starts out holding the parent's findings; only trust them as THIS run's result if
    # Strix really wrote to the run (it rewrites run.json when it starts and when it finishes).
    results_are_fresh = True
    if run_json_before is not None:
        current = find_run_dir(work_dir)
        try:
            results_are_fresh = current is not None and (current / "run.json").stat().st_mtime_ns != run_json_before
        except OSError:
            results_are_fresh = False

    status, detail = decide(result, parsed, early_error, settings.scan_timeout_minutes, last_line, results_are_fresh)
    _finalize(scan_id, status, detail, result, parsed)
    return status.value


@celery.task(name="oreshnik.reap_stale_scans")
def reap_stale_scans() -> int:
    """Fail scans whose worker vanished (no heartbeat), and remove their leftover sandboxes."""
    cutoff = _utcnow() - timedelta(seconds=settings.scan_stale_seconds)
    reaped: list[int] = []
    with SessionLocal() as db:
        stale = db.scalars(
            select(Scan).where(
                Scan.status.in_((ScanStatus.running, ScanStatus.cancelling)),
                func.coalesce(Scan.heartbeat_at, Scan.started_at, Scan.created_at) < cutoff,
            )
        ).all()
        for scan in stale:
            was_cancelling = scan.status == ScanStatus.cancelling
            scan.status = ScanStatus.cancelled if was_cancelling else ScanStatus.failed
            scan.status_detail = (
                "Остановлен (воркер не отвечал)" if was_cancelling else "Воркер потерян: нет heartbeat, скан прерван"
            )
            scan.finished_at = _utcnow()
            reaped.append(scan.id)
        db.commit()
    for scan_id in reaped:
        log.warning("reaped stale scan %s", scan_id)
        cleanup_containers(run_label(scan_id))
    return len(reaped)


@celery.task(name="oreshnik.run_due_schedules")
def run_due_schedules() -> int:
    """Start every schedule whose time has come. Runs every 30 s from celery beat.

    Each due schedule is *claimed* with a compare-and-swap on `next_run_at` before anything starts, so two scheduler
    processes (or an overlapping tick) can never start the same firing twice. The next time is computed from now, which
    means firings missed while beat was down are not replayed as a burst of scans.
    """
    now = _utcnow()
    with SessionLocal() as db:
        due = db.execute(
            select(Schedule.id, Schedule.cron, Schedule.timezone, Schedule.next_run_at)
            .where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
            .order_by(Schedule.next_run_at)
        ).all()
    started = 0
    for schedule_id, cron, tz_name, expected in due:
        try:
            following = next_fire(cron, tz_name, now)
        except Exception:
            log.exception("schedule %s has an unusable cron/time zone; skipping", schedule_id)
            continue
        with SessionLocal() as db:
            claimed = db.execute(
                update(Schedule)
                .where(Schedule.id == schedule_id, Schedule.enabled.is_(True), Schedule.next_run_at == expected)
                .values(next_run_at=following)
            )
            db.commit()
            if claimed.rowcount == 0:
                continue
            if fire_scheduled(db, schedule_id, lambda scan_id: scan_run.delay(scan_id), now) == "started":
                started += 1
    return started
