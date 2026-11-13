"""The Celery scan task end to end (run inline), with the mock and the fake-strix runners."""
import json
import shutil
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Finding, RunnerKind, ScanStatus
from app.services.scan_files import activity_path, agents_path, read_log_lines
from app.worker import tasks
from app.worker.tasks import reap_stale_scans, scan_run
from tests.conftest import load_scan, make_profile, make_runner, make_scan, make_user

FAKE = Path(__file__).parent / "fake_strix.py"


def findings(scan_id):
    with SessionLocal() as db:
        return list(db.scalars(select(Finding).where(Finding.scan_id == scan_id).order_by(Finding.id)))


def log_text(scan_id):
    return "\n".join(read_log_lines(scan_id, 0, 5000)[0])


@pytest.fixture
def local_strix(tmp_path, monkeypatch):
    target = tmp_path / "bin" / "strix"
    target.parent.mkdir()
    shutil.copy(FAKE, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    from app.core.config import settings
    monkeypatch.setattr(settings, "strix_bin", str(target))
    from app.engine import local
    monkeypatch.setattr(local, "cleanup_containers", lambda label: 0)


# ── mock runner ─────────────────────────────────────────────────────────

def test_mock_scan_finds_issues_and_stores_everything():
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id)
    assert scan_run(scan.id) == "vulns_found"
    done = load_scan(scan.id)
    assert done.status == ScanStatus.vulns_found and done.exit_code == 2
    assert done.run_status == "completed" and done.cost == 0.0 and done.run_name.startswith("mock-")
    assert done.started_at and done.finished_at and done.finished_at >= done.started_at
    rows = findings(scan.id)
    assert 3 <= len(rows) <= 5
    assert all(r.title.startswith("[MOCK]") for r in rows)  # simulated data is always labelled
    assert "Scan completed" in log_text(scan.id)


def test_mock_is_deterministic_for_the_same_targets():
    user, runner = make_user("a@example.com"), make_runner()
    a, b = (make_scan(user.id, runner.id, targets=["https://x.test"]) for _ in range(2))
    scan_run(a.id), scan_run(b.id)
    assert [f.title for f in findings(a.id)] == [f.title for f in findings(b.id)]


def test_mock_clean_directive():
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, instruction="please MOCK:clean")
    assert scan_run(scan.id) == "succeeded"
    assert findings(scan.id) == []


def test_mock_fail_directive():
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, instruction="MOCK:fail")
    assert scan_run(scan.id) == "failed"
    done = load_scan(scan.id)
    assert done.exit_code == 1 and "кодом 1" in done.status_detail and "model connection failed" in done.status_detail


def test_scan_is_claimed_only_once():
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id)
    assert scan_run(scan.id) == "vulns_found"
    assert scan_run(scan.id) == "skipped"  # a redelivered task must not run (and spend) twice


def test_a_scan_cancelled_while_queued_is_never_started():
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, status=ScanStatus.cancelled)
    assert scan_run(scan.id) == "skipped"
    assert load_scan(scan.id).status == ScanStatus.cancelled
    assert scan_run(999999) == "skipped"


def test_cancel_a_running_scan(monkeypatch):
    monkeypatch.setattr(tasks._CancelProbe, "INTERVAL", 0.05)
    from app.core.config import settings
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.05)
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, instruction="MOCK:slow")
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", scan_run(scan.id)))
    worker.start()
    for _ in range(100):
        if load_scan(scan.id).status == ScanStatus.running:
            break
        time.sleep(0.05)
    assert load_scan(scan.id).status == ScanStatus.running
    with SessionLocal() as db:  # what POST /cancel does
        from sqlalchemy import update
        from app.models import Scan
        db.execute(update(Scan).where(Scan.id == scan.id).values(status=ScanStatus.cancelling))
        db.commit()
    worker.join(timeout=10)
    assert not worker.is_alive() and out["r"] == "cancelled"
    done = load_scan(scan.id)
    assert done.status == ScanStatus.cancelled and done.run_status == "interrupted" and done.heartbeat_at


# ── preflight failures ──────────────────────────────────────────────────

def test_runner_deleted_before_start():
    user = make_user("a@example.com")
    scan = make_scan(user.id, None)
    assert scan_run(scan.id) == "failed"
    assert "Раннер был удалён" in load_scan(scan.id).status_detail
    assert "Раннер был удалён" in log_text(scan.id)  # also visible in the live log


