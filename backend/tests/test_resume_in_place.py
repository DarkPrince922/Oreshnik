"""Continuing a scan IN PLACE: the same scan (same id) goes back to the queue and Strix picks its run up again."""
import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.api import scans as scans_api
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import RunnerKind, Scan, ScanStatus
from app.services.activity import ActivityTailer
from app.services.scan_files import activity_path, log_path, scan_dir
from app.worker.tasks import scan_run
from tests.conftest import auth_headers, load_scan, make_profile, make_runner, make_scan, make_user
from tests.test_continue import _fake_run_dir, create, local_parent, local_strix, mock_runner, queued_only, run_json  # noqa: F401


def resume(client, headers, sid, **body):
    return client.post(f"/api/scans/{sid}/continue", headers=headers, json=body)


def me(client, headers) -> int:
    return client.get("/api/auth/me", headers=headers).json()["id"]


def finished(client, headers, runner, **extra) -> dict:
    """A finished scan with run data on disk, without running anything (for tests that keep the queue frozen)."""
    scan = make_scan(me(client, headers), runner.id, status=ScanStatus.succeeded, run_name="r1", **extra)
    _fake_run_dir(scan.id)
    return client.get(f"/api/scans/{scan.id}", headers=headers).json()


def total_scans(client, headers) -> int:
    return client.get("/api/scans", headers=headers).json()["total"]


def events(client, headers, sid) -> list[dict]:
    return client.get(f"/api/scans/{sid}/activity?limit=2000", headers=headers).json()["events"]


def log_lines(client, headers, sid) -> list[str]:
    return client.get(f"/api/scans/{sid}/log?limit=5000", headers=headers).json()["lines"]


# ── the same scan, not a new one ────────────────────────────────────────

def test_the_scan_continues_in_place_with_the_same_id(client, operator, mock_runner):
    first = create(client, operator, instruction="original task").json()
    assert first["status"] == "vulns_found"
    r = resume(client, operator, first["id"], instruction="Look closer at the admin area")
    assert r.status_code == 200  # not "created": nothing new exists
    scan = r.json()
    assert scan["id"] == first["id"] and total_scans(client, operator) == 1
    assert scan["status"] == "vulns_found" and scan["run_name"] == first["run_name"]
    assert scan["findings_total"] == first["findings_total"] + 1  # cumulative, as in the run directory
    assert scan["resume_count"] == 1 and scan["parent_scan_id"] is None and scan["cost_base"] is None
    assert scan["instruction"] == "original task"  # what the scan was started with is not rewritten
    assert [h["instruction"] for h in scan["resume_history"]] == ["Look closer at the admin area"]
    assert len(list((scan_dir(first["id"]) / "strix_runs").iterdir())) == 1  # no copy of the run
    assert client.get(f"/api/scans/{first['id']}", headers=operator).json()["continuations"] == []


def test_the_log_carries_on_after_what_it_already_has(client, operator, mock_runner):
    first = create(client, operator).json()
    before = log_lines(client, operator, first["id"])
    resume(client, operator, first["id"], instruction="check the admin area")
    after = log_lines(client, operator, first["id"])
    assert after[: len(before)] == before and len(after) > len(before)  # appended, nothing lost or rewritten
    new = "\n".join(after[len(before):])
    assert "Продолжение скана (раз: 1)" in new and "Resuming run" in new


def test_the_agent_feed_carries_on_with_continuous_numbers_and_no_duplicates(client, operator, mock_runner):
    first = create(client, operator).json()
    old = events(client, operator, first["id"])
    resume(client, operator, first["id"], instruction="check the admin area")
    now = events(client, operator, first["id"])
    assert now[: len(old)] == old  # the earlier history is exactly as it was
    assert [e["seq"] for e in now] == list(range(len(now)))  # no gap and no repeat: the browser addresses events by seq
    added = now[len(old):]
    assert (added[0]["kind"], added[0]["role"], added[0]["text"]) == ("message", "user", "check the admin area")
    assert any(e["kind"] == "tool_call" and e["tool"] == "create_agent" for e in added)
    page = client.get(f"/api/scans/{first['id']}/activity", headers=operator).json()
    assert {a["id"] for a in page["agents"]} >= {"agent_root", "agent_recon"}


