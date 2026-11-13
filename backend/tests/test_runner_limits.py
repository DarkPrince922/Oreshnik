"""What a runner takes on: scans per account and distinct targets at once, both set per runner."""
import threading
import time

import pytest

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Role, Runner, ScanStatus
from app.models.scan import ScanMode
from app.models.user import User
from app.services import scan_factory
from app.services.scan_factory import ScanRequestError, create_queued_scan
from app.services.targets import target_key
from app.worker.tasks import run_due_schedules
from tests.conftest import load_scan, make_runner, make_scan, make_user
from tests.test_resume_in_place import finished, me, resume
from tests.test_schedules import due, load_schedule
from tests.test_scans_api import create, queued_only  # noqa: F401
from tests.conftest import make_schedule


def set_limits(runner_id: int, **limits) -> None:
    with SessionLocal() as db:
        db.query(Runner).filter(Runner.id == runner_id).update(limits)
        db.commit()


def scan_on(client, headers, runner, *targets):
    return create(client, headers, runner_id=runner.id, targets=list(targets) or ["https://example.com"])


# ── what counts as "a target" ────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, key",
    [
        ("https://App.Example.com:8443/login", "app.example.com"),
        ("app.example.com", "app.example.com"),
        ("APP.example.com:8080/x/y", "app.example.com"),
        ("http://app.example.com./", "app.example.com"),
        ("192.168.1.42", "192.168.1.42"),
        ("http://192.168.1.42:8080/a", "192.168.1.42"),
        ("http://[::1]:8080/", "::1"),
        ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
        ("git://example.com/org/repo.git", "git://example.com/org/repo.git"),
        ("./my-project", "./my-project"),
    ],
)
def test_target_key_is_the_host_a_target_points_at(raw, key):
    assert target_key(raw) == key


def test_the_same_host_in_different_spellings_is_one_target():
    spellings = ["https://app.example.com", "app.example.com:8443", "http://APP.example.com/a?b=1", "app.example.com/x"]
    assert len({target_key(t) for t in spellings}) == 1
    assert target_key("app.example.com") != target_key("www.app.example.com")  # a different host is a different target


# ── scans per account, per runner ────────────────────────────────────────

def test_the_account_limit_belongs_to_the_runner(client, operator, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 2)
    a, b = make_runner(name="a"), make_runner(name="b")
    assert scan_on(client, operator, a).status_code == 201
    assert scan_on(client, operator, a).status_code == 201
    refused = scan_on(client, operator, a)
    assert refused.status_code == 429 and "Лимит — 2" in refused.json()["detail"] and "«a»" in refused.json()["detail"]
    # another runner has its own room: this is the point of setting it per runner
    assert scan_on(client, operator, b).status_code == 201
    assert scan_on(client, operator, b).status_code == 201
    assert scan_on(client, operator, b).status_code == 429


