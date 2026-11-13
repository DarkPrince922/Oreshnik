"""Cron handling and the rules that stop a schedule from silently burning money."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models import RunnerKind
from app.services.schedule import (
    ScheduleError,
    check_definition,
    get_zone,
    min_gap,
    next_fire,
    normalize_cron,
    upcoming,
)

UTC = timezone.utc


@pytest.mark.parametrize("raw,expected", [
    ("0 3 * * *", "0 3 * * *"),
    ("  */15   *  * * *  ", "*/15 * * * *"),
    ("0 9 * * 1-5", "0 9 * * 1-5"),
    ("0 0 1,15 * *", "0 0 1,15 * *"),
])
def test_valid_cron_is_normalised(raw, expected):
    assert normalize_cron(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "* * * *", "* * * * * *", "61 * * * *", "* 25 * * *", "@daily", "@hourly",
                                 "a b c d e", "0 3 * * * ; rm -rf /", "0 0 31 2 *x"])
def test_invalid_cron_is_refused(raw):
    with pytest.raises(ScheduleError):
        normalize_cron(raw)


def test_unknown_time_zone_is_refused():
    assert get_zone("Europe/Berlin").key == "Europe/Berlin"
    for bad in ("Mars/Olympus", "", "../../etc/passwd", "Europe"):
        with pytest.raises(ScheduleError):
            get_zone(bad)


def test_firing_times_are_evaluated_in_the_schedules_own_zone_and_returned_in_utc():
    after = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    # 03:00 in Moscow (UTC+3, no DST) is 00:00 UTC
    assert next_fire("0 3 * * *", "Europe/Moscow", after) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert next_fire("0 3 * * *", "UTC", after) == datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    # 23:00 UTC is already 08:00 on 1 Jan in Tokyo, so the next 03:00 JST is on 2 Jan = 18:00 UTC on 1 Jan
    assert next_fire("0 3 * * *", "Asia/Tokyo", after) == datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    assert next_fire("0 3 * * *", "UTC", after).tzinfo is not None


def test_next_fire_is_strictly_after_the_reference_time():
    exactly = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    assert next_fire("0 3 * * *", "UTC", exactly) == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)  # never the same instant twice


def test_the_local_time_stays_put_across_a_dst_change():
    """03:00 Berlin is 02:00 UTC in winter and 01:00 UTC in summer; the clock time is what the user asked for."""
    times = upcoming("0 3 * * *", "Europe/Berlin", 4, datetime(2026, 3, 27, 12, 0, tzinfo=UTC))
    assert [t.isoformat() for t in times] == [
        "2026-03-28T02:00:00+00:00",  # CET (UTC+1)
        "2026-03-29T01:00:00+00:00",  # the night the clocks jump: CEST (UTC+2) already
        "2026-03-30T01:00:00+00:00",
        "2026-03-31T01:00:00+00:00",
    ]


def test_a_time_that_does_not_exist_on_the_dst_day_still_fires_once():
    # 02:30 does not exist in Berlin on 29 March 2026 (02:00 jumps to 03:00).
    times = upcoming("30 2 * * *", "Europe/Berlin", 3, datetime(2026, 3, 28, 12, 0, tzinfo=UTC))
    days = [t.astimezone(get_zone("Europe/Berlin")).date().isoformat() for t in times]
    assert days == ["2026-03-29", "2026-03-30", "2026-03-31"]  # one firing per day, none lost, none doubled


def test_a_time_that_happens_twice_on_the_fall_back_day_fires_once():
    # 02:30 happens twice in Berlin on 25 October 2026 (02:30 CEST, then 02:30 CET an hour later).
    times = upcoming("30 2 * * *", "Europe/Berlin", 4, datetime(2026, 10, 24, 12, 0, tzinfo=UTC))
    berlin = [t.astimezone(get_zone("Europe/Berlin")) for t in times]
    assert [t.date().isoformat() for t in berlin] == ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"]
    assert times[0] == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # the FIRST 02:30 (CEST, UTC+2)


def test_asking_again_right_after_the_first_firing_does_not_return_the_repeat():
    """This is exactly what the scheduler does: it computes the next time from 'now', a few seconds after firing."""
    first = upcoming("30 2 * * *", "Europe/Berlin", 1, datetime(2026, 10, 24, 12, 0, tzinfo=UTC))[0]
    just_after = first + timedelta(seconds=20)
    assert next_fire("30 2 * * *", "Europe/Berlin", just_after) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)
    # (A tick that is 40 minutes late is a different question: from the clock alone the second 02:10 cannot be told
    # apart from "the schedule was just created in the repeated hour", and the second 02:30 must still fire then.)


def test_created_during_the_repeated_hour_before_the_time_it_still_fires_that_day():
    # 02:10 CET (the second 02:10): the first 02:30 is already past, but the second one is still ahead.
    second_0210 = datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
    assert next_fire("30 2 * * *", "Europe/Berlin", second_0210) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize("cron,minutes", [
    ("*/5 * * * *", 5), ("*/15 * * * *", 15), ("0 * * * *", 60), ("0 9,10 * * *", 60),
    ("0 3 * * *", 24 * 60), ("0 3 * * 1", 7 * 24 * 60), ("* * * * *", 1),
])
def test_the_shortest_gap_between_firings(cron, minutes):
    assert min_gap(cron, "UTC") == timedelta(minutes=minutes)


def test_an_uneven_schedule_is_judged_by_its_shortest_gap():
    # 03:00 and 03:05 every day: the 5-minute gap is what matters, not the 24-hour one
    assert min_gap("0,5 3 * * *", "UTC") == timedelta(minutes=5)


# ── the safety rules ────────────────────────────────────────────────────

def test_a_real_runner_needs_a_budget_limit():
    with pytest.raises(ScheduleError, match="лимит бюджета"):
        check_definition("0 3 * * *", "UTC", RunnerKind.local, None)
    check_definition("0 3 * * *", "UTC", RunnerKind.local, 5.0)


def test_the_simulated_runner_needs_no_budget():
    check_definition("0 3 * * *", "UTC", RunnerKind.mock, None)


def test_real_runners_cannot_be_scheduled_too_often(monkeypatch):
    monkeypatch.setattr(settings, "schedule_min_interval_minutes", 15)
    for cron in ("* * * * *", "*/5 * * * *", "*/14 * * * *"):
        with pytest.raises(ScheduleError, match="не меньше 15 мин"):
            check_definition(cron, "UTC", RunnerKind.local, 5.0)
    check_definition("*/15 * * * *", "UTC", RunnerKind.local, 5.0)


def test_the_simulated_runner_may_run_every_minute():
    check_definition("* * * * *", "UTC", RunnerKind.mock, None)