def test_it_can_be_continued_again_and_again(client, operator, mock_runner):
    first = create(client, operator).json()
    one = resume(client, operator, first["id"], instruction="one").json()
    two = resume(client, operator, first["id"], instruction="two").json()
    assert two["id"] == first["id"] and two["resume_count"] == 2 and two["findings_total"] == first["findings_total"] + 2
    assert [h["instruction"] for h in two["resume_history"]] == ["one", "two"]
    assert one["findings_total"] == first["findings_total"] + 1
    assert total_scans(client, operator) == 1
    seqs = [e["seq"] for e in events(client, operator, first["id"])]
    assert seqs == list(range(len(seqs)))


def test_a_scan_that_finished_on_its_own_needs_an_instruction_but_a_stopped_one_does_not(client, operator, mock_runner):
    done = create(client, operator).json()
    r = resume(client, operator, done["id"])
    assert r.status_code == 422 and "что агентам делать дальше" in r.json()["detail"]
    assert resume(client, operator, done["id"], instruction="  ").status_code == 422
    assert load_scan(done["id"]).resume_count == 0  # a refused request changes nothing
    stopped = make_scan(me(client, operator), mock_runner.id, status=ScanStatus.cancelled, run_name="r1")
    _fake_run_dir(stopped.id)
    r = resume(client, operator, stopped.id)
    assert r.status_code == 200 and r.json()["status"] in ("succeeded", "vulns_found")


# ── the state of the scan row ───────────────────────────────────────────

def test_the_time_spent_so_far_is_banked_and_the_clock_restarts(client, operator, mock_runner, queued_only):
    start = datetime.now(timezone.utc) - timedelta(hours=5)
    scan = make_scan(me(client, operator), mock_runner.id, status=ScanStatus.cancelled, run_name="r1",
                     started_at=start, finished_at=start + timedelta(minutes=10), elapsed_base=60.0,
                     exit_code=130, status_detail="Остановлен пользователем")
    _fake_run_dir(scan.id)
    out = resume(client, operator, scan.id, instruction="go on").json()
    assert out["status"] == "queued" and out["started_at"] is None and out["finished_at"] is None
    assert out["elapsed_base"] == 660.0  # 60 s from before + 10 min of the last run; the 5 h idle in between do not count
    assert out["status_detail"] is None and out["exit_code"] is None
    scan_run(scan.id)
    done = load_scan(scan.id)
    assert done.started_at is not None and done.elapsed_base == 660.0 and done.finished_at >= done.started_at


def test_a_scan_that_never_ran_still_resumes_with_zero_banked_time(client, operator, mock_runner, queued_only):
    scan = make_scan(me(client, operator), mock_runner.id, status=ScanStatus.failed, run_name="r1")  # no started_at
    _fake_run_dir(scan.id)
    assert resume(client, operator, scan.id).json()["elapsed_base"] == 0.0


def test_only_finished_scans_can_be_continued_in_place(client, operator, mock_runner):
    for status in (ScanStatus.queued, ScanStatus.running, ScanStatus.cancelling):
        scan = make_scan(me(client, operator), mock_runner.id, status=status, run_name="r1")
        _fake_run_dir(scan.id)
        r = resume(client, operator, scan.id, instruction="x")
        assert r.status_code == 409 and "ещё выполняется" in r.json()["detail"]
        assert load_scan(scan.id).resume_count == 0 and load_scan(scan.id).status == status


def test_a_second_click_is_refused_not_queued_twice(client, operator, mock_runner, queued_only):
    scan = finished(client, operator, mock_runner)
    first = resume(client, operator, scan["id"], instruction="a")
    assert first.status_code == 200 and first.json()["status"] == "queued"
    second = resume(client, operator, scan["id"], instruction="b")
    assert second.status_code == 409
    row = load_scan(scan["id"])
    assert row.resume_count == 1 and [h["instruction"] for h in row.resume_history] == ["a"]


def test_a_competitor_that_wins_after_the_checks_is_refused(client, operator, mock_runner, monkeypatch):
    """Between the checks and the update somebody else may take the scan (a second request, a worker): the loser gets a
    clear refusal and nothing is double-booked."""
    scan = create(client, operator).json()
    real = scans_api.check_resumable

    def sneaky(scan_id, run_name):
        real(scan_id, run_name)
        with SessionLocal() as db:  # a competing request wins right after our checks
            db.query(Scan).filter(Scan.id == scan_id).update({"status": ScanStatus.queued})
            db.commit()

    monkeypatch.setattr(scans_api, "check_resumable", sneaky)
    r = resume(client, operator, scan["id"], instruction="x")
    assert r.status_code == 409 and "уже продолжается" in r.json()["detail"]
    assert load_scan(scan["id"]).resume_count == 0