def test_ssh_runner_is_not_supported_yet():
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.ssh)
    scan = make_scan(user.id, runner.id)
    assert scan_run(scan.id) == "failed" and "пока не поддерживаются" in load_scan(scan.id).status_detail


def test_real_runner_without_profile_fails_cleanly(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    scan = make_scan(user.id, runner.id)
    assert scan_run(scan.id) == "failed" and "LLM-профиль" in load_scan(scan.id).status_detail


def test_local_runner_without_strix_installed(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "strix_bin", "/definitely/not/strix")
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    profile = make_profile(user.id)
    scan = make_scan(user.id, runner.id, profile_id=profile.id)
    assert scan_run(scan.id) == "failed" and "Не найден исполняемый файл" in load_scan(scan.id).status_detail


# ── real runner path (fake strix executable) ────────────────────────────

def test_local_scan_end_to_end(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    profile = make_profile(user.id, key="sk-live-key-9999999999")
    scan = make_scan(user.id, runner.id, profile_id=profile.id, instruction="ok_findings", max_budget=3.0)
    assert scan_run(scan.id) == "vulns_found"
    done = load_scan(scan.id)
    assert done.exit_code == 2 and done.cost == 0.42 and done.total_tokens == 150 and done.run_name == "fake-run"
    assert [(f.vuln_id, f.severity) for f in findings(scan.id)] == [
        ("vuln-0003", "critical"), ("vuln-0002", "medium"), ("vuln-0001", "unknown")]
    from app.services.scan_files import scan_dir
    invoked = json.loads((scan_dir(scan.id) / "argv.json").read_text())
    assert invoked["argv"][:3] == ["-n", "-m", "quick"] and "--max-budget=3" in invoked["argv"]
    assert invoked["env"]["LLM_API_KEY"] == "sk-live-key-9999999999"  # decrypted from the profile
    assert invoked["env"]["STRIX_LLM"] == "anthropic/claude-sonnet-4-6"


def test_the_api_key_never_reaches_the_log(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    profile = make_profile(user.id, key="sk-live-key-9999999999")
    scan = make_scan(user.id, runner.id, profile_id=profile.id, instruction="secret_echo")
    scan_run(scan.id)
    text = log_text(scan.id)
    assert "debug LLM_API_KEY=***" in text and "sk-live-key" not in text


def test_ansi_and_progress_output_is_cleaned(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    scan = make_scan(user.id, runner.id, profile_id=make_profile(user.id).id, instruction="ansi")
    scan_run(scan.id)
    text = log_text(scan.id)
    assert "red text" in text and "\x1b" not in text and "progress 100%" in text and "progress 10%" not in text


def test_exit_code_2_without_findings_is_a_failure_not_a_vulnerability(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    scan = make_scan(user.id, runner.id, profile_id=make_profile(user.id).id, instruction="argerror")
    assert scan_run(scan.id) == "failed"
    assert "Invalid target" in load_scan(scan.id).status_detail


def test_clean_exit_without_artifacts_is_flagged(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    scan = make_scan(user.id, runner.id, profile_id=make_profile(user.id).id, instruction="no_artifacts")
    assert scan_run(scan.id) == "succeeded" and "артефакты" in load_scan(scan.id).status_detail


# ── reaper ──────────────────────────────────────────────────────────────

def test_reaper_fails_scans_whose_worker_vanished(monkeypatch):
    cleaned = []
    monkeypatch.setattr(tasks, "cleanup_containers", lambda label: cleaned.append(label) or 0)
    user, runner = make_user("a@example.com"), make_runner()
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    fresh = datetime.now(timezone.utc)
    stale = make_scan(user.id, runner.id, status=ScanStatus.running, started_at=old, heartbeat_at=old)
    stale_cancelling = make_scan(user.id, runner.id, status=ScanStatus.cancelling, started_at=old, heartbeat_at=old)
    never_beat = make_scan(user.id, runner.id, status=ScanStatus.running, started_at=old)
    alive = make_scan(user.id, runner.id, status=ScanStatus.running, started_at=old, heartbeat_at=fresh)
    queued = make_scan(user.id, runner.id, status=ScanStatus.queued)
    done = make_scan(user.id, runner.id, status=ScanStatus.succeeded, heartbeat_at=old)

    assert reap_stale_scans() == 3
    assert load_scan(stale.id).status == ScanStatus.failed and "Воркер потерян" in load_scan(stale.id).status_detail
    assert load_scan(stale_cancelling.id).status == ScanStatus.cancelled
    assert load_scan(never_beat.id).status == ScanStatus.failed
    assert load_scan(alive.id).status == ScanStatus.running
    assert load_scan(queued.id).status == ScanStatus.queued
    assert load_scan(done.id).status == ScanStatus.succeeded
    assert sorted(cleaned) == sorted(f"oreshnik-scan-{i.id}" for i in (stale, stale_cancelling, never_beat))
    assert reap_stale_scans() == 0


# ── agent activity ──────────────────────────────────────────────────────

def activity_events(scan_id):
    return [json.loads(l) for l in activity_path(scan_id).read_text().splitlines()]


def roster(scan_id):
    return {a["id"]: a for a in json.loads(agents_path(scan_id).read_text())}


def test_a_mock_scan_records_a_multi_agent_transcript():
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, targets=["https://shop.example.com"])
    scan_run(scan.id)
    evs = activity_events(scan.id)
    assert [e["seq"] for e in evs] == list(range(len(evs)))
    kinds = {e["kind"] for e in evs}
    assert {"task", "message", "tool_call", "tool_output"} <= kinds
    agents = roster(scan.id)
    assert agents["agent_root"]["parent_id"] is None and agents["agent_recon"]["parent_id"] == "agent_root"
    assert sum(1 for a in agents.values() if a["name"].startswith("Validator")) >= 3
    assert {a["status"] for a in agents.values()} == {"completed"}  # nothing is left "running" after a finished scan
    commands = [e["summary"] for e in evs if e["kind"] == "tool_call" and e["tool"] == "exec_command"]
    assert any(c.startswith("nmap") for c in commands) and any("curl" in c for c in commands)
    spawn = next(e for e in evs if e["tool"] == "create_agent" and e["kind"] == "tool_call")
    assert "Recon Specialist" in spawn["summary"]
    outputs = [e for e in evs if e["kind"] == "tool_output"]
    assert all(o["tool"] for o in outputs)  # every output is paired with its call


def test_clean_and_failed_mock_scans_leave_honest_agent_states():
    user, runner = make_user("a@example.com"), make_runner()
    failed = make_scan(user.id, runner.id, instruction="MOCK:fail")
    scan_run(failed.id)
    states = {a["status"] for a in roster(failed.id).values()}
    assert states == {"failed"} and "model connection failed" in next(iter(roster(failed.id).values()))["error"]
    clean = make_scan(user.id, runner.id, instruction="MOCK:clean")
    scan_run(clean.id)
    assert not any(e["tool"] == "create_vulnerability_report" for e in activity_events(clean.id))


def test_cancelling_marks_running_agents_as_stopped(monkeypatch):
    monkeypatch.setattr(tasks._CancelProbe, "INTERVAL", 0.05)
    from app.core.config import settings
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.05)
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, instruction="MOCK:slow")
    worker = threading.Thread(target=lambda: scan_run(scan.id))
    worker.start()
    for _ in range(100):
        if load_scan(scan.id).status == ScanStatus.running and activity_path(scan.id).exists() and len(activity_events(scan.id)) > 8:
            break
        time.sleep(0.05)
    seen_live = len(activity_events(scan.id))
    assert seen_live > 8, "events must appear while the scan is still running, not only at the end"
    with SessionLocal() as db:
        from sqlalchemy import update
        from app.models import Scan
        db.execute(update(Scan).where(Scan.id == scan.id).values(status=ScanStatus.cancelling))
        db.commit()
    worker.join(timeout=10)
    assert load_scan(scan.id).status == ScanStatus.cancelled
    assert {a["status"] for a in roster(scan.id).values()} == {"stopped"}
    assert len(activity_events(scan.id)) >= seen_live


def test_local_runner_activity_is_captured_and_the_api_key_is_redacted_in_it(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    profile = make_profile(user.id, key="sk-live-key-9999999999")
    scan = make_scan(user.id, runner.id, profile_id=profile.id, instruction="secret_echo")
    scan_run(scan.id)
    raw = activity_path(scan.id).read_text()
    assert "sk-live-key" not in raw and "hi (key=***)" in raw
    evs = activity_events(scan.id)
    assert [e["kind"] for e in evs] == ["task", "tool_call", "tool_output", "message"]
    assert roster(scan.id)["child"]["parent_id"] == "root"


def test_a_scan_without_agent_data_just_has_no_activity(local_strix):
    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    scan = make_scan(user.id, runner.id, profile_id=make_profile(user.id).id, instruction="fail")
    assert scan_run(scan.id) == "failed"
    assert not activity_path(scan.id).exists()