def test_a_runners_own_number_replaces_the_panel_default(client, operator, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    roomy, tight = make_runner(name="roomy"), make_runner(name="tight")
    set_limits(roomy.id, max_scans_per_user=4)
    set_limits(tight.id, max_scans_per_user=1)
    assert [scan_on(client, operator, roomy).status_code for _ in range(5)] == [201, 201, 201, 201, 429]
    assert [scan_on(client, operator, tight).status_code for _ in range(2)] == [201, 429]


def test_a_runner_without_a_number_follows_the_panel_default(client, operator, queued_only, monkeypatch):
    runner = make_runner()
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    assert [scan_on(client, operator, runner).status_code for _ in range(2)] == [201, 429]
    monkeypatch.setattr(settings, "max_active_scans_per_user", 2)
    assert scan_on(client, operator, runner).status_code == 201  # the change applies at once, no runner was edited


def test_another_accounts_scans_do_not_use_up_my_places(client, admin, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_scans_per_user=1)
    assert scan_on(client, admin, runner).status_code == 201
    assert scan_on(client, operator, runner).status_code == 201


def test_finished_scans_free_the_place(client, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_scans_per_user=1)
    first = scan_on(client, operator, runner).json()["id"]
    assert scan_on(client, operator, runner).status_code == 429
    client.post(f"/api/scans/{first}/cancel", headers=operator)
    assert scan_on(client, operator, runner).status_code == 201


# ── distinct targets at once, per runner ─────────────────────────────────

def test_targets_are_counted_across_accounts_and_refusals_name_nobody_elses_targets(client, admin, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_targets=2)
    assert scan_on(client, admin, runner, "https://secret-one.example.org").status_code == 201
    assert scan_on(client, admin, runner, "https://secret-two.example.org").status_code == 201
    refused = scan_on(client, operator, runner, "https://mine.example.com")
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert "Лимит целей" in detail and "2" in detail
    assert "secret-one" not in detail and "secret-two" not in detail  # counts, never somebody's targets


def test_a_target_already_being_scanned_does_not_take_another_place(client, admin, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_targets=1)
    assert scan_on(client, admin, runner, "https://app.example.com").status_code == 201
    assert scan_on(client, operator, runner, "app.example.com:8443/other").status_code == 201  # same host
    assert scan_on(client, operator, runner, "https://elsewhere.example.com").status_code == 429


def test_places_come_back_when_the_scans_end(client, admin, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_targets=1)
    first = scan_on(client, admin, runner, "https://one.example.com").json()["id"]
    assert scan_on(client, operator, runner, "https://two.example.com").status_code == 429
    client.post(f"/api/scans/{first}/cancel", headers=admin)
    assert scan_on(client, operator, runner, "https://two.example.com").status_code == 201


def test_one_scan_with_more_targets_than_the_runner_can_ever_hold_is_refused_for_good(client, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_targets=2)
    r = scan_on(client, operator, runner, "https://a.example.com", "https://b.example.com", "https://c.example.com")
    assert r.status_code == 422 and "Разбейте" in r.json()["detail"]  # not 429: waiting would never help
    assert scan_on(client, operator, runner, "https://a.example.com", "https://a.example.com/x").status_code == 201  # two URLs, one host


def test_the_target_limit_is_per_runner(client, operator, queued_only):
    a, b = make_runner(name="a"), make_runner(name="b")
    set_limits(a.id, max_targets=1)
    assert scan_on(client, operator, a, "https://one.example.com").status_code == 201
    assert scan_on(client, operator, a, "https://two.example.com").status_code == 429
    assert scan_on(client, operator, b, "https://two.example.com").status_code == 201  # b has no limit


def test_no_limit_means_no_limit(client, operator, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 50)
    runner = make_runner()
    targets = [f"https://host{i}.example.com" for i in range(20)]
    assert scan_on(client, operator, runner, *targets).status_code == 201


# ── the same rules where a scan comes back to the queue ──────────────────

def test_continuing_a_scan_needs_room_on_the_runner_too(client, admin, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_targets=1)
    parent = finished(client, operator, runner)  # https://example.com
    make_scan(me(client, admin), runner.id, targets=["https://busy.example.org"])  # somebody else is using the only place
    for as_new in (False, True):
        r = resume(client, operator, parent["id"], instruction="more", as_new_scan=as_new)
        assert r.status_code == 429 and "Лимит целей" in r.json()["detail"]
    row = load_scan(parent["id"])
    assert row.status == ScanStatus.succeeded and row.resume_count == 0  # nothing was changed
    # and once the place is free it goes through
    with SessionLocal() as db:
        from app.models import Scan
        db.query(Scan).filter(Scan.targets == ["https://busy.example.org"]).update({"status": ScanStatus.cancelled})
        db.commit()
    assert resume(client, operator, parent["id"], instruction="more").status_code == 200


def test_continuing_counts_against_the_accounts_own_places(client, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_scans_per_user=1)
    parent = finished(client, operator, runner)
    make_scan(me(client, operator), runner.id)  # my one place is taken
    assert resume(client, operator, parent["id"], instruction="x").status_code == 429


def test_a_schedule_that_does_not_fit_is_skipped_not_switched_off(client, operator, queued_only):
    runner = make_runner()
    set_limits(runner.id, max_targets=1)
    make_scan(me(client, operator), runner.id, targets=["https://busy.example.org"], status=ScanStatus.running)
    schedule = make_schedule(me(client, operator), runner.id)
    due(schedule)
    assert run_due_schedules() == 0
    after = load_schedule(schedule.id)
    assert after.enabled and after.last_result == "skipped" and "Лимит" in after.last_detail


# ── two requests for the last place ──────────────────────────────────────

def _race(user_id, runner_id, jobs):
    """Run `jobs` (each a list of targets) at the same moment; return what each got."""
    results: list = []
    start = threading.Barrier(len(jobs))

    def go(targets):
        with SessionLocal() as db:
            user = db.get(User, user_id)
            start.wait()
            try:
                create_queued_scan(
                    db, user, targets=targets, scan_mode=ScanMode.quick, instruction=None, max_budget=None,
                    runner_id=runner_id, llm_profile_id=None,
                )
                results.append("ok")
            except ScanRequestError as exc:
                results.append(exc.status_code)

    threads = [threading.Thread(target=go, args=(t,)) for t in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def _slow_check(monkeypatch):
    """Widen the gap between 'there is room' and 'the scan is stored', which is where an unlocked check goes wrong."""
    real = scan_factory.check_runner_capacity

    def slow(*args, **kwargs):
        real(*args, **kwargs)
        time.sleep(0.15)

    monkeypatch.setattr(scan_factory, "check_runner_capacity", slow)


def test_simultaneous_requests_cannot_exceed_the_accounts_limit(client, operator, monkeypatch):
    runner = make_runner()
    set_limits(runner.id, max_scans_per_user=3)
    _slow_check(monkeypatch)
    results = _race(me(client, operator), runner.id, [["https://example.com"]] * 8)
    assert results.count("ok") == 3 and results.count(429) == 5


def test_simultaneous_requests_cannot_exceed_the_target_limit(client, operator, monkeypatch):
    runner = make_runner()
    set_limits(runner.id, max_targets=2)
    _slow_check(monkeypatch)
    results = _race(me(client, operator), runner.id, [[f"https://host{i}.example.com"] for i in range(6)])
    assert results.count("ok") == 2 and results.count(429) == 4


# ── setting the limits ───────────────────────────────────────────────────

def test_limits_are_set_when_the_runner_is_created_and_shown_to_everybody(client, admin, operator):
    r = client.post("/api/runners", headers=admin, json={
        "name": "box", "kind": "ssh", "ssh_host": "10.0.0.5", "ssh_user": "root", "ssh_password": "pw",
        "max_targets": 3, "max_scans_per_user": 2,
    })
    assert r.status_code == 201
    assert (r.json()["max_targets"], r.json()["max_scans_per_user"], r.json()["effective_max_scans_per_user"]) == (3, 2, 2)
    seen = next(x for x in client.get("/api/runners", headers=operator).json() if x["name"] == "box")
    assert seen["max_targets"] == 3 and seen["effective_max_scans_per_user"] == 2
    assert seen["ssh_host"] is None  # the limits are public, the connection details are not


def test_a_runner_created_without_limits_uses_the_defaults(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 3)
    r = client.post("/api/runners", headers=admin, json={"name": "plain", "kind": "local"}).json()
    assert r["max_scans_per_user"] is None and r["max_targets"] is None and r["effective_max_scans_per_user"] == 3


@pytest.mark.parametrize("bad", [0, -1, 101, "x"])
def test_nonsense_account_limits_are_refused(client, admin, bad):
    r = client.post("/api/runners", headers=admin, json={"name": "x", "kind": "local", "max_scans_per_user": bad})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [0, -5, 1001, 2.5])
def test_nonsense_target_limits_are_refused(client, admin, bad):
    r = client.post("/api/runners", headers=admin, json={"name": "x", "kind": "local", "max_targets": bad})
    assert r.status_code == 422


def test_updating_limits_sets_clears_and_leaves_alone(client, admin):
    rid = client.post("/api/runners", headers=admin, json={"name": "r", "kind": "local", "max_targets": 4}).json()["id"]
    url = f"/api/runners/{rid}"
    r = client.patch(url, headers=admin, json={"name": "renamed"}).json()
    assert r["max_targets"] == 4  # not sent -> untouched
    r = client.patch(url, headers=admin, json={"max_scans_per_user": 7}).json()
    assert (r["max_scans_per_user"], r["max_targets"], r["effective_max_scans_per_user"]) == (7, 4, 7)
    r = client.patch(url, headers=admin, json={"max_targets": None}).json()
    assert r["max_targets"] is None and r["max_scans_per_user"] == 7  # null -> no limit
    r = client.patch(url, headers=admin, json={"max_scans_per_user": None}).json()
    assert r["max_scans_per_user"] is None and r["effective_max_scans_per_user"] == settings.max_active_scans_per_user


def test_only_an_administrator_sets_limits(client, operator):
    assert client.post("/api/runners", headers=operator, json={"name": "x", "kind": "local", "max_targets": 1}).status_code == 403