def test_the_conditional_update_is_the_last_line_of_defence(client, operator, mock_runner, monkeypatch):
    """A competitor that wins in the last instant before the update — after every check has passed — is still refused:
    the update itself only applies to a finished scan."""
    scan = create(client, operator).json()

    class Racing(datetime):
        @classmethod
        def now(cls, tz=None):
            with SessionLocal() as db:  # the competing request takes the scan right here
                db.query(Scan).filter(Scan.id == scan["id"]).update({"status": ScanStatus.queued})
                db.commit()
            return datetime.now(tz)

    monkeypatch.setattr(scans_api, "datetime", Racing)
    r = resume(client, operator, scan["id"], instruction="x")
    assert r.status_code == 409 and r.json()["detail"] == "Скан уже продолжается"
    row = load_scan(scan["id"])
    assert row.resume_count == 0 and row.resume_history == []  # nothing of the loser was written


def test_a_refused_request_leaves_the_scan_exactly_as_it_was(client, operator, mock_runner, monkeypatch):
    scan = create(client, operator, instruction="orig").json()
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    make_scan(me(client, operator), mock_runner.id)  # one active scan already
    before = load_scan(scan["id"])
    assert resume(client, operator, scan["id"], instruction="x").status_code == 429
    after = load_scan(scan["id"])
    assert (after.status, after.resume_count, after.finished_at, after.started_at, after.max_budget, after.elapsed_base) == \
           (before.status, 0, before.finished_at, before.started_at, before.max_budget, before.elapsed_base)


# ── who may do it ───────────────────────────────────────────────────────

def test_permissions(client, admin, operator, viewer, mock_runner):
    sid = create(client, operator).json()["id"]
    assert resume(client, viewer, sid, instruction="x").status_code == 403
    assert client.post(f"/api/scans/{sid}/continue", json={"instruction": "x"}).status_code == 401
    make_user("op2@example.com")
    assert resume(client, auth_headers(client, "op2@example.com"), sid, instruction="x").status_code == 404
    assert resume(client, operator, 99999, instruction="x").status_code == 404


def test_an_admin_cannot_continue_someone_elses_scan_in_place_but_can_fork_it(client, admin, operator, mock_runner):
    """The key is personal: continuing in place would run the owner's scan on the admin's key and bill the owner."""
    sid = create(client, operator).json()["id"]
    r = resume(client, admin, sid, instruction="x")
    assert r.status_code == 403 and "новый скан" in r.json()["detail"]
    assert load_scan(sid).resume_count == 0
    fork = resume(client, admin, sid, instruction="x", as_new_scan=True)
    assert fork.status_code == 201 and fork.json()["id"] != sid and fork.json()["owner_email"] == "admin@example.com"
    own = create(client, admin).json()  # an admin's own scan continues in place like anybody's
    assert resume(client, admin, own["id"], instruction="x").json()["id"] == own["id"]


def test_the_viewer_does_not_see_what_was_asked(client, operator, viewer, mock_runner):
    sid = create(client, operator).json()["id"]
    resume(client, operator, sid, instruction="secret test credentials: admin/hunter2")
    mine = client.get(f"/api/scans/{sid}", headers=operator).json()
    seen = client.get(f"/api/scans/{sid}", headers=viewer).json()
    assert mine["resume_history"][0]["instruction"] == "secret test credentials: admin/hunter2"
    assert seen["resume_count"] == 1 and seen["resume_history"][0]["instruction"] is None
    assert "hunter2" not in json.dumps(seen)
    assert "hunter2" not in json.dumps(client.get("/api/scans", headers=viewer).json())


# ── budget, profile, runner ─────────────────────────────────────────────

@pytest.mark.parametrize("limit,ok", [(1.5, False), (2.0, False), (2.01, True), (50, True), (None, True)])
def test_strix_budget_is_for_the_whole_run(client, operator, mock_runner, queued_only, limit, ok):
    scan = make_scan(me(client, operator), mock_runner.id, status=ScanStatus.succeeded, run_name="r1", cost=2.0, max_budget=5.0)
    _fake_run_dir(scan.id)
    body = {"instruction": "go on"}
    if limit is not None:
        body["max_budget"] = limit
    r = resume(client, operator, scan.id, **body)
    assert r.status_code == (200 if ok else 422)
    if ok:
        assert r.json()["max_budget"] == limit  # the new limit replaces the old one; none means no limit
        assert r.json()["resume_history"][0]["max_budget"] == limit
    else:
        assert "$2.00" in r.json()["detail"] and load_scan(scan.id).max_budget == 5.0 and load_scan(scan.id).resume_count == 0


