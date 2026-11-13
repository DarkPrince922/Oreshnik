"""Schedules: the API, and the scheduler task that starts them."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.api import schedules as schedules_api
from app.api import scans as scans_api
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Role, RunnerKind, Scan, ScanStatus, Schedule
from app.services import schedule as schedule_service
from app.worker import tasks
from app.worker.celery_app import celery
from app.worker.tasks import run_due_schedules
from tests.conftest import (
    auth_headers, load_scan, load_schedule, make_profile, make_runner, make_scan, make_schedule, make_user,
)


@pytest.fixture
def mock_runner():
    return make_runner()


@pytest.fixture
def queued_only(monkeypatch):
    """Scans created by a schedule stay `queued` (no worker picks them up), so their state can be driven by hand."""
    monkeypatch.setattr(scans_api.scan_run, "delay", lambda *_a, **_k: None)


def body(**over):
    data = {"name": "nightly", "cron": "0 3 * * *", "targets": ["https://example.com"]}
    data.update(over)
    return data


def create(client, headers, **over):
    return client.post("/api/schedules", headers=headers, json=body(**over))


def me(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["id"]


def scans_of(schedule_id):
    with SessionLocal() as db:
        return list(db.query(Scan).filter(Scan.schedule_id == schedule_id).order_by(Scan.id))


def due(schedule, minutes_ago=1):
    with SessionLocal() as db:
        db.execute(update(Schedule).where(Schedule.id == schedule.id)
                   .values(next_run_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
        db.commit()


# ── create / read ───────────────────────────────────────────────────────

def test_create_resolves_the_default_runner_and_computes_the_next_runs(client, operator, mock_runner):
    r = create(client, operator, timezone="Europe/Moscow", scan_mode="standard", instruction="focus on auth")
    assert r.status_code == 201
    s = r.json()
    assert s["runner_id"] == mock_runner.id and s["runner_kind"] == "mock"  # stored explicitly, not as "the default"
    assert s["enabled"] and s["cron"] == "0 3 * * *" and s["owner_email"] == "op@example.com"
    assert s["scan_mode"] == "standard" and s["instruction"] == "focus on auth"
    assert len(s["next_runs"]) == 3 and s["next_run_at"] == s["next_runs"][0]
    first = datetime.fromisoformat(s["next_run_at"].replace("Z", "+00:00"))
    assert first.hour == 0 and first > datetime.now(timezone.utc)  # 03:00 Moscow = 00:00 UTC
    assert s["last_run_at"] is None and s["last_result"] is None


def test_a_disabled_schedule_has_no_next_run(client, operator, mock_runner):
    s = create(client, operator, enabled=False).json()
    assert s["enabled"] is False and s["next_run_at"] is None and s["next_runs"] == []


@pytest.mark.parametrize("bad,detail", [
    ({"cron": "* * * *"}, "5 полей"),
    ({"cron": "nonsense"}, "5 полей"),
    ({"cron": "99 * * * *"}, "Некорректное"),
    ({"timezone": "Mars/Base"}, "часовой пояс"),
    ({"targets": ["-evil"]}, "Некорректная цель"),
    ({"targets": []}, None),
    ({"name": "   "}, None),
    ({"max_budget": 0}, None),
    ({"scan_mode": "extreme"}, None),
])
def test_invalid_schedules_are_refused(client, operator, mock_runner, bad, detail):
    r = create(client, operator, **bad)
    assert r.status_code == 422
    if detail:
        assert detail in r.text


def test_a_real_runner_needs_a_budget_a_profile_and_a_sane_interval(client, operator):
    local = make_runner(RunnerKind.local, "local")
    profile = make_profile(me(client, operator), name="mine")
    r = create(client, operator, runner_id=local.id, llm_profile_id=profile.id)
    assert r.status_code == 422 and "лимит бюджета" in r.json()["detail"]
    r = create(client, operator, runner_id=local.id, max_budget=3)
    assert r.status_code == 422 and "LLM-профиль" in r.json()["detail"]
    r = create(client, operator, runner_id=local.id, llm_profile_id=profile.id, max_budget=3, cron="*/5 * * * *")
    assert r.status_code == 422 and "не меньше 15 мин" in r.json()["detail"]
    ok = create(client, operator, runner_id=local.id, llm_profile_id=profile.id, max_budget=3, cron="*/15 * * * *")
    assert ok.status_code == 201 and ok.json()["llm_profile_name"] == "mine"


def test_the_simulated_runner_can_run_every_minute_without_a_budget(client, operator, mock_runner):
    assert create(client, operator, cron="* * * * *").status_code == 201


def test_names_are_unique_per_owner_only(client, admin, operator, mock_runner):
    assert create(client, operator).status_code == 201
    assert create(client, operator).status_code == 409
    assert create(client, admin).status_code == 201


def test_other_users_profile_cannot_be_used(client, admin, operator):
    local = make_runner(RunnerKind.local, "local")
    foreign = make_profile(me(client, admin), name="admins")
    r = create(client, operator, runner_id=local.id, llm_profile_id=foreign.id, max_budget=3)
    assert r.status_code == 404


def test_paths_are_admin_only_like_for_scans(client, admin, operator, mock_runner):
    assert create(client, operator, targets=["/etc"]).status_code == 403
    assert create(client, admin, targets=["./project"]).status_code == 201


def test_per_user_limit(client, operator, mock_runner, monkeypatch):
    monkeypatch.setattr(settings, "max_schedules_per_user", 2)
    assert create(client, operator, name="a").status_code == 201
    assert create(client, operator, name="b").status_code == 201
    assert create(client, operator, name="c").status_code == 409


# ── permissions ─────────────────────────────────────────────────────────

def test_permissions_and_visibility(client, admin, operator, viewer, mock_runner):
    mine = create(client, operator).json()["id"]
    theirs = create(client, admin, name="admins").json()["id"]
    for method, path in (("get", "/api/schedules"), ("post", "/api/schedules"), ("get", f"/api/schedules/{mine}"),
                         ("delete", f"/api/schedules/{mine}"), ("post", f"/api/schedules/{mine}/run")):
        assert getattr(client, method)(path, headers=viewer).status_code == 403, path  # the instruction may hold credentials
    assert client.get("/api/schedules").status_code == 401
    assert [s["id"] for s in client.get("/api/schedules", headers=operator).json()] == [mine]
    assert sorted(s["id"] for s in client.get("/api/schedules", headers=admin).json()) == sorted([mine, theirs])
    for call in (client.get, client.delete):
        assert call(f"/api/schedules/{theirs}", headers=operator).status_code == 404
    assert client.post(f"/api/schedules/{theirs}/run", headers=operator).status_code == 404
    assert client.patch(f"/api/schedules/{theirs}", headers=operator, json={"name": "x"}).status_code == 404


# ── preview ─────────────────────────────────────────────────────────────

def test_preview(client, operator, viewer):
    ok = client.get("/api/schedules/preview", params={"cron": "0 9 * * 1-5", "timezone": "Europe/Berlin", "count": 4}, headers=operator).json()
    assert ok["valid"] and len(ok["next_runs"]) == 4 and ok["min_interval_minutes"] == 24 * 60
    bad = client.get("/api/schedules/preview", params={"cron": "x y z"}, headers=operator).json()
    assert bad["valid"] is False and "5 полей" in bad["error"] and bad["next_runs"] == []
    assert client.get("/api/schedules/preview", params={"cron": "0 9 * * *", "timezone": "Nowhere/Land"}, headers=operator).json()["valid"] is False
    assert client.get("/api/schedules/preview", params={"cron": "0 9 * * *"}, headers=viewer).status_code == 403
    assert client.get("/api/schedules/preview", params={"cron": "0 9 * * *"}).status_code == 401


# ── update / delete ─────────────────────────────────────────────────────

def test_update_recomputes_the_next_run_only_when_the_timing_changes(client, operator, mock_runner):
    s = create(client, operator).json()
    renamed = client.patch(f"/api/schedules/{s['id']}", headers=operator, json={"name": "renamed"}).json()
    assert renamed["name"] == "renamed" and renamed["next_run_at"] == s["next_run_at"]  # untouched
    moved = client.patch(f"/api/schedules/{s['id']}", headers=operator, json={"cron": "30 5 * * *", "timezone": "Asia/Tokyo"}).json()
    assert moved["cron"] == "30 5 * * *" and moved["next_run_at"] != s["next_run_at"]
    assert datetime.fromisoformat(moved["next_run_at"].replace("Z", "+00:00")).minute == 30


def test_disabling_and_re_enabling(client, operator, mock_runner):
    s = create(client, operator).json()
    off = client.patch(f"/api/schedules/{s['id']}", headers=operator, json={"enabled": False}).json()
    assert off["enabled"] is False and off["next_run_at"] is None
    with SessionLocal() as db:
        db.execute(update(Schedule).where(Schedule.id == s["id"]).values(disabled_reason="Отключено автоматически: x"))
        db.commit()
    on = client.patch(f"/api/schedules/{s['id']}", headers=operator, json={"enabled": True}).json()
    assert on["enabled"] and on["next_run_at"] and on["disabled_reason"] is None  # the reason is cleared once the owner turns it on


def test_update_validates_like_create(client, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    for bad in ({"cron": "bad"}, {"timezone": "Nope/Zone"}, {"targets": ["ftp://x.com"]}, {"targets": []}):
        assert client.patch(f"/api/schedules/{sid}", headers=operator, json=bad).status_code == 422, bad
    create(client, operator, name="other")
    assert client.patch(f"/api/schedules/{sid}", headers=operator, json={"name": "other"}).status_code == 409


def test_a_budget_cannot_be_removed_from_a_real_schedule(client, operator):
    local = make_runner(RunnerKind.local, "local")
    profile = make_profile(me(client, operator))
    sid = create(client, operator, runner_id=local.id, llm_profile_id=profile.id, max_budget=3).json()["id"]
    r = client.patch(f"/api/schedules/{sid}", headers=operator, json={"max_budget": None})
    assert r.status_code == 422 and "лимит бюджета" in r.json()["detail"]
    assert client.patch(f"/api/schedules/{sid}", headers=operator, json={"max_budget": 9}).json()["max_budget"] == 9


def test_an_admin_cannot_attach_their_own_profile_to_someone_elses_schedule(client, admin, operator):
    local = make_runner(RunnerKind.local, "local")
    mine = make_profile(me(client, operator), name="mine")
    sid = create(client, operator, runner_id=local.id, llm_profile_id=mine.id, max_budget=3).json()["id"]
    admins = make_profile(me(client, admin), name="admins")
    assert client.patch(f"/api/schedules/{sid}", headers=admin, json={"llm_profile_id": admins.id}).status_code == 404
    assert client.patch(f"/api/schedules/{sid}", headers=admin, json={"name": "renamed by admin"}).status_code == 200


def test_a_schedule_whose_runner_was_deleted_must_be_given_a_new_one(client, admin, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    client.delete(f"/api/runners/{mock_runner.id}", headers=admin)
    assert client.get(f"/api/schedules/{sid}", headers=operator).json()["runner_id"] is None
    r = client.patch(f"/api/schedules/{sid}", headers=operator, json={"name": "x"})
    assert r.status_code == 422 and "Раннер расписания удалён" in r.json()["detail"]  # never silently falls back to the default
    fresh = make_runner(name="fresh")
    assert client.patch(f"/api/schedules/{sid}", headers=operator, json={"runner_id": fresh.id}).json()["runner_id"] == fresh.id


def test_delete_keeps_the_scans_it_started(client, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    scan_id = client.post(f"/api/schedules/{sid}/run", headers=operator).json()["scan_id"]
    assert client.delete(f"/api/schedules/{sid}", headers=operator).status_code == 204
    assert client.get(f"/api/schedules/{sid}", headers=operator).status_code == 404
    scan = client.get(f"/api/scans/{scan_id}", headers=operator).json()
    assert scan["schedule_id"] is None and scan["schedule_name"] is None


# ── run now ─────────────────────────────────────────────────────────────

def test_run_now_starts_a_scan_from_the_template(client, operator, mock_runner):
    sid = create(client, operator, scan_mode="standard", instruction="MOCK:clean", targets=["https://a.test", "b.test"]).json()["id"]
    r = client.post(f"/api/schedules/{sid}/run", headers=operator)
    assert r.status_code == 201
    scan = client.get(f"/api/scans/{r.json()['scan_id']}", headers=operator).json()
    assert scan["schedule_id"] == sid and scan["schedule_name"] == "nightly"
    assert scan["targets"] == ["https://a.test", "b.test"] and scan["scan_mode"] == "standard" and scan["instruction"] == "MOCK:clean"
    assert scan["status"] == "succeeded"  # ran inline
    after = client.get(f"/api/schedules/{sid}", headers=operator).json()
    assert after["last_result"] == "started" and after["last_scan_id"] == scan["id"] and after["last_run_at"]
    assert after["next_run_at"] is not None  # a manual run does not shift the timetable


def test_run_now_works_on_a_disabled_schedule(client, operator, mock_runner):
    sid = create(client, operator, enabled=False).json()["id"]
    assert client.post(f"/api/schedules/{sid}/run", headers=operator).status_code == 201
    assert client.get(f"/api/schedules/{sid}", headers=operator).json()["enabled"] is False


def test_run_now_is_refused_while_the_previous_scan_is_still_going(client, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    first = client.post(f"/api/schedules/{sid}/run", headers=operator)
    assert first.status_code == 201
    again = client.post(f"/api/schedules/{sid}/run", headers=operator)
    assert again.status_code == 409 and f"#{first.json()['scan_id']}" in again.json()["detail"]
    client.post(f"/api/scans/{first.json()['scan_id']}/cancel", headers=operator)
    assert client.post(f"/api/schedules/{sid}/run", headers=operator).status_code == 201


def test_run_now_counts_towards_the_active_scan_limit(client, operator, mock_runner, queued_only, monkeypatch):
    sid = create(client, operator).json()["id"]
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    make_scan(me(client, operator), mock_runner.id)
    assert client.post(f"/api/schedules/{sid}/run", headers=operator).status_code == 429


def test_the_scan_list_can_be_filtered_by_schedule(client, operator, mock_runner):
    a = create(client, operator, name="a").json()["id"]
    b = create(client, operator, name="b").json()["id"]
    client.post(f"/api/schedules/{a}/run", headers=operator)
    client.post(f"/api/schedules/{b}/run", headers=operator)
    client.post("/api/scans", headers=operator, json={"targets": ["x.test"]})
    assert client.get("/api/scans", headers=operator).json()["total"] == 3
    only_a = client.get(f"/api/scans?schedule_id={a}", headers=operator).json()
    assert only_a["total"] == 1 and only_a["items"][0]["schedule_name"] == "a"


# ── the scheduler ───────────────────────────────────────────────────────

def test_a_due_schedule_starts_exactly_one_scan_and_moves_its_clock_forward(client, operator, mock_runner):
    user_id = me(client, operator)
    s = make_schedule(user_id, mock_runner.id, cron="0 3 * * *", instruction="MOCK:clean")
    due(s)
    before = datetime.now(timezone.utc)
    assert run_due_schedules() == 1
    scans = scans_of(s.id)
    assert len(scans) == 1 and scans[0].user_id == user_id and scans[0].status == ScanStatus.succeeded
    assert scans[0].instruction == "MOCK:clean" and scans[0].schedule_id == s.id
    after = load_schedule(s.id)
    assert after.next_run_at > before and after.last_result == "started" and after.last_scan_id == scans[0].id
    assert run_due_schedules() == 0  # nothing is due any more
    assert len(scans_of(s.id)) == 1


def test_a_schedule_that_is_not_due_does_not_fire(client, operator, mock_runner):
    s = make_schedule(me(client, operator), mock_runner.id, next_run_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert run_due_schedules() == 0 and scans_of(s.id) == []


def test_a_disabled_schedule_never_fires(client, operator, mock_runner):
    s = make_schedule(me(client, operator), mock_runner.id, enabled=False,
                      next_run_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert run_due_schedules() == 0 and scans_of(s.id) == []


def test_firings_missed_while_the_scheduler_was_down_are_not_replayed(client, operator, mock_runner):
    s = make_schedule(me(client, operator), mock_runner.id, cron="*/15 * * * *",
                      next_run_at=datetime.now(timezone.utc) - timedelta(days=3))
    assert run_due_schedules() == 1
    assert len(scans_of(s.id)) == 1  # one catch-up scan, not 288
    assert load_schedule(s.id).next_run_at > datetime.now(timezone.utc)
    assert run_due_schedules() == 0


def test_a_firing_is_skipped_while_the_previous_scan_is_still_running(client, operator, mock_runner):
    user_id = me(client, operator)
    s = make_schedule(user_id, mock_runner.id)
    make_scan(user_id, mock_runner.id, status=ScanStatus.running, schedule_id=s.id)
    due(s)
    assert run_due_schedules() == 0
    after = load_schedule(s.id)
    assert after.last_result == "skipped" and "ещё выполняется" in after.last_detail
    assert after.enabled and after.next_run_at > datetime.now(timezone.utc)  # tries again at the next time, stays on
    assert len(scans_of(s.id)) == 1


def test_reaching_the_active_scan_limit_skips_but_does_not_switch_off(client, operator, mock_runner, monkeypatch):
    user_id = me(client, operator)
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    make_scan(user_id, mock_runner.id, status=ScanStatus.running)
    s = make_schedule(user_id, mock_runner.id)
    due(s)
    assert run_due_schedules() == 0
    after = load_schedule(s.id)
    assert after.enabled and after.last_result == "skipped" and "Лимит" in after.last_detail


def test_a_schedule_of_a_disabled_user_switches_itself_off_with_a_reason(client, admin, mock_runner):
    ghost = make_user("ghost@example.com", role=Role.operator, active=False)
    s = make_schedule(ghost.id, mock_runner.id)
    due(s)
    assert run_due_schedules() == 0
    after = load_schedule(s.id)
    assert after.enabled is False and after.next_run_at is None
    assert "Владелец расписания отключён" in after.disabled_reason and after.last_result == "error"
    assert scans_of(s.id) == []


def test_a_schedule_whose_runner_was_deleted_switches_itself_off_instead_of_using_the_default(client, operator, mock_runner):
    s = make_schedule(me(client, operator), None)  # runner_id is NULL after the runner was deleted
    due(s)
    assert run_due_schedules() == 0
    after = load_schedule(s.id)
    assert after.enabled is False and "Раннер расписания удалён" in after.disabled_reason
    assert scans_of(s.id) == []  # in particular: no simulated scan was started in its place


def test_a_real_schedule_whose_profile_was_deleted_switches_itself_off(client, operator):
    local = make_runner(RunnerKind.local, "local")
    s = make_schedule(me(client, operator), local.id, profile_id=None, max_budget=3.0)
    due(s)
    assert run_due_schedules() == 0
    after = load_schedule(s.id)
    assert after.enabled is False and "LLM-профиль" in after.disabled_reason


def test_the_owner_can_turn_a_switched_off_schedule_back_on_after_fixing_it(client, operator, mock_runner):
    s = make_schedule(me(client, operator), None)
    due(s)
    run_due_schedules()
    fresh = make_runner(name="fresh")
    r = client.patch(f"/api/schedules/{s.id}", headers=operator, json={"runner_id": fresh.id, "enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] and r.json()["disabled_reason"] is None


def test_two_schedulers_cannot_start_the_same_firing_twice(client, operator, mock_runner, monkeypatch):
    """The scheduler claims a firing with a compare-and-swap on next_run_at. Simulate a second scheduler that claims it
    between our SELECT and our UPDATE."""
    s = make_schedule(me(client, operator), mock_runner.id)
    due(s)
    real = tasks.next_fire

    def rival_claims_first(cron, tz, after):
        with SessionLocal() as db:
            db.execute(update(Schedule).where(Schedule.id == s.id).values(next_run_at=datetime.now(timezone.utc) + timedelta(hours=5)))
            db.commit()
        return real(cron, tz, after)

    monkeypatch.setattr(tasks, "next_fire", rival_claims_first)
    assert run_due_schedules() == 0
    assert scans_of(s.id) == []


def test_concurrent_ticks_start_one_scan(client, operator, mock_runner, queued_only):
    s = make_schedule(me(client, operator), mock_runner.id)
    due(s)
    results = []
    threads = [threading.Thread(target=lambda: results.append(run_due_schedules())) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) == 1 and len(scans_of(s.id)) == 1


def test_the_template_is_copied_into_the_scan(client, operator):
    local = make_runner(RunnerKind.local, "local")
    user_id = me(client, operator)
    profile = make_profile(user_id, name="mine")
    s = make_schedule(user_id, local.id, profile_id=profile.id, max_budget=2.5, targets=["a.test", "https://b.test"],
                      instruction="check logout", tz="Europe/Berlin")
    due(s)
    # leave it queued: a real runner would need Docker
    assert run_due_schedules() == 1
    scan = scans_of(s.id)[0]
    assert (scan.runner_id, scan.llm_profile_id, scan.max_budget) == (local.id, profile.id, 2.5)
    assert scan.targets == ["a.test", "https://b.test"] and scan.instruction == "check logout" and scan.scan_mode.value == "quick"


def test_a_broker_outage_is_recorded_and_does_not_switch_the_schedule_off(client, operator, mock_runner, monkeypatch):
    def boom(*_a, **_k):
        raise ConnectionError("redis down")
    monkeypatch.setattr(tasks.scan_run, "delay", boom)
    s = make_schedule(me(client, operator), mock_runner.id)
    due(s)
    assert run_due_schedules() == 0
    after = load_schedule(s.id)
    assert after.enabled and after.last_result == "skipped" and "Очередь" in after.last_detail
    assert scans_of(s.id)[0].status == ScanStatus.failed  # the scan that could not be queued is not left hanging


def test_the_scheduler_is_wired_into_celery_beat():
    entry = celery.conf.beat_schedule["run-due-schedules"]
    assert entry["task"] == "oreshnik.run_due_schedules" and 0 < entry["schedule"] <= 60
    assert "oreshnik.run_due_schedules" in celery.tasks
