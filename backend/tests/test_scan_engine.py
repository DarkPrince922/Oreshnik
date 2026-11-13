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
from app.services.scan_files import read_log_lines
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