def test_a_real_runner_needs_your_own_profile_and_it_is_recorded(client, operator, local_strix, queued_only):
    local = make_runner(RunnerKind.local, "local")
    op = me(client, operator)
    mine, other = make_profile(op, name="mine"), make_profile(make_user("op2@example.com").id, name="theirs")
    scan = make_scan(op, local.id, status=ScanStatus.succeeded, run_name="r1", profile_id=mine.id)
    _fake_run_dir(scan.id)
    assert resume(client, operator, scan.id, instruction="a", llm_profile_id=other.id).status_code == 404  # not yours
    assert load_scan(scan.id).resume_count == 0
    spare = make_profile(op, name="spare")
    out = resume(client, operator, scan.id, instruction="a", llm_profile_id=spare.id).json()
    assert out["llm_profile_name"] == "spare"


def test_a_scan_whose_profile_is_gone_asks_for_one(client, operator, local_strix, queued_only):
    local = make_runner(RunnerKind.local, "local")
    scan = make_scan(me(client, operator), local.id, status=ScanStatus.succeeded, run_name="r1")  # profile deleted earlier
    _fake_run_dir(scan.id)
    r = resume(client, operator, scan.id, instruction="a")
    assert r.status_code == 422 and "свой LLM-профиль" in r.json()["detail"]


