import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.engine import RunResult, ScanSpec, get_runner
from app.engine.docker_cleanup import cleanup_containers, run_label
from app.models.runner import Runner, RunnerKind, RunnerStatus
from app.models.scan import Finding, Scan, ScanStatus
from app.services.activity import ActivityTailer
from app.services.artifacts import ParsedRun, parse_run_dir
from app.services.llm import build_strix_env
from app.services.runner_health import check_runner
from app.services.scan_files import LogWriter, find_run_dir, scan_dir
from app.services.scan_outcome import decide
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
        return _Snapshot(
            scan_id=scan.id,
            runner_kind=scan.runner.kind if scan.runner else None,
            targets=list(scan.targets),
            scan_mode=scan.scan_mode.value,
            instruction=scan.instruction,
            max_budget=scan.max_budget,
            has_profile=scan.llm_profile is not None,
            env=env,
            env_error=env_error,
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

    def __call__(self) -> bool:
        now = time.monotonic()
        if self._flag or now - self._last < self.INTERVAL:
            return self._flag
        self._last = now
        try:
            with SessionLocal() as db:
                status = db.execute(select(Scan.status).where(Scan.id == self.scan_id)).scalar_one_or_none()
                db.execute(update(Scan).where(Scan.id == self.scan_id).values(heartbeat_at=_utcnow()))
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
        scan.run_name = parsed.run_name
        scan.run_status = parsed.run_status
        scan.cost = parsed.cost
        scan.total_tokens = parsed.total_tokens
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

    status, detail = decide(result, parsed, early_error, settings.scan_timeout_minutes, last_line)
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
