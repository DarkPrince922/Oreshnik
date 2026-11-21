"""Recurring scans: cron handling and the firing of a schedule.

Cron expressions are the classic five fields (minute hour day-of-month month day-of-week), evaluated in the
schedule's own IANA time zone (so "every day at 03:00 Europe/Moscow" stays 03:00 across DST changes), and stored
as UTC instants in `next_run_at`.

Rules that keep a schedule from silently burning money:
  * a schedule that starts a REAL runner (one that calls an LLM) must carry a budget limit (`--max-budget`);
  * such a schedule cannot fire more often than SCHEDULE_MIN_INTERVAL_MINUTES (the simulated runner is exempt);
  * a firing is skipped while the previous scan of the same schedule is still active (no pile-up);
  * missed firings (beat was down) are not replayed: at most one scan starts per tick, and the next time is
    computed from *now*;
  * a schedule whose owner was disabled, or whose runner/profile was deleted, switches itself off with a reason.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.runner import RunnerKind
from app.models.scan import ACTIVE_STATUSES, Scan, ScanStatus
from app.models.schedule import Schedule
from app.services.scan_factory import ScanRequestError, create_queued_scan


class ScheduleError(ValueError):
    """A schedule definition that is not acceptable; the message is shown to the user."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── cron ─────────────────────────────────────────────────────────────────


def normalize_cron(expr: str) -> str:
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ScheduleError("Cron должен состоять из 5 полей: минута час день месяц день_недели")
    cron = " ".join(fields)
    if not croniter.is_valid(cron):
        raise ScheduleError("Некорректное cron-выражение")
    return cron


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"Неизвестный часовой пояс: {name}")


def _fire_after(cron: str, zone: ZoneInfo, after: datetime) -> datetime:
    """The next firing strictly after `after` (a UTC instant), as a UTC instant.

    The expression is evaluated on the *wall clock* of the schedule's zone, like classic cron: a time that happens twice
    on the night the clocks go back (02:30 in Berlin on 25 Oct 2026) fires once, at its first occurrence; a time that does
    not exist on the night they go forward fires once, just after the jump. (croniter on aware datetimes fires the repeated
    time twice, which would start a daily scan twice an hour apart.)
    """
    wall = croniter(cron, after.astimezone(zone).replace(tzinfo=None))
    for _ in range(8):
        naive = wall.get_next(datetime)
        for fold in (0, 1):  # first occurrence of an ambiguous time, then the second if the first is already past
            candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if candidate > after:
                return candidate
    raise ScheduleError("Не удалось вычислить следующий запуск")


def upcoming(cron: str, tz_name: str, count: int, after: datetime | None = None) -> list[datetime]:
    """The next `count` firing times as UTC instants, strictly after `after` (default: now)."""
    zone = get_zone(tz_name)
    current = after or utcnow()
    times = []
    for _ in range(count):
        current = _fire_after(cron, zone, current)
        times.append(current)
    return times


def next_fire(cron: str, tz_name: str, after: datetime | None = None) -> datetime:
    return upcoming(cron, tz_name, 1, after)[0]


def min_gap(cron: str, tz_name: str) -> timedelta:
    """The shortest gap between consecutive firings among the next dozen."""
    times = upcoming(cron, tz_name, 12)
    return min(b - a for a, b in zip(times, times[1:]))


def check_definition(cron: str, tz_name: str, runner_kind: RunnerKind | None, max_budget: float | None) -> None:
    """The schedule-specific rules (the scan template itself is validated by the scan factory)."""
    real = runner_kind is not None and runner_kind != RunnerKind.mock
    if real and max_budget is None:
        raise ScheduleError(
            "Для расписания с настоящим раннером задайте лимит бюджета: без него каждый запуск может стоить сколько угодно."
        )
    minimum = timedelta(minutes=settings.schedule_min_interval_minutes if real else 1)
    if min_gap(cron, tz_name) < minimum:
        raise ScheduleError(
            f"Слишком частое расписание: для {'настоящего' if real else 'любого'} раннера интервал между запусками "
            f"не меньше {int(minimum.total_seconds() // 60)} мин."
        )


# ── firing ───────────────────────────────────────────────────────────────


def _record(schedule: Schedule, result: str, detail: str | None, scan: Scan | None, now: datetime) -> None:
    schedule.last_run_at = now
    schedule.last_result = result
    schedule.last_detail = detail
    if scan is not None:
        schedule.last_scan_id = scan.id


def start(db: Session, schedule: Schedule, enqueue: Callable[[int], None], now: datetime | None = None) -> Scan:
    """Start one scan from the schedule's template. Raises ScanRequestError if it cannot start.

    The caller decides what a failure means (a manual run reports it, the scheduler records it or switches the
    schedule off) — see `ScanRequestError.transient`.
    """
    now = now or utcnow()
    owner = schedule.user
    if not owner.is_active:
        raise ScanRequestError(403, "Владелец расписания отключён")
    if schedule.runner_id is None:
        # Never fall back to "the default runner": that could silently turn a real scan into a simulated one.
        raise ScanRequestError(404, "Раннер расписания удалён")
    busy = db.scalar(
        select(Scan.id).where(Scan.schedule_id == schedule.id, Scan.status.in_(ACTIVE_STATUSES)).limit(1)
    )
    if busy is not None:
        raise ScanRequestError(409, f"Предыдущий запуск (скан #{busy}) ещё выполняется")

    scan = create_queued_scan(
        db,
        owner,
        targets=list(schedule.targets),
        scan_mode=schedule.scan_mode,
        instruction=schedule.instruction,
        max_budget=schedule.max_budget,
        runner_id=schedule.runner_id,
        llm_profile_id=schedule.llm_profile_id,
        llm_model=schedule.llm_model,
        schedule_id=schedule.id,
    )
    try:
        enqueue(scan.id)
    except Exception:  # broker down
        scan.status = ScanStatus.failed
        scan.status_detail = "Очередь задач недоступна"
        scan.finished_at = now
        _record(schedule, "error", "Очередь задач недоступна", scan, now)
        db.commit()
        raise ScanRequestError(503, "Очередь задач недоступна, скан не запущен")
    _record(schedule, "started", None, scan, now)
    db.commit()
    return scan


def fire_scheduled(db: Session, schedule_id: int, enqueue: Callable[[int], None], now: datetime) -> str:
    """Fire one *due* schedule from the scheduler. Returns "started", "skipped" or "error"."""
    schedule = db.get(Schedule, schedule_id)
    if schedule is None or not schedule.enabled:
        return "skipped"
    try:
        start(db, schedule, enqueue, now)
        return "started"
    except ScanRequestError as exc:
        db.rollback()
        schedule = db.get(Schedule, schedule_id)
        if exc.transient:
            _record(schedule, "skipped", exc.detail, None, now)
            db.commit()
            return "skipped"
        # A configuration problem will not fix itself: switch off, with the reason shown to the owner.
        _record(schedule, "error", exc.detail, None, now)
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.disabled_reason = f"Отключено автоматически: {exc.detail}"
        db.commit()
        return "error"
