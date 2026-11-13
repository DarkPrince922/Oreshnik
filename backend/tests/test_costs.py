"""Spend accounting."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import make_runner, make_scan, make_user
from app.models import Role, ScanStatus

NOW = datetime.now(timezone.utc)


def day(offset: int, hour: int = 12) -> datetime:
    """A moment `offset` UTC days ago (0 = today), at the given hour — always safely inside that UTC day."""
    base = NOW.replace(hour=hour, minute=0, second=0, microsecond=0)
    return base - timedelta(days=offset)


@pytest.fixture
def runner():
    return make_runner()


def spend(user_id, runner_id, cost, *, when=None, base=None, status=ScanStatus.succeeded, tokens=0, targets=None):
    """A scan that cost `cost` in total (and `base` of that was already spent by the scan it continues)."""
    return make_scan(user_id, runner_id, status=status, cost=cost, cost_base=base, total_tokens=tokens,
                     finished_at=when if status not in (ScanStatus.running, ScanStatus.queued) else None,
                     started_at=when, targets=targets)


def summary(client, headers, **params):
    r = client.get("/api/costs", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def me(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["id"]


def test_with_no_scans_everything_is_zero_and_the_calendar_is_complete(client, operator):
    data = summary(client, operator, days=7)
    assert data["periods"] == {"today": 0, "week": 0, "month": 0, "window": 0, "all_time": 0}
    assert data["running"] == {"count": 0, "cost": 0} and data["tokens"] == 0 and data["top_scans"] == []
    dates = [d["date"] for d in data["per_day"]]
    assert len(dates) == 7 and dates[-1] == NOW.date().isoformat()
    assert dates == sorted(dates) and len(set(dates)) == 7  # no gaps, no duplicates, oldest first
    assert all(d["cost"] == 0 and d["scans"] == 0 for d in data["per_day"])


def test_a_continuation_counts_only_its_own_spend(client, operator, runner):
    """Strix's cost is cumulative across a resume. Parent $2.00, continuation $3.50 in total => $1.50 of its own, $3.50 overall."""
    uid = me(client, operator)
    spend(uid, runner.id, 2.0, when=day(0))
    spend(uid, runner.id, 3.5, base=2.0, when=day(0))
    data = summary(client, operator)
    assert data["periods"]["today"] == 3.5  # not 5.5
    by_scan = {s["id"]: s["cost"] for s in data["top_scans"]}
    assert sorted(by_scan.values()) == [1.5, 2.0]


def test_costs_are_booked_on_the_day_the_scan_finished(client, operator, runner):
    uid = me(client, operator)
    spend(uid, runner.id, 1.0, when=day(0))
    spend(uid, runner.id, 2.0, when=day(3))
    spend(uid, runner.id, 4.0, when=day(10))
    spend(uid, runner.id, 8.0, when=day(40))  # outside a 30-day window, still in all-time
    data = summary(client, operator, days=30)
    assert data["periods"]["today"] == 1.0
    assert data["periods"]["week"] == 3.0          # today + 3 days ago
    assert data["periods"]["window"] == 7.0        # 1 + 2 + 4
    assert data["periods"]["all_time"] == 15.0
    per_day = {d["date"]: d for d in data["per_day"]}
    assert per_day[day(3).date().isoformat()]["cost"] == 2.0 and per_day[day(3).date().isoformat()]["scans"] == 1
    assert day(40).date().isoformat() not in per_day
    assert summary(client, operator, days=7)["periods"]["window"] == 3.0


def test_the_month_to_date_period_starts_on_the_first(client, operator, runner):
    uid = me(client, operator)
    first = NOW.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    spend(uid, runner.id, 5.0, when=first)
    spend(uid, runner.id, 7.0, when=first - timedelta(days=1))  # last day of the previous month
    data = summary(client, operator)
    assert data["periods"]["month"] == 5.0   # the previous month's last day is not in it
    assert data["periods"]["all_time"] == 12.0