def test_a_deleted_runner_or_missing_run_data_blocks_it(client, admin, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    shutil.rmtree(scan_dir(sid) / "strix_runs")
    r = resume(client, operator, sid, instruction="x")
    assert r.status_code == 409 and "нет файлов прогона" in r.json()["detail"]
    sid2 = create(client, operator).json()["id"]
    client.delete(f"/api/runners/{mock_runner.id}", headers=admin)
    assert resume(client, operator, sid2, instruction="x").json()["detail"] == "Раннер этого скана удалён"


def test_a_run_cannot_be_active_twice_even_across_a_fork_and_in_place(client, operator, mock_runner, queued_only):
    scan = finished(client, operator, mock_runner)
    fork = resume(client, operator, scan["id"], instruction="a", as_new_scan=True).json()  # a copy of the run is now queued
    r = resume(client, operator, scan["id"], instruction="b")
    assert r.status_code == 409 and f"#{fork['id']}" in r.json()["detail"]


def test_the_active_scan_limit_and_the_broker(client, operator, mock_runner, monkeypatch):
    scan = create(client, operator).json()
    with monkeypatch.context() as broken:
        broken.setattr(scans_api.scan_run, "delay", lambda *_a, **_k: (_ for _ in ()).throw(ConnectionError("broker down")))
        r = resume(client, operator, scan["id"], instruction="x")
    assert r.status_code == 503
    row = load_scan(scan["id"])
    assert row.status == ScanStatus.failed and row.status_detail == "Очередь задач недоступна" and row.resume_count == 1
    # nothing was lost: the run is intact and the scan can be picked up again
    again = resume(client, operator, scan["id"])
    assert again.status_code == 200 and again.json()["resume_count"] == 2 and again.json()["status"] in ("succeeded", "vulns_found")


# ── accounting ──────────────────────────────────────────────────────────

def test_cost_is_cumulative_and_counted_once(client, operator, mock_runner):
    first = create(client, operator, instruction="MOCK:cost").json()
    assert round(first["cost"], 2) > 0
    out = resume(client, operator, first["id"], instruction="more MOCK:cost").json()
    assert out["cost_base"] is None and out["cost"] > first["cost"] and round(out["cost"] - first["cost"], 2) == 0.12
    costs = client.get("/api/costs?days=30", headers=operator).json()
    assert round(costs["periods"]["all_time"], 2) == round(out["cost"], 2)  # the whole scan, once
    assert costs["top_scans"][0]["id"] == first["id"]


def test_a_fork_that_is_continued_in_place_counts_only_its_own_part(client, operator, mock_runner):
    parent = create(client, operator, instruction="MOCK:cost").json()
    fork = resume(client, operator, parent["id"], instruction="a MOCK:cost", as_new_scan=True).json()
    assert fork["cost_base"] == parent["cost"]
    again = resume(client, operator, fork["id"], instruction="b MOCK:cost").json()
    assert again["id"] == fork["id"] and again["cost_base"] == parent["cost"] and again["cost"] > fork["cost"]
    costs = client.get("/api/costs?days=30", headers=operator).json()
    expected = parent["cost"] + (again["cost"] - again["cost_base"])
    assert round(costs["periods"]["all_time"], 2) == round(expected, 2)


def test_a_resumed_scan_holds_back_its_schedule_like_any_running_scan(client, operator, mock_runner, queued_only):
    from app.services.schedule import fire_scheduled
    from tests.conftest import make_schedule
    op = me(client, operator)
    schedule = make_schedule(op, mock_runner.id)
    scan = make_scan(op, mock_runner.id, status=ScanStatus.succeeded, run_name="r1", schedule_id=schedule.id)
    _fake_run_dir(scan.id)
    assert resume(client, operator, scan.id, instruction="x").json()["status"] == "queued"
    with SessionLocal() as db:
        assert fire_scheduled(db, schedule.id, lambda _id: None, datetime.now(timezone.utc)) == "skipped"
    assert total_scans(client, operator) == 1


# ── the worker, with a fake strix executable ────────────────────────────

def test_the_real_cli_shape_on_the_scans_own_run(client, operator, local_strix):
    scan, _, profile = local_parent(client, operator)
    run_dir = next((scan_dir(scan.id) / "strix_runs").iterdir())
    out = resume(client, operator, scan.id, instruction="focus on the admin panel", max_budget=4)
    assert out.status_code == 200
    out = out.json()
    assert out["id"] == scan.id and out["status"] == "vulns_found" and out["findings_total"] == 4
    assert out["cost"] == 0.84 and out["cost_base"] is None and out["total_tokens"] == 300
    argv = json.loads((scan_dir(scan.id) / "argv.json").read_text())["argv"]
    assert "--resume=fake-run" in argv and not any(a.startswith(("--target", "-t")) for a in argv)
    assert "--instruction-file=instruction.txt" in argv and "--max-budget=4" in argv
    assert (scan_dir(scan.id) / "instruction.txt").read_text() == "focus on the admin panel"
    assert next((scan_dir(scan.id) / "strix_runs").iterdir()) == run_dir  # the very same directory, nothing copied
    env = json.loads((scan_dir(scan.id) / "argv.json").read_text())["env"]
    assert env["LLM_API_KEY"] == "sk-live-key-9999999999"
    log = "\n".join(log_lines(client, operator, scan.id))
    assert "done: 3 findings" in log and "Продолжение скана (раз: 1)" in log and "Resumed fake-run" in log  # old and new, one log
    assert len(client.get(f"/api/scans/{scan.id}/findings", headers=operator).json()) == 4


def test_a_continue_without_a_new_instruction_does_not_repeat_the_old_one(client, operator, local_strix):
    scan, _, _ = local_parent(client, operator)  # started with the instruction "ok_findings"
    assert resume(client, operator, scan.id, instruction="startup_error first").json()["status"] == "failed"
    argv = json.loads((scan_dir(scan.id) / "argv.json").read_text())["argv"]
    assert "--instruction-file=instruction.txt" in argv  # the instruction of THIS resume is passed on
    assert resume(client, operator, scan.id).status_code == 200  # a failed scan is simply picked up again
    argv = json.loads((scan_dir(scan.id) / "argv.json").read_text())["argv"]
    assert "--resume=fake-run" in argv and not any(a.startswith("--instruction-file") for a in argv)  # not the old ones again
    assert [h["instruction"] for h in load_scan(scan.id).resume_history] == ["startup_error first", None]


def test_a_startup_error_is_not_taken_for_findings_the_scan_already_had(client, operator, local_strix):
    scan, _, _ = local_parent(client, operator)
    out = resume(client, operator, scan.id, instruction="startup_error please").json()
    assert out["status"] == "failed" and out["exit_code"] == 2 and "startup problem" in out["status_detail"]
    # what the scan had is still there...
    assert out["findings_total"] == 3 and out["cost"] == 0.42 and out["run_name"] == "fake-run"
    # ...and it can simply be tried again
    again = resume(client, operator, scan.id, instruction="a proper instruction").json()
    assert again["status"] == "vulns_found" and again["findings_total"] == 4 and again["resume_count"] == 2


def test_results_survive_a_run_that_cannot_be_read_back(client, operator, local_strix, queued_only):
    """The run directory vanishes between queueing and starting: the scan fails, but its findings and cost stay."""
    scan, _, _ = local_parent(client, operator)
    resume(client, operator, scan.id, instruction="x")
    assert load_scan(scan.id).status == ScanStatus.queued
    shutil.rmtree(scan_dir(scan.id) / "strix_runs")
    assert scan_run(scan.id) == "failed"
    row = load_scan(scan.id)
    assert "нет файлов прогона" in row.status_detail
    assert row.cost == 0.42 and row.run_name == "fake-run" and row.total_tokens is not None
    assert len(client.get(f"/api/scans/{scan.id}/findings", headers=operator).json()) == 3


def test_a_failed_preflight_keeps_everything_too(client, operator, local_strix, queued_only):
    scan, _, profile = local_parent(client, operator)
    resume(client, operator, scan.id, instruction="x")
    with SessionLocal() as db:  # the key is deleted before the worker gets to it
        from app.models import LLMProfile
        db.query(LLMProfile).filter(LLMProfile.id == profile.id).delete()
        db.commit()
    assert scan_run(scan.id) == "failed"
    row = load_scan(scan.id)
    assert "LLM-профиль" in row.status_detail and row.cost == 0.42 and row.run_name == "fake-run"
    assert len(client.get(f"/api/scans/{scan.id}/findings", headers=operator).json()) == 3


def test_it_can_be_stopped_while_continuing_and_continued_again(client, operator, mock_runner, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.05)
    from app.worker import tasks
    monkeypatch.setattr(tasks._CancelProbe, "INTERVAL", 0.05)
    def agents_are_there(sid):
        return any((scan_dir(sid) / "strix_runs").glob("*/.state/agents.json"))

    scan = make_scan(me(client, operator), mock_runner.id, instruction="MOCK:slow")
    t = threading.Thread(target=lambda: scan_run(scan.id))
    t.start()
    deadline = time.time() + 20
    while not (load_scan(scan.id).status == ScanStatus.running and agents_are_there(scan.id)) and time.time() < deadline:
        time.sleep(0.05)
    assert client.post(f"/api/scans/{scan.id}/cancel", headers=operator).status_code == 200
    t.join(timeout=30)
    assert load_scan(scan.id).status == ScanStatus.cancelled
    # picked up again: in place, as a slow continuation this time, which is stopped as well
    r = resume(client, operator, scan.id, instruction="MOCK:slow go on")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    t = threading.Thread(target=lambda: scan_run(scan.id))
    t.start()
    while load_scan(scan.id).status != ScanStatus.running and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)  # let the continuation get going
    client.post(f"/api/scans/{scan.id}/cancel", headers=operator)
    t.join(timeout=30)
    row = load_scan(scan.id)
    assert row.status == ScanStatus.cancelled and row.resume_count == 1 and row.started_at is not None
    # ...and a third time, to the end
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.0)
    last = resume(client, operator, scan.id, instruction="finish it")
    assert last.status_code == 200 and last.json()["resume_count"] == 2 and last.json()["status"] == "queued"
    assert scan_run(scan.id) in ("succeeded", "vulns_found")
    assert load_scan(scan.id).elapsed_base > 0  # the time of the earlier runs is kept


