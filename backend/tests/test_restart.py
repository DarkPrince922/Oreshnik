"""«Продолжить» for a scan that failed or was stopped BEFORE Strix created a run (the model was unreachable, Docker failed,
it was stopped in the queue): there is nothing to resume, so the same scan starts again from the beginning."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import RunnerKind, ScanStatus
from app.services.scan_files import scan_dir
from app.worker.tasks import scan_run
from tests.conftest import load_scan, make_profile, make_runner, make_scan
from tests.test_continue import local_strix, mock_runner, queued_only  # noqa: F401
from tests.test_resume_in_place import log_lines, me, resume, total_scans


def never_started(client, headers, runner, status=ScanStatus.failed, **extra):
    extra.setdefault("status_detail", "LLM CONNECTION FAILED")
    return make_scan(me(client, headers), runner.id, status=status, exit_code=1, **extra)


def test_a_scan_that_failed_before_strix_started_runs_again_as_the_same_scan(client, operator, mock_runner):
    scan = never_started(client, operator, mock_runner, instruction="original task")
    r = resume(client, operator, scan.id)
    assert r.status_code == 200  # not "created": it is the same scan
    out = r.json()
    assert out["id"] == scan.id and total_scans(client, operator) == 1
    assert out["status"] == "vulns_found" and out["run_name"]  # the simulated runner really ran it this time
    assert out["resume_count"] == 0 and out["parent_scan_id"] is None  # a fresh run, not a resume
    assert out["instruction"] == "original task" and out["status_detail"] != "LLM CONNECTION FAILED"


def test_it_goes_back_to_the_queue_clean_and_the_log_says_so(client, operator, mock_runner, queued_only):
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    scan = never_started(client, operator, mock_runner, started_at=t0, finished_at=t0 + timedelta(seconds=30), max_budget=3.0)
    out = resume(client, operator, scan.id, max_budget=5).json()
    assert out["status"] == "queued" and out["status_detail"] is None and out["max_budget"] == 5
    row = load_scan(scan.id)
    assert row.exit_code is None and row.started_at is None and row.finished_at is None
    assert row.elapsed_base == 30.0 and row.resume_count == 0 and row.resume_run_name is None
    assert any("Повторный запуск" in line for line in log_lines(client, operator, scan.id))


def test_a_scan_stopped_in_the_queue_restarts_too_but_one_that_finished_does_not(client, operator, mock_runner, queued_only):
    stopped = never_started(client, operator, mock_runner, status=ScanStatus.cancelled)
    assert resume(client, operator, stopped.id).json()["status"] == "queued"
    done = make_scan(me(client, operator), mock_runner.id, status=ScanStatus.succeeded)
    r = resume(client, operator, done.id, instruction="x")
    assert r.status_code == 409 and "не дошёл до запуска" in r.json()["detail"]


def test_a_second_click_is_refused(client, operator, mock_runner, queued_only):
    scan = never_started(client, operator, mock_runner)
    assert resume(client, operator, scan.id).status_code == 200
    r = resume(client, operator, scan.id)
    assert r.status_code == 409 and "ещё выполняется" in r.json()["detail"]


def test_somebody_elses_scan_restarts_only_as_a_new_scan_on_your_key(client, admin, operator, mock_runner, queued_only):
    scan = never_started(client, operator, mock_runner, instruction="original task", targets=["https://a.example"])
    assert resume(client, admin, scan.id).status_code == 403
    r = resume(client, admin, scan.id, as_new_scan=True)
    assert r.status_code == 201
    new = r.json()
    assert new["id"] != scan.id and new["user_id"] == me(client, admin) and new["status"] == "queued"
    assert new["targets"] == ["https://a.example"] and new["instruction"] == "original task"
    assert new["parent_scan_id"] is None and load_scan(new["id"]).resume_run_name is None  # no run to carry over
    assert load_scan(scan.id).status == ScanStatus.failed  # the original is left as it was


def test_the_real_cli_is_started_from_scratch_not_resumed(client, operator, local_strix):
    runner = make_runner(RunnerKind.local, "local")
    profile = make_profile(me(client, operator), key="sk-live-key-9999999999")
    scan = make_scan(me(client, operator), runner.id, profile_id=profile.id, instruction="fail")
    assert scan_run(scan.id) == "failed"  # the fake Strix exits before writing a run, like a failed LLM preflight
    assert load_scan(scan.id).run_name is None

    r = resume(client, operator, scan.id, instruction="ok_findings")  # a new instruction replaces the old one
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "vulns_found" and out["run_name"] == "fake-run" and out["findings_total"] == 3
    assert out["instruction"] == "ok_findings" and out["resume_count"] == 0
    argv = json.loads((scan_dir(scan.id) / "argv.json").read_text())["argv"]
    assert any(a.startswith("--target=") for a in argv) and not any(a.startswith("--resume") for a in argv)
    log = "\n".join(log_lines(client, operator, scan.id))
    assert "DOCKER NOT AVAILABLE" in log and "Повторный запуск" in log and "done: 3 findings" in log  # one log, in order


def test_a_real_runner_still_needs_your_own_profile(client, operator, local_strix, queued_only):
    runner = make_runner(RunnerKind.local, "local")
    scan = never_started(client, operator, runner)  # no profile on the scan
    r = resume(client, operator, scan.id)
    assert r.status_code == 422 and "LLM-профиль" in r.json()["detail"]
    profile = make_profile(me(client, operator), key="sk-live-key-9999999999")
    assert resume(client, operator, scan.id, llm_profile_id=profile.id).json()["llm_profile_id"] == profile.id