def test_a_scan_that_is_still_running_shows_what_it_has_cost_so_far(client, operator, runner):
    uid = me(client, operator)
    make_scan(uid, runner.id, status=ScanStatus.running, cost=0.8, started_at=day(0))
    make_scan(uid, runner.id, status=ScanStatus.queued)  # cost is NULL: counts as nothing
    data = summary(client, operator)
    assert data["running"] == {"count": 2, "cost": 0.8}
    assert data["periods"]["today"] == 0.8


def test_null_and_nonsense_costs_never_make_the_totals_negative_or_fail(client, operator, runner):
    uid = me(client, operator)
    spend(uid, runner.id, None, when=day(0))
    spend(uid, runner.id, 1.0, base=5.0, when=day(0))  # cost below its baseline (e.g. a restored run): clamps to 0
    spend(uid, runner.id, 2.0, when=day(0))
    assert summary(client, operator)["periods"]["today"] == 2.0


def test_tokens_are_summed_over_the_window(client, operator, runner):
    uid = me(client, operator)
    spend(uid, runner.id, 1.0, when=day(1), tokens=1500)
    spend(uid, runner.id, 1.0, when=day(2), tokens=500)
    spend(uid, runner.id, 1.0, when=day(60), tokens=999)
    assert summary(client, operator, days=30)["tokens"] == 2000


def test_top_scans_are_the_most_expensive_first_and_skip_free_ones(client, operator, runner):
    uid = me(client, operator)
    ids = {}
    for cost in (0.5, 9.0, 3.0, 0.0):
        ids[cost] = spend(uid, runner.id, cost, when=day(1), targets=[f"https://t{cost}.test"]).id
    top = summary(client, operator)["top_scans"]
    assert [s["cost"] for s in top] == [9.0, 3.0, 0.5]
    assert top[0]["id"] == ids[9.0] and top[0]["target"] == "https://t9.0.test" and top[0]["owner_email"] == "op@example.com"


def test_top_scans_are_limited_to_ten(client, operator, runner):
    uid = me(client, operator)
    for i in range(1, 14):
        spend(uid, runner.id, float(i), when=day(1))
    top = summary(client, operator)["top_scans"]
    assert len(top) == 10 and top[0]["cost"] == 13.0 and top[-1]["cost"] == 4.0


def test_operators_see_only_their_own_spend_admins_see_everyone(client, admin, operator, runner):
    op_id, admin_id = me(client, operator), me(client, admin)
    other = make_user("op2@example.com")
    spend(op_id, runner.id, 1.0, when=day(0))
    spend(other.id, runner.id, 10.0, when=day(0))
    spend(admin_id, runner.id, 100.0, when=day(0))
    assert summary(client, operator)["periods"]["today"] == 1.0
    assert [u["email"] for u in summary(client, operator)["per_user"]] == ["op@example.com"]
    everyone = summary(client, admin)
    assert everyone["periods"]["today"] == 111.0
    assert [(u["email"], u["cost"]) for u in everyone["per_user"]] == [("admin@example.com", 100.0), ("op2@example.com", 10.0), ("op@example.com", 1.0)]
    only_other = summary(client, admin, user_id=other.id)
    assert only_other["periods"]["today"] == 10.0 and [u["email"] for u in only_other["per_user"]] == ["op2@example.com"]


def test_an_operator_cannot_look_at_someone_else_by_passing_user_id(client, admin, operator, runner):
    other = make_user("op2@example.com")
    spend(other.id, runner.id, 10.0, when=day(0))
    assert summary(client, operator, user_id=other.id)["periods"]["today"] == 0  # the parameter is ignored for operators


def test_viewers_and_anonymous_users_get_nothing(client, viewer):
    assert client.get("/api/costs", headers=viewer).status_code == 403
    assert client.get("/api/costs").status_code == 401


@pytest.mark.parametrize("days", [0, -1, 366, 10000])
def test_the_window_is_bounded(client, operator, days):
    assert client.get("/api/costs", headers=operator, params={"days": days}).status_code == 422


def test_a_scan_with_the_per_day_count_includes_free_scans(client, operator, runner):
    uid = me(client, operator)
    spend(uid, runner.id, 0.0, when=day(0))
    spend(uid, runner.id, 2.0, when=day(0))
    today = summary(client, operator)["per_day"][-1]
    assert today["scans"] == 2 and today["cost"] == 2.0
