"""LocalRunner against a fake `strix` executable — real subprocesses, real signals."""
import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from app.engine import LocalRunner, ScanSpec
from app.engine import local as local_module
from app.engine.docker_cleanup import cleanup_containers
from app.services.artifacts import parse_run_dir
from app.services.scan_files import find_run_dir

FAKE = Path(__file__).parent / "fake_strix.py"


@pytest.fixture
def strix_bin(tmp_path):
    target = tmp_path / "bin" / "strix"
    target.parent.mkdir()
    shutil.copy(FAKE, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    return str(target)


def make_spec(tmp_path, mode="ok_clean", **kw):
    defaults = dict(
        scan_id=7, targets=["https://example.com"], scan_mode="quick", instruction=mode, max_budget=None,
        work_dir=tmp_path / "scan-7", run_label="oreshnik-scan-7",
        env={"STRIX_LLM": "anthropic/claude-sonnet-4-6", "LLM_API_KEY": "sk-secret-123456"},
    )
    defaults.update(kw)
    return ScanSpec(**defaults)


def run(strix_bin, spec, cancel_after_lines=None):
    lines = []

    def on_line(line):
        lines.append(line)

    def should_cancel():
        return cancel_after_lines is not None and sum(1 for l in lines if l.startswith("working")) >= cancel_after_lines

    result = LocalRunner(strix_bin).run(spec, on_line, should_cancel)
    return result, lines


def invoked(spec):
    return json.loads((spec.work_dir / "argv.json").read_text())


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(local_module, "cleanup_containers", lambda label: calls.append(label) or 0)
    return calls


# ── command construction ────────────────────────────────────────────────

def test_build_argv_uses_option_equals_value_form(tmp_path):
    spec = make_spec(tmp_path, instruction="focus on auth", max_budget=2.5, scan_mode="standard",
                     targets=["https://a.com", "b.com"])
    assert LocalRunner("strix").build_argv(spec) == [
        "strix", "-n", "-m", "standard", "--target=https://a.com", "--target=b.com",
        "--instruction-file=instruction.txt", "--max-budget=2.5",
    ]


def test_build_argv_minimal(tmp_path):
    spec = make_spec(tmp_path, instruction=None)
    assert LocalRunner("strix").build_argv(spec) == ["strix", "-n", "-m", "quick", "--target=https://example.com"]


def test_a_target_that_looks_like_an_option_stays_one_attached_value(tmp_path):
    spec = make_spec(tmp_path, instruction=None, targets=["-evil"])
    assert LocalRunner("strix").build_argv(spec)[-1] == "--target=-evil"


# ── real subprocess behaviour ───────────────────────────────────────────

def test_environment_is_scrubbed_of_the_panels_own_secrets(tmp_path, strix_bin, monkeypatch):
    for name, value in {"JWT_SECRET": "j" * 40, "SECRET_ENCRYPTION_KEY": "fernet-key", "POSTGRES_PASSWORD": "pgpass",
                        "ADMIN_PASSWORD": "adminpass", "REDIS_URL": "redis://x"}.items():
        monkeypatch.setenv(name, value)
    spec = make_spec(tmp_path)
    run(strix_bin, spec)
    seen = invoked(spec)
    for leaked in ("JWT_SECRET", "SECRET_ENCRYPTION_KEY", "POSTGRES_PASSWORD", "ADMIN_PASSWORD", "REDIS_URL"):
        assert leaked not in seen["env"]
    assert seen["env"]["LLM_API_KEY"] == "sk-secret-123456"
    assert seen["env"]["STRIX_LLM"] == "anthropic/claude-sonnet-4-6"
    assert seen["env"]["STRIX_RUN_ID"] == "oreshnik-scan-7"
    assert seen["env"]["HOME"] == str(spec.work_dir / "home")  # config is never shared between scans
    assert Path(seen["cwd"]).resolve() == spec.work_dir.resolve()
    assert (spec.work_dir / "instruction.txt").read_text() == "ok_clean"


def test_clean_run(tmp_path, strix_bin):
    spec = make_spec(tmp_path, "ok_clean")
    result, lines = run(strix_bin, spec)
    assert (result.exit_code, result.cancelled, result.timed_out, result.error) == (0, False, False, None)
    assert any("nothing found" in l for l in lines)
    parsed = parse_run_dir(find_run_dir(spec.work_dir))
    assert parsed.run_status == "completed" and parsed.cost == 0.42 and parsed.findings == []


def test_findings_exit_code_2_and_unusual_data_survive_parsing(tmp_path, strix_bin):
    spec = make_spec(tmp_path, "ok_findings")
    result, _ = run(strix_bin, spec)
    assert result.exit_code == 2
    parsed = parse_run_dir(find_run_dir(spec.work_dir))
    assert [(f.vuln_id, f.severity, f.cvss) for f in parsed.findings] == [
        ("vuln-0003", "critical", 9.8), ("vuln-0002", "medium", 4.3), ("vuln-0001", "unknown", None)]


def test_failure_exit_code_and_output_are_reported(tmp_path, strix_bin):
    result, lines = run(strix_bin, make_spec(tmp_path, "fail"))
    assert result.exit_code == 1 and "DOCKER NOT AVAILABLE" in lines


def test_missing_binary_is_a_runner_error_not_a_crash(tmp_path):
    result = LocalRunner(str(tmp_path / "does-not-exist")).run(make_spec(tmp_path), lambda l: None, lambda: False)
    assert result.exit_code is None and "Не найден исполняемый файл" in result.error


def test_graceful_cancel_uses_sigint_and_lets_strix_clean_up(tmp_path, strix_bin):
    spec = make_spec(tmp_path, "slow")
    t0 = time.monotonic()
    result, lines = run(strix_bin, spec, cancel_after_lines=3)
    assert result.cancelled and result.exit_code == 130  # the fake's SIGINT handler ran
    assert "Interrupted, cleaning up" in lines
    assert time.monotonic() - t0 < 6
    assert parse_run_dir(find_run_dir(spec.work_dir)).run_status == "interrupted"


def test_a_process_that_ignores_sigint_is_escalated_to_sigterm(tmp_path, strix_bin):
    spec = make_spec(tmp_path, "ignore_sigint")
    t0 = time.monotonic()
    result, _ = run(strix_bin, spec, cancel_after_lines=2)
    assert result.cancelled and result.exit_code == -15  # killed by SIGTERM after the 1 s grace
    assert 0.9 < time.monotonic() - t0 < 8


def _is_dead(pid: int) -> bool:
    """Gone, or a zombie (killed but not yet reaped by init — os.kill(pid, 0) still succeeds on those)."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return True
    return any(line.startswith("State:") and "Z" in line.split(":", 1)[1] for line in status.splitlines())


def test_children_of_strix_die_with_it(tmp_path, strix_bin):
    spec = make_spec(tmp_path, "spawn_child")
    result, _ = run(strix_bin, spec, cancel_after_lines=2)
    assert result.cancelled
    pid = int((spec.work_dir / "child.pid").read_text())
    deadline = time.monotonic() + 3
    while not _is_dead(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _is_dead(pid)


def test_wall_clock_timeout(tmp_path, strix_bin):
    spec = make_spec(tmp_path, "slow", timeout_seconds=1)
    t0 = time.monotonic()
    result, _ = run(strix_bin, spec)
    assert result.timed_out and not result.cancelled
    assert time.monotonic() - t0 < 8


def test_sandbox_containers_are_cleaned_up_after_every_run(tmp_path, strix_bin, no_real_docker):
    run(strix_bin, make_spec(tmp_path, "ok_clean"))
    run(strix_bin, make_spec(tmp_path, "slow", work_dir=tmp_path / "scan-8"), cancel_after_lines=1)
    assert no_real_docker == ["oreshnik-scan-7", "oreshnik-scan-7"]


# ── docker cleanup helper itself ────────────────────────────────────────

def test_cleanup_containers_removes_exactly_the_labelled_ones(tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "ps" ]; then echo c1; echo c2; fi
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    assert cleanup_containers("oreshnik-scan-9") == 2
    assert log.read_text().splitlines() == ["ps -aq --filter label=strix-run-id=oreshnik-scan-9", "rm -f c1 c2"]


def test_cleanup_without_docker_is_a_noop(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert cleanup_containers("oreshnik-scan-9") == 0
