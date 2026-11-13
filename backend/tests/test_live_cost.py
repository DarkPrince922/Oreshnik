"""The spend of a running scan, read from Strix's run.json while the scan is still going."""
import json
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import RunnerKind, Scan, ScanStatus
from app.services.artifacts import read_live_usage
from app.worker import tasks
from app.worker.tasks import scan_run
from tests.conftest import load_scan, make_profile, make_runner, make_scan, make_user

FAKE = Path(__file__).parent / "fake_strix.py"


def write_run(path: Path, usage) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.json").write_text(usage if isinstance(usage, str) else json.dumps({"status": "running", "llm_usage": usage}))
    return path


def test_read_live_usage(tmp_path):
    assert read_live_usage(None) == (None, None)
    assert read_live_usage(tmp_path / "missing") == (None, None)
    assert read_live_usage(write_run(tmp_path / "a", {"cost": 0.42, "total_tokens": 1500})) == (0.42, 1500)
    assert read_live_usage(write_run(tmp_path / "b", {"cost": 1})) == (1.0, None)


@pytest.mark.parametrize("content", ["", "{ not json", "[]", '{"llm_usage": 5}', '{"status": "running"}', '{"llm_usage": {"cost": "free"}}'])
def test_a_half_written_or_odd_run_json_is_ignored_not_fatal(tmp_path, content):
    assert read_live_usage(write_run(tmp_path / "r", content)) == (None, None)


def poll_until(predicate, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


@pytest.fixture
def fast_probe(monkeypatch):
    monkeypatch.setattr(tasks._CancelProbe, "INTERVAL", 0.05)


def test_the_cost_of_a_running_scan_grows_in_the_database_while_it_runs(fast_probe, tmp_path, monkeypatch):
    """A real subprocess rewrites run.json with 0.11, 0.22, 0.33; the panel must see those values *during* the run."""
    target = tmp_path / "bin" / "strix"
    target.parent.mkdir()
    shutil.copy(FAKE, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(settings, "strix_bin", str(target))
    from app.engine import local
    monkeypatch.setattr(local, "cleanup_containers", lambda label: 0)

    user, runner = make_user("a@example.com"), make_runner(RunnerKind.local)
    scan = make_scan(user.id, runner.id, profile_id=make_profile(user.id).id, instruction="cost_live")
    worker = threading.Thread(target=lambda: scan_run(scan.id))
    worker.start()

    seen = []

    def watch():
        row = load_scan(scan.id)
        if row.status == ScanStatus.running and row.cost not in (None, 0.0) and row.cost not in seen:
            seen.append(row.cost)
        return len(seen) >= 2 and row.status == ScanStatus.running

    assert poll_until(watch), f"cost never changed while running: {seen}"
    assert seen == sorted(seen) and seen[0] < seen[-1] <= 0.33
    worker.join(timeout=20)
    done = load_scan(scan.id)
    assert done.status == ScanStatus.succeeded and done.cost == 0.33 and done.total_tokens == 150  # the final value wins


def test_the_simulated_runner_spends_nothing_unless_asked_to(fast_probe):
    user, runner = make_user("a@example.com"), make_runner()
    free = make_scan(user.id, runner.id)
    scan_run(free.id)
    assert load_scan(free.id).cost == 0.0 and load_scan(free.id).total_tokens == 0


def test_mock_cost_directive_simulates_a_growing_spend(fast_probe, monkeypatch):
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.05)
    user, runner = make_user("a@example.com"), make_runner()
    scan = make_scan(user.id, runner.id, instruction="MOCK:cost MOCK:slow")
    worker = threading.Thread(target=lambda: scan_run(scan.id))
    worker.start()
    values = []

    def watch():
        row = load_scan(scan.id)
        if row.status == ScanStatus.running and row.cost and row.cost not in values:
            values.append(row.cost)
        return len(values) >= 3

    assert poll_until(watch, timeout=30), f"live cost did not grow: {values}"
    assert values == sorted(values) and values[0] < values[-1]
    with SessionLocal() as db:
        from sqlalchemy import update
        db.execute(update(Scan).where(Scan.id == scan.id).values(status=ScanStatus.cancelling))
        db.commit()
    worker.join(timeout=20)
    final = load_scan(scan.id)
    assert final.status == ScanStatus.cancelled and final.cost >= values[-1] and final.total_tokens >= 1200  # what was spent stays booked


def test_a_continuations_cost_is_cumulative_and_its_own_part_is_derivable(fast_probe):
    from app.engine.mock_agents import MockAgents
    from app.services.scan_files import scan_dir
    user, runner = make_user("a@example.com"), make_runner()
    parent = make_scan(user.id, runner.id, status=ScanStatus.succeeded, run_name="r1", cost=0.6)
    run = scan_dir(parent.id) / "strix_runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"targets_info": [{"original": "https://example.com"}], "status": "completed",
                                              "llm_usage": {"cost": 0.6, "total_tokens": 7200, "requests": 6}}))
    agents = MockAgents(run)
    agents.spawn("agent_root", "Root", None, "t")
    agents.close()
    child = make_scan(user.id, runner.id, instruction="MOCK:cost", parent_scan_id=parent.id, resume_run_name="r1", cost_base=0.6)
    scan_run(child.id)
    done = load_scan(child.id)
    # The mock bills $0.03 / 1 200 tokens per step and a resume has 4 steps: Strix carried the parent's $0.60 and
    # 7 200 tokens over (cumulative) and this scan added its own $0.12 / 4 800 on top.
    assert done.cost_base == 0.6
    assert round(done.cost, 2) == 0.72 and done.total_tokens == 12000
    assert round(done.cost - done.cost_base, 2) == 0.12
