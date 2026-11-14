import hashlib
import logging
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.engine import RunResult, ScanSpec, get_runner
from app.engine.docker_cleanup import cleanup_containers, run_label
from app.models.llm_profile import LLMProfile
from app.models.ops import OpsHostStatus, OpsRun, OpsRunHost, OpsRunStatus
from app.models.runner import Runner, RunnerKind, RunnerStatus
from app.models.scan import Finding, Scan, ScanStatus
from app.models.schedule import Schedule
from app.services import ops
from app.services.activity import ActivityTailer
from app.services.artifacts import ParsedRun, parse_run_dir, read_live_usage
from app.services.llm import build_strix_env
from app.services.resume import ResumeError, check_resumable, stage_run
from app.services.runner_health import check_runner
from app.services.ssh import SshError, SshTarget
from app.services.ssh_runner import check_ssh_runner, is_provisioning, target_for
from app.services import provision
from app.engine.ssh import cleanup_remote
from app.services.scan_files import LogWriter, find_run_dir, scan_dir
from app.services.scan_outcome import decide
from app.services.schedule import fire_scheduled, next_fire
from app.worker.celery_app import celery

log = logging.getLogger("oreshnik.worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _run_json_token(path: Path) -> str:
    """What run.json looked like, to tell later whether Strix really wrote to it. Content, not modification time: a run
    fetched back from a remote host is a new file even when nothing in it changed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── runner health ────────────────────────────────────────────────────────


def _check_and_store(db, runner: Runner) -> dict:
    if runner.kind == RunnerKind.ssh:
        if is_provisioning(runner):  # the installation owns the status until it ends
            return {"status": runner.status.value, "detail": runner.status_detail}
        result = check_ssh_runner(runner)
        status, detail = result.status, result.detail
        if result.pending_host_key is not None:
            runner.ssh_host_key_pending = result.pending_host_key
        elif result.status == RunnerStatus.online:
            runner.ssh_host_key_pending = None
    else:
        status, detail = check_runner(runner)
    runner.status = status
    runner.status_detail = detail
    if status == RunnerStatus.online:
        runner.last_seen_at = _utcnow()
    db.commit()
    return {"status": status.value, "detail": detail}


@celery.task(name="oreshnik.runner_check")
def runner_check(runner_id: int) -> dict:
    """Health-check a runner from the worker's point of view and persist the result."""
    with SessionLocal() as db:
        runner = db.get(Runner, runner_id)
        if runner is None:
            return {"status": "error", "detail": "runner not found"}
        return _check_and_store(db, runner)


@celery.task(name="oreshnik.runner_provision")
def runner_provision(runner_id: int) -> dict:
    """Install what an SSH runner is missing (Docker, Strix, …), logging every step into the runner's `provision_log`."""

    def save(text: str) -> None:
        with SessionLocal() as db:
            db.execute(update(Runner).where(Runner.id == runner_id).values(provision_log=text))
            db.commit()

    buffer = provision.LogBuffer(save)
    with SessionLocal() as db:
        runner = db.get(Runner, runner_id)
        if runner is None or runner.kind != RunnerKind.ssh:
            return {"status": "error", "detail": "runner not found"}
        db.expunge(runner)  # do not hold a transaction open for the minutes the installation takes
    try:
        outcome = provision.run_provision(runner, buffer, provision.default_commands())
        status, detail = outcome.status, outcome.detail
    except Exception:
        log.exception("provisioning of runner %s crashed", runner_id)
        buffer("✗ Внутренняя ошибка установки")
        status, detail = RunnerStatus.error, "Установка прервана: внутренняя ошибка"
    buffer.flush()
    with SessionLocal() as db:
        row = db.get(Runner, runner_id)
        if row is not None:
            row.status, row.status_detail = status, detail
            row.provision_started_at = None
            if status == RunnerStatus.online:
                row.last_seen_at = _utcnow()
            db.commit()
    return {"status": status.value, "detail": detail}


@celery.task(name="oreshnik.check_ssh_runners")
def check_ssh_runners() -> int:
    """Periodic: keep the status of the SSH runners honest ("online" must not be a week-old fact). Hosts whose key is not
    confirmed yet are left alone — nobody asked for them to be contacted beyond the first look."""
    with SessionLocal() as db:
        ids = db.scalars(
            select(Runner.id)
            .where(
                Runner.kind == RunnerKind.ssh,
                Runner.ssh_host_key.is_not(None),
                # A host that rejected the login (or changed its key) is not asked again every two minutes: a wrong
                # password repeated that way gets the panel locked out by fail2ban and the like. An administrator's
                # "Проверить" — or fixing the credentials — starts it again.
                Runner.status != RunnerStatus.error,
            )
            .order_by(Runner.id)
        ).all()
    done = 0
    for runner_id in ids:
        try:
            with SessionLocal() as db:
                runner = db.get(Runner, runner_id)
                if runner is not None:
                    _check_and_store(db, runner)
                    done += 1
        except Exception:
            log.exception("periodic check of runner %s failed", runner_id)
    return done


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
    # Where an SSH scan runs (the key is decrypted here, in the worker process only), or why that could not be prepared.
    ssh: SshTarget | None = None
    ssh_error: str | None = None
    # Continuing THIS scan (not a copy of another): which time it is, counting from 1; 0 for a first run or a fork.
    resume_count: int = 0


# Serializes "count what runs, then start one" between workers, so two of them cannot both take the last place.
_SLOT_LOCK = 7101
_WAIT_SECONDS = 5


def _claim(scan_id: int) -> "_Snapshot | str | None":
    """Atomically move queued -> running; None if the scan is gone or was cancelled meanwhile, "busy" while the
    limit of scans running at once (`scan_concurrency`, changeable in «Настройки») is reached — the scan stays queued."""
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SLOT_LOCK})  # released by the commit below
        if db.scalar(select(Scan.status).where(Scan.id == scan_id)) != ScanStatus.queued:
            return None
        running = db.scalar(select(func.count()).select_from(Scan).where(Scan.status.in_((ScanStatus.running, ScanStatus.cancelling))))
        if running >= settings.scan_concurrency:
            return "busy"
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
        ssh_target: SshTarget | None = None
        ssh_error = None
        if scan.runner is not None and scan.runner.kind == RunnerKind.ssh:
            try:
                ssh_target = target_for(scan.runner)
            except SshError as exc:
                ssh_error = exc.message
            except Exception as exc:
                ssh_error = f"Не удалось подготовить SSH-подключение ({exc.__class__.__name__})"
        if scan.llm_profile is not None:
            try:
                env = build_strix_env(scan.llm_profile, scan.llm_model)  # decrypts the key; only lives in this process
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
            ssh=ssh_target,
            ssh_error=ssh_error,
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
    if snap == "busy":
        # No free place: try again shortly, the scan stays queued meanwhile (cancelling it makes the next try a no-op).
        # Places are not handed out strictly in the order of arrival.
        if not celery.conf.task_always_eager:
            scan_run.apply_async((scan_id,), countdown=_WAIT_SECONDS)
        return "waiting"
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
    run_json_before: str | None = None
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
        if snap.runner_kind == RunnerKind.ssh:
            if snap.ssh_error:
                raise _Preflight(snap.ssh_error)
            if snap.ssh is None or not snap.ssh.host_key:
                raise _Preflight("Ключ хоста раннера не подтверждён — подтвердите отпечаток на странице «Раннеры»")

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
            run_json_before = _run_json_token(run_dir / "run.json")
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
            run_json_before = _run_json_token(staged / "run.json")

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
            ssh=snap.ssh,
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
        last_line = writer.last_message or writer.last_line
        writer.close()

    # A continued run starts out holding the parent's findings; only trust them as THIS run's result if
    # Strix really wrote to the run (it rewrites run.json when it starts and when it finishes).
    results_are_fresh = True
    if run_json_before is not None:
        current = find_run_dir(work_dir)
        try:
            results_are_fresh = current is not None and _run_json_token(current / "run.json") != run_json_before
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
    remote_cleanups: list[tuple[int, SshTarget]] = []
    with SessionLocal() as db:
        stale = db.scalars(
            select(Scan).where(
                Scan.status.in_((ScanStatus.running, ScanStatus.cancelling)),
                func.coalesce(Scan.heartbeat_at, Scan.started_at, Scan.created_at) < cutoff,
            )
        ).all()
        for scan in stale:
            if scan.runner is not None and scan.runner.kind == RunnerKind.ssh:
                try:
                    remote_cleanups.append((scan.id, target_for(scan.runner)))
                except Exception:
                    log.warning("scan %s: cannot prepare remote cleanup", scan.id)
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
    for scan_id, target in remote_cleanups:  # the worker that ran these is gone: Strix may still be running on the host
        cleanup_remote(target, scan_id, run_label(scan_id))
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


