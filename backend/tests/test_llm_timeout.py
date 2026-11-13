"""LLM_PREFLIGHT_TIMEOUT: how long Strix waits for the model's answer to its one test request, set by the admin and nothing else."""
import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.engine import LocalRunner
from app.engine.base import strix_llm_timeout_env
from app.engine.ssh import SshRunner, build_script, remote_dir
from tests.test_continue import local_strix  # noqa: F401
from tests.test_strix_privacy import spec


def test_the_default_is_strixs_own_so_it_changes_nothing():
    assert Settings(_env_file=None).llm_preflight_timeout == 30
    assert strix_llm_timeout_env() == {"LLM_PREFLIGHT_TIMEOUT": "30"}


def test_the_setting_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_PREFLIGHT_TIMEOUT", "120")
    assert Settings(_env_file=None).llm_preflight_timeout == 120


@pytest.mark.parametrize("bad", [0, -1, 601, "soon"])
def test_a_nonsense_value_is_refused_at_startup(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_preflight_timeout=bad)


def test_the_local_runner_sets_it_and_a_scans_own_env_cannot_override_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_preflight_timeout", 90)
    env = LocalRunner(strix_bin="strix").build_env(spec(tmp_path, env={"LLM_PREFLIGHT_TIMEOUT": "1", "LLM_API_KEY": "k"}))
    assert env["LLM_PREFLIGHT_TIMEOUT"] == "90" and env["LLM_API_KEY"] == "k"


def test_a_real_subprocess_receives_it(tmp_path, local_strix, monkeypatch):
    monkeypatch.setattr(settings, "llm_preflight_timeout", 75)
    s = spec(tmp_path, env={"LLM_PREFLIGHT_TIMEOUT": "1"})
    s.work_dir.mkdir(parents=True)
    assert LocalRunner().run(s, lambda _l: None, lambda: False).exit_code == 0
    assert json.loads((s.work_dir / "argv.json").read_text())["env"]["LLM_PREFLIGHT_TIMEOUT"] == "75"


def test_the_remote_script_sets_it_after_the_scans_own_variables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_preflight_timeout", 45)
    script = build_script(spec(tmp_path, env={"LLM_PREFLIGHT_TIMEOUT": "1"}), ["strix", "-n"], remote_dir(1))
    lines = script.splitlines()
    own = lines.index("export LLM_PREFLIGHT_TIMEOUT=1")
    last = max(i for i, l in enumerate(lines) if l.startswith("export LLM_PREFLIGHT_TIMEOUT="))
    assert lines[last] == "export LLM_PREFLIGHT_TIMEOUT=45" and last > own


def test_the_host_really_gets_it(tmp_path, fake_host, monkeypatch):
    monkeypatch.setattr(settings, "llm_preflight_timeout", 60)
    bindir = fake_host.home / ".local" / "bin"
    (bindir / "strix").write_text('#!/bin/sh\nprintf "PREFLIGHT=%s\\n" "$LLM_PREFLIGHT_TIMEOUT"\n')
    (bindir / "strix").chmod(0o755)
    lines: list[str] = []
    s = spec(tmp_path, env={"LLM_PREFLIGHT_TIMEOUT": "1"}, ssh=fake_host.target())
    assert SshRunner().run(s, lines.append, lambda: False).error is None
    assert "PREFLIGHT=60" in lines
