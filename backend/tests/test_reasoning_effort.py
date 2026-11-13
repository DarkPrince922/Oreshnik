"""STRIX_REASONING_EFFORT: how hard each model call thinks, set by the admin and nothing else."""
import json

import pytest
from pydantic import ValidationError

from app.core.config import STRIX_REASONING_EFFORTS, Settings, settings
from app.engine import LocalRunner, ScanSpec
from app.engine.base import strix_reasoning_env
from app.engine.ssh import SshRunner, build_script, remote_dir
from tests.test_continue import local_strix  # noqa: F401
from tests.test_strix_privacy import spec


# ── the setting ───────────────────────────────────────────────────────

def test_the_default_matches_strixs_own_default_so_it_changes_nothing():
    fresh = Settings(_env_file=None)
    assert fresh.strix_reasoning_effort == "high"
    assert strix_reasoning_env() == {"STRIX_REASONING_EFFORT": "high"}


def test_every_level_strix_itself_accepts_is_accepted_here(monkeypatch):
    assert set(STRIX_REASONING_EFFORTS) == {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    for level in STRIX_REASONING_EFFORTS:
        monkeypatch.setattr(settings, "strix_reasoning_effort", level)
        assert strix_reasoning_env() == {"STRIX_REASONING_EFFORT": level}


def test_a_typo_is_refused_at_startup_not_silently_sent_to_strix():
    with pytest.raises(ValidationError, match="STRIX_REASONING_EFFORT"):
        Settings(_env_file=None, strix_reasoning_effort="hihg")


def test_the_setting_is_case_and_whitespace_insensitive_like_strixs_own():
    assert Settings(_env_file=None, strix_reasoning_effort=" XHIGH ").strix_reasoning_effort == "xhigh"


def test_the_setting_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("STRIX_REASONING_EFFORT", "Low")
    assert Settings(_env_file=None).strix_reasoning_effort == "low"


# ── the local runner ──────────────────────────────────────────────────

def test_the_local_runner_always_sets_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strix_reasoning_effort", "minimal")
    env = LocalRunner(strix_bin="strix").build_env(spec(tmp_path))
    assert env["STRIX_REASONING_EFFORT"] == "minimal"


def test_a_scans_own_env_cannot_override_the_admin_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strix_reasoning_effort", "low")
    hostile = spec(tmp_path, env={"STRIX_REASONING_EFFORT": "max", "LLM_API_KEY": "k"})
    env = LocalRunner(strix_bin="strix").build_env(hostile)
    assert env["STRIX_REASONING_EFFORT"] == "low" and env["LLM_API_KEY"] == "k"


def test_a_real_subprocess_receives_it(tmp_path, local_strix, monkeypatch):
    monkeypatch.setattr(settings, "strix_reasoning_effort", "medium")
    s = spec(tmp_path, env={"STRIX_REASONING_EFFORT": "max"})
    s.work_dir.mkdir(parents=True)
    result = LocalRunner().run(s, lambda _l: None, lambda: False)
    assert result.exit_code == 0
    seen = json.loads((s.work_dir / "argv.json").read_text())["env"]
    assert seen["STRIX_REASONING_EFFORT"] == "medium"


# ── the SSH runner ────────────────────────────────────────────────────

def test_the_remote_script_sets_it_after_the_scans_own_variables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strix_reasoning_effort", "xhigh")
    script = build_script(spec(tmp_path, env={"STRIX_REASONING_EFFORT": "none"}), ["strix", "-n"], remote_dir(1))
    lines = script.splitlines()
    scan_own = lines.index("export STRIX_REASONING_EFFORT=none")
    last = max(i for i, l in enumerate(lines) if l.startswith("export STRIX_REASONING_EFFORT="))
    assert lines[last] == "export STRIX_REASONING_EFFORT=xhigh" and last > scan_own


def test_the_host_really_gets_it(tmp_path, fake_host, monkeypatch):
    monkeypatch.setattr(settings, "strix_reasoning_effort", "low")
    bindir = fake_host.home / ".local" / "bin"
    (bindir / "strix").write_text('#!/bin/sh\nprintf "EFFORT=%s\\n" "$STRIX_REASONING_EFFORT"\n')
    (bindir / "strix").chmod(0o755)
    lines: list[str] = []
    s = spec(tmp_path, env={"STRIX_REASONING_EFFORT": "max"}, ssh=fake_host.target())  # the scan tries to raise it; only the setting counts
    assert SshRunner().run(s, lines.append, lambda: False).error is None
    assert "EFFORT=low" in lines
