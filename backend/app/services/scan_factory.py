"""Creating a scan from a request — shared by `POST /api/scans` and by schedules.

All the rules live here once: target classification (filesystem paths are admin-only), runner and LLM-profile
resolution (a profile is personal and mandatory for any runner that really calls a model), and the runner's capacity
(scans per account, targets at once). Errors are raised as `ScanRequestError`, which the API turns into an HTTP response
and the scheduler turns into a note on the schedule.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.llm_profile import LLMProfile
from app.models.runner import Runner, RunnerKind
from app.models.scan import ACTIVE_STATUSES, Scan, ScanMode, ScanStatus
from app.models.user import Role, User
from app.services.ssh_runner import is_configured, is_provisioning
from app.services.targets import TargetKind, classify_all, target_key


class ScanRequestError(Exception):
    """A scan cannot be created. `status_code` mirrors the HTTP status the API answers with."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

    @property
    def transient(self) -> bool:
        """Worth retrying later (a limit or a conflict), as opposed to a configuration that is simply wrong."""
        return self.status_code in (409, 429, 503)


@dataclass
class Resolved:
    targets: list[str]
    runner: Runner
    profile: LLMProfile | None


def check_runner_usable(runner: Runner) -> None:
    """Refuse what can never work, before a scan is created (so a schedule is switched off with a reason instead of
    producing a failed scan every night)."""
    if runner.kind == RunnerKind.ssh:
        if not is_configured(runner):
            raise ScanRequestError(422, "SSH-раннер не настроен: нужны хост, пользователь и ключ или пароль")
        if is_provisioning(runner):
            raise ScanRequestError(409, "На раннере идёт установка — дождитесь её окончания")
        if not runner.ssh_host_key:
            raise ScanRequestError(422, "Ключ хоста SSH-раннера не подтверждён — подтвердите отпечаток на странице «Раннеры»")


def resolve_request(
    db: Session,
    user: User,
    *,
    targets: list[str],
    runner_id: int | None,
    llm_profile_id: int | None,
) -> Resolved:
    """Validate everything except the active-scan limit and return the resolved objects."""
    try:
        classified = classify_all(targets)
    except ValueError as exc:
        raise ScanRequestError(422, f"Некорректная цель: {exc}")
    if user.role != Role.admin and any(kind == TargetKind.path for _, kind in classified):
        raise ScanRequestError(
            403,
            "Локальные пути и файлы может сканировать только администратор. "
            "Укажите URL, домен, IP или git-репозиторий.",
        )

    if runner_id is not None:
        runner = db.get(Runner, runner_id)
        if runner is None:
            raise ScanRequestError(404, "Раннер не найден")
    else:
        try:
            wanted = RunnerKind(settings.default_runner)
        except ValueError:
            wanted = RunnerKind.mock
        runner = db.scalar(select(Runner).where(Runner.kind == wanted).order_by(Runner.id))
        if runner is None:
            raise ScanRequestError(422, "Нет доступного раннера — создайте его в разделе «Раннеры»")
    check_runner_usable(runner)
    if runner.kind == RunnerKind.ssh and any(kind == TargetKind.path for _, kind in classified):
        raise ScanRequestError(
            422,
            "Локальный путь нельзя сканировать на SSH-раннере: он указывал бы на файловую систему самого раннера, "
            "а не панели. Используйте URL, домен, IP или git-репозиторий.",
        )

    profile: LLMProfile | None = None
    if llm_profile_id is not None:
        profile = db.scalar(select(LLMProfile).where(LLMProfile.id == llm_profile_id, LLMProfile.user_id == user.id))
        if profile is None:
            raise ScanRequestError(404, "LLM-профиль не найден")
    elif runner.kind != RunnerKind.mock:
        raise ScanRequestError(422, "Для этого раннера нужно выбрать LLM-профиль")

    return Resolved(targets=[t for t, _ in classified], runner=runner, profile=profile)


def effective_scans_per_user(runner: Runner) -> int:
    """Active scans one account may have on this runner: its own setting, else the panel default."""
    return runner.max_scans_per_user if runner.max_scans_per_user is not None else settings.max_active_scans_per_user


def check_runner_capacity(db: Session, user: User, runner: Runner, targets: list[str]) -> None:
    """Refuse a scan the runner has no room for: too many of this account's scans on it, or too many distinct targets
    (domains / hosts) being scanned on it at once, all accounts together.

    Call it right before the scan is stored and commit straight after. It first takes a row lock on the runner, so two
    requests for the same runner are checked one after the other — otherwise both could count the same free place.
    The lock is released by the commit (or by the rollback when the request fails).

    Nothing here names another account's targets: a refusal only says how many places are taken.
    """
    db.execute(select(Runner.id).where(Runner.id == runner.id).with_for_update())

    limit = effective_scans_per_user(runner)
    active = db.scalar(
        select(func.count())
        .select_from(Scan)
        .where(Scan.user_id == user.id, Scan.runner_id == runner.id, Scan.status.in_(ACTIVE_STATUSES))
    )
    if active >= limit:
        raise ScanRequestError(
            429,
            f"Уже запущено сканов на раннере «{runner.name}»: {active}. Лимит — {limit}; "
            "дождитесь завершения или отмените лишние.",
        )

    if runner.max_targets is None:
        return
    wanted = {target_key(t) for t in targets}
    if len(wanted) > runner.max_targets:
        # Not "later" — this request can never fit, however empty the runner gets.
        raise ScanRequestError(
            422,
            f"В скане целей: {len(wanted)}, а раннер «{runner.name}» сканирует одновременно не больше "
            f"{runner.max_targets}. Разбейте скан на несколько.",
        )
    busy: set[str] = set()
    for scan_targets in db.scalars(
        select(Scan.targets).where(Scan.runner_id == runner.id, Scan.status.in_(ACTIVE_STATUSES))
    ):
        busy.update(target_key(t) for t in scan_targets)
    added = len(wanted - busy)
    if len(busy) + added > runner.max_targets:
        raise ScanRequestError(
            429,
            f"Лимит целей на раннере «{runner.name}»: {runner.max_targets} одновременно (домены и хосты). "
            f"Сейчас занято {len(busy)}, этот скан добавил бы ещё {added}. Дождитесь, пока другие сканы завершатся.",
        )


def create_queued_scan(
    db: Session,
    user: User,
    *,
    targets: list[str],
    scan_mode: ScanMode,
    instruction: str | None,
    max_budget: float | None,
    runner_id: int | None,
    llm_profile_id: int | None,
    schedule_id: int | None = None,
) -> Scan:
    """Validate, apply the per-user limit and persist a `queued` scan. The caller enqueues it."""
    resolved = resolve_request(db, user, targets=targets, runner_id=runner_id, llm_profile_id=llm_profile_id)
    check_runner_capacity(db, user, resolved.runner, resolved.targets)
    scan = Scan(
        user_id=user.id,
        runner_id=resolved.runner.id,
        llm_profile_id=resolved.profile.id if resolved.profile else None,
        targets=resolved.targets,
        scan_mode=scan_mode,
        instruction=instruction,
        max_budget=max_budget,
        status=ScanStatus.queued,
        schedule_id=schedule_id,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)
    return scan