@celery.task(name="oreshnik.ops_run")
def ops_run(ops_run_id: int) -> str:
    """Draft the script, guard it, then run it host by host. Never raises: every outcome is written to the row."""
    with SessionLocal() as db:
        run = db.get(OpsRun, ops_run_id)
        if run is None:
            return "not_found"
        profile = db.get(LLMProfile, run.llm_profile_id) if run.llm_profile_id else None
        hosts = list(db.scalars(select(OpsRunHost).where(OpsRunHost.ops_run_id == ops_run_id).order_by(OpsRunHost.id)))
        runner_ids = {h.id: h.runner_id for h in hosts}
        runner_names = [h.runner_name for h in hosts]
        db.expunge_all()

    if profile is None:
        _ops_finish(ops_run_id, OpsRunStatus.failed, script_error="LLM-профиль удалён")
        _ops_fail_all_hosts(ops_run_id, "скрипт не был сгенерирован: LLM-профиль удалён")
        return "no_profile"

    try:
        script = ops.draft_script(profile, run.goal, runner_names, run.llm_model)
    except ops.OpsError as exc:
        _ops_finish(ops_run_id, OpsRunStatus.failed, script_error=str(exc))
        _ops_fail_all_hosts(ops_run_id, str(exc))
        return "draft_failed"

    with SessionLocal() as db:
        db.execute(update(OpsRun).where(OpsRun.id == ops_run_id).values(script=script))
        db.commit()

    blocked = ops.looks_catastrophic(script)
    if blocked:
        _ops_finish(ops_run_id, OpsRunStatus.failed, blocked_reason=blocked)
        _ops_fail_all_hosts(ops_run_id, f"выполнение остановлено: {blocked}", status=OpsHostStatus.blocked)
        return "blocked"

    with SessionLocal() as db:
        db.execute(update(OpsRun).where(OpsRun.id == ops_run_id).values(status=OpsRunStatus.running, started_at=_utcnow()))
        db.commit()

    for host in hosts:
        with SessionLocal() as db:
            cancelled = bool(db.scalar(select(OpsRun.cancel_requested).where(OpsRun.id == ops_run_id)))
        if cancelled:
            with SessionLocal() as db:
                db.execute(
                    update(OpsRunHost)
                    .where(OpsRunHost.ops_run_id == ops_run_id, OpsRunHost.status == OpsHostStatus.queued)
                    .values(status=OpsHostStatus.cancelled)
                )
                db.commit()
            break

        with SessionLocal() as db:
            db.execute(update(OpsRunHost).where(OpsRunHost.id == host.id).values(status=OpsHostStatus.running, started_at=_utcnow()))
            db.commit()
            runner = db.get(Runner, runner_ids.get(host.id)) if runner_ids.get(host.id) else None
            if runner is not None:
                db.expunge(runner)  # do not hold the session open for the minutes the script may run

        if runner is None:
            status, exit_code, output = "unreachable", None, "Раннер удалён"
        else:
            status, exit_code, output = ops.run_on_host(runner, script, timeout=settings.ops_host_timeout_seconds)

        with SessionLocal() as db:
            db.execute(
                update(OpsRunHost)
                .where(OpsRunHost.id == host.id)
                .values(status=OpsHostStatus(status), exit_code=exit_code, output=output, finished_at=_utcnow())
            )
            db.commit()

    with SessionLocal() as db:
        db.execute(update(OpsRun).where(OpsRun.id == ops_run_id).values(status=OpsRunStatus.done, finished_at=_utcnow()))
        db.commit()
    return "done"