def test_deleting_a_continued_scan_removes_its_files(client, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    resume(client, operator, sid, instruction="x")
    assert client.delete(f"/api/scans/{sid}", headers=operator).status_code == 204
    assert not scan_dir(sid).exists() and total_scans(client, operator) == 0


# ── units ───────────────────────────────────────────────────────────────

def test_the_tailer_carries_on_numbering_from_the_existing_feed(client, operator, mock_runner):
    first = create(client, operator).json()
    n = len(events(client, operator, first["id"]))
    run = next((scan_dir(first["id"]) / "strix_runs").iterdir())
    tailer = ActivityTailer(first["id"])
    tailer.continue_in_place(run)
    assert tailer._seq == n and tailer._bytes == activity_path(first["id"]).stat().st_size
    assert tailer.poll() == 0  # everything in agents.db is already in the feed: nothing is written twice


def test_the_tailer_copes_with_a_damaged_or_missing_feed(client, operator, mock_runner):
    first = create(client, operator).json()
    run = next((scan_dir(first["id"]) / "strix_runs").iterdir())
    path = activity_path(first["id"])
    n = len(path.read_text().splitlines())
    path.write_text(path.read_text() + "{ not json\n")  # a torn last line
    tailer = ActivityTailer(first["id"])
    tailer.continue_in_place(run)
    assert tailer._seq == n + 1  # falls back to counting lines
    path.unlink()
    fresh = ActivityTailer(first["id"])
    fresh.continue_in_place(run)  # an old scan from before the feed existed: starts from zero, never crashes
    assert fresh._seq == 0
