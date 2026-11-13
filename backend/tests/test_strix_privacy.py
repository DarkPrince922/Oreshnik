"""Strix must not phone home by itself: no telemetry, no update check — unless an administrator turns them on."""
import json
from pathlib import Path

import pytest

from app.core.config import Settings, settings
from app.engine import LocalRunner, ScanSpec
from app.engine.base import strix_privacy_env
from app.engine.ssh import SshRunner, build_script, remote_dir
from tests.test_continue import local_strix  # noqa: F401


def spec(tmp_path, **extra) -> ScanSpec:
    return ScanSpec(scan_id=1, targets=["https://example.com"], scan_mode="quick", instruction=None, max_budget=None,
                    work_dir=tmp_path / "scan", run_label="oreshnik-scan-1", **extra)


# ── the settings ────────────────────────────────────────────────────────

def test_both_are_off_by_default():
    fresh = Settings(_env_file=None)
    assert fresh.strix_telemetry is False and fresh.strix_update_check is False
    assert strix_privacy_env() == {"STRIX_TELEMETRY": "0", "STRIX_NO_UPDATE_CHECK": "1"}


def test_an_administrator_can_turn_them_on(monkeypatch):
    monkeypatch.setattr(settings, "strix_telemetry", True)
    assert strix_privacy_env() == {"STRIX_TELEMETRY": "1", "STRIX_NO_UPDATE_CHECK": "1"}
    monkeypatch.setattr(settings, "strix_update_check", True)
    assert strix_privacy_env() == {"STRIX_TELEMETRY": "1"}  # the update check is on when the variable is simply not set


def test_the_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("STRIX_TELEMETRY", "true")
    monkeypatch.setenv("STRIX_UPDATE_CHECK", "1")
    env = Settings(_env_file=None)
    assert env.strix_telemetry is True and env.strix_update_check is True


# ── the local runner ────────────────────────────────────────────────────

def test_the_local_runner_always_sets_them(tmp_path):
    env = LocalRunner(strix_bin="strix").build_env(spec(tmp_path))
    assert env["STRIX_TELEMETRY"] == "0" and env["STRIX_NO_UPDATE_CHECK"] == "1"


def test_nothing_can_switch_them_back_on_behind_the_panels_back(tmp_path, monkeypatch):
    monkeypatch.setenv("STRIX_TELEMETRY", "1")  # the worker's own environment
    monkeypatch.delenv("STRIX_NO_UPDATE_CHECK", raising=False)
    hostile = spec(tmp_path, env={"STRIX_TELEMETRY": "1", "STRIX_NO_UPDATE_CHECK": "", "LLM_API_KEY": "k"})  # the scan's own variables
    env = LocalRunner(strix_bin="strix").build_env(hostile)
    assert env["STRIX_TELEMETRY"] == "0" and env["STRIX_NO_UPDATE_CHECK"] == "1" and env["LLM_API_KEY"] == "k"


def test_a_real_subprocess_receives_them(tmp_path, local_strix):
    s = spec(tmp_path, env={"STRIX_TELEMETRY": "1"})
    s.work_dir.mkdir(parents=True)
    result = LocalRunner().run(s, lambda _l: None, lambda: False)
    assert result.exit_code == 0
    seen = json.loads((s.work_dir / "argv.json").read_text())["env"]  # what the (fake) Strix process saw
    assert seen["STRIX_TELEMETRY"] == "0" and seen["STRIX_NO_UPDATE_CHECK"] == "1"


# ── the SSH runner ──────────────────────────────────────────────────────

def test_the_remote_script_sets_them_after_the_scans_own_variables(tmp_path):
    script = build_script(spec(tmp_path, env={"STRIX_TELEMETRY": "1"}), ["strix", "-n"], remote_dir(1))
    lines = script.splitlines()
    scan_own = lines.index("export STRIX_TELEMETRY=1")  # the scan's own value (a shell quotes only what needs it)
    last_telemetry = max(i for i, l in enumerate(lines) if l.startswith("export STRIX_TELEMETRY="))
    assert lines[last_telemetry] == "export STRIX_TELEMETRY=0" and last_telemetry > scan_own  # the last word wins in a shell
    assert "export STRIX_NO_UPDATE_CHECK=1" in lines and lines.index("export STRIX_NO_UPDATE_CHECK=1") > scan_own


@pytest.mark.parametrize("telemetry,expected", [(False, "0"), (True, "1")])
def test_the_host_really_gets_them(tmp_path, fake_host, monkeypatch, telemetry, expected):
    monkeypatch.setattr(settings, "strix_telemetry", telemetry)
    bindir = fake_host.home / ".local" / "bin"
    (bindir / "strix").write_text('#!/bin/sh\nprintf "TELEMETRY=%s\\n" "$STRIX_TELEMETRY"\nprintf "NOUPDATE=%s\\n" "$STRIX_NO_UPDATE_CHECK"\n')
    (bindir / "strix").chmod(0o755)
    lines: list[str] = []
    s = spec(tmp_path, env={"STRIX_TELEMETRY": "1"}, ssh=fake_host.target())  # the scan tries to switch it on; only the setting counts
    assert SshRunner().run(s, lines.append, lambda: False).error is None
    assert f"TELEMETRY={expected}" in lines and "NOUPDATE=1" in lines