def _ops_finish(ops_run_id: int, status: OpsRunStatus, **fields: object) -> None:
    with SessionLocal() as db:
        db.execute(update(OpsRun).where(OpsRun.id == ops_run_id).values(status=status, finished_at=_utcnow(), **fields))
        db.commit()


def _ops_fail_all_hosts(ops_run_id: int, note: str, *, status: OpsHostStatus = OpsHostStatus.failed) -> None:
    with SessionLocal() as db:
        db.execute(
            update(OpsRunHost)
            .where(OpsRunHost.ops_run_id == ops_run_id, OpsRunHost.status == OpsHostStatus.queued)
            .values(status=status, output=note, finished_at=_utcnow())
        )
        db.commit()


# ── Агент ────────────────────────────────────────────────────────────────


@celery.task(name="oreshnik.agent_turn")
def agent_turn(session_id: int, prompt: str) -> None:
    """One message to the agent: stream the isolated container's events into the session's history."""
    from app.services import agent as agent_service

    agent_service.run_turn(SessionLocal, session_id, prompt)


@celery.task(name="oreshnik.reap_stale_agent_turns")
def reap_stale_agent_turns() -> int:
    """A turn whose worker vanished (no event for `agent_stale_seconds`) is given up on, so the chat is usable again."""
    from app.models.agent import AgentSession, AgentSessionStatus
    from app.services import agent as agent_service

    cutoff = _utcnow() - timedelta(seconds=settings.agent_stale_seconds)
    reaped: list[str] = []
    with SessionLocal() as db:
        stale = db.scalars(
            select(AgentSession).where(
                AgentSession.status == AgentSessionStatus.running,
                func.coalesce(AgentSession.heartbeat_at, AgentSession.updated_at) < cutoff,
            )
        ).all()
        for session in stale:
            message = "Воркер потерян: ход прерван"
            agent_service.append_event(db, session.id, "turn.error", {"message": message})
            session.status = AgentSessionStatus.idle
            session.last_error = message
            session.cancel_requested = False
            reaped.append(session.key)
        db.commit()
    for key in reaped:
        agent_service.stop_turn(key)
    return len(reaped)
