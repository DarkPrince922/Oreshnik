"""Spend accounting.

What a scan itself cost is `cost - cost_base`: Strix restores the usage of the run it continues, so the `cost` of a
continuation is cumulative and the parent's share must not be counted twice. A scan is booked on the day it finished
(its start, or creation, if it has not finished yet) — in UTC — so a scan that is still running already shows its spend
so far.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.db import get_db
from app.models.scan import ACTIVE_STATUSES, Scan
from app.models.user import Role, User
from app.schemas.costs import CostDay, CostPeriods, CostScan, CostSummary, CostUser, RunningNow

router = APIRouter(prefix="/api/costs", tags=["costs"])

OWN_COST = func.greatest(func.coalesce(Scan.cost, 0.0) - func.coalesce(Scan.cost_base, 0.0), 0.0)
BOOKED_AT = func.coalesce(Scan.finished_at, Scan.started_at, Scan.created_at)


def _r(value) -> float:
    return round(float(value or 0.0), 4)


def _utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@router.get("", response_model=CostSummary)
def cost_summary(
    days: int = Query(default=30, ge=1, le=365),
    user_id: int | None = Query(default=None, description="admin only: limit to one user"),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> CostSummary:
    """Spend by period, day, user and scan. Operators see their own; admins see everyone (or one user)."""
    if user.role == Role.operator:
        scope = Scan.user_id == user.id
    elif user_id is not None:
        scope = Scan.user_id == user_id
    else:
        scope = true()

    today = datetime.now(timezone.utc).date()
    first_day = today - timedelta(days=days - 1)
    window_start = _utc_midnight(first_day)
    week_start = _utc_midnight(today - timedelta(days=6))
    month_start = _utc_midnight(today.replace(day=1))

    def total(*conditions) -> float:
        return _r(db.scalar(select(func.coalesce(func.sum(OWN_COST), 0.0)).where(scope, *conditions)))

    periods = CostPeriods(
        today=total(BOOKED_AT >= _utc_midnight(today)),
        week=total(BOOKED_AT >= week_start),
        month=total(BOOKED_AT >= month_start),
        window=total(BOOKED_AT >= window_start),
        all_time=total(),
    )

    running_count, running_cost = db.execute(
        select(func.count(), func.coalesce(func.sum(OWN_COST), 0.0)).where(scope, Scan.status.in_(ACTIVE_STATUSES))
    ).one()

    day = func.date(func.timezone("UTC", BOOKED_AT))
    rows = db.execute(
        select(day, func.coalesce(func.sum(OWN_COST), 0.0), func.count(), func.coalesce(func.sum(Scan.total_tokens), 0))
        .where(scope, BOOKED_AT >= window_start)
        .group_by(day)
    ).all()
    by_day = {d: (c, n) for d, c, n, _ in rows}
    tokens = int(sum(t for *_, t in rows))
    per_day = [
        CostDay(date=d, cost=_r(by_day.get(d, (0, 0))[0]), scans=by_day.get(d, (0, 0))[1])
        for d in (first_day + timedelta(days=i) for i in range(days))
    ]

    per_user = [
        CostUser(user_id=uid, email=email, cost=_r(cost), scans=n)
        for uid, email, cost, n in db.execute(
            select(Scan.user_id, User.email, func.coalesce(func.sum(OWN_COST), 0.0), func.count())
            .join(User, User.id == Scan.user_id)
            .where(scope, BOOKED_AT >= window_start)
            .group_by(Scan.user_id, User.email)
            .order_by(func.sum(OWN_COST).desc())
        )
    ]

    top_scans = [
        CostScan(id=sid, target=(targets[0] if targets else "—"), owner_email=email, status=st.value, cost=_r(cost))
        for sid, targets, email, st, cost in db.execute(
            select(Scan.id, Scan.targets, User.email, Scan.status, OWN_COST)
            .join(User, User.id == Scan.user_id)
            .where(scope, BOOKED_AT >= window_start, OWN_COST > 0)
            .order_by(OWN_COST.desc(), Scan.id.desc())
            .limit(10)
        )
    ]

    return CostSummary(
        days=days,
        periods=periods,
        running=RunningNow(count=running_count, cost=_r(running_cost)),
        tokens=tokens,
        per_day=per_day,
        per_user=per_user,
        top_scans=top_scans,
    )
