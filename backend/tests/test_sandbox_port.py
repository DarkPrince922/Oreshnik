"""The sandbox host-port clash: a sandbox network (no published port) and a restart when it happens anyway."""
from app.core.config import Settings, settings
from app.engine import LocalRunner, RunResult
from app.engine.base import strix_sandbox_env
from app.engine.retry import run_with_port_retry
from app.engine.ssh import build_script
from tests.test_strix_privacy import spec

CLASH = (
    'docker.errors.APIError: 500 Server Error: Internal Server Error ("failed to set up container networking: driver failed '
    'programming external connectivity on endpoint sharp_benz (97ef): failed to bind host port 127.0.0.1:32839/tcp: address already in use")'
)


class Scripted:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def run(self, spec, on_line, should_cancel):
        lines, result = self.outcomes[self.calls]
        self.calls += 1
        for line in lines:
            on_line(line)
        return result


def go(engine, **kw):
    out: list[str] = []
    result = run_with_port_retry(engine, None, out.append, kw.pop("cancel", lambda: False), sleep=lambda _s: None, **kw)
    return result, out


def test_off_by_default_so_nothing_changes():
    assert Settings(_env_file=None).strix_sandbox_network == "" and strix_sandbox_env() == {}


def test_network_name_is_validated():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, strix_sandbox_network="x; rm -rf /")


def test_network_reaches_local_env_and_cannot_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strix_sandbox_network", "oreshnik-sandbox")
    env = LocalRunner(strix_bin="strix").build_env(spec(tmp_path, env={"STRIX_DOCKER_SANDBOX_NETWORK": "evil"}))
    assert env["STRIX_DOCKER_SANDBOX_NETWORK"] == "oreshnik-sandbox"


def test_network_reaches_the_ssh_script_and_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strix_sandbox_network", "oreshnik-sandbox")
    script = build_script(spec(tmp_path), ["strix"], "oreshnik/scan-1")
    assert "export STRIX_DOCKER_SANDBOX_NETWORK=oreshnik-sandbox" in script
    assert "docker network create oreshnik-sandbox" in script


def test_a_clash_is_retried_and_the_second_try_wins():
    engine = Scripted(([CLASH], RunResult(exit_code=1)), (["ok"], RunResult(exit_code=0)))
    result, out = go(engine, retries=2)
    assert result.exit_code == 0 and engine.calls == 2
    assert any("повторный запуск" in l for l in out)


def test_retries_are_bounded():
    engine = Scripted(*[([CLASH], RunResult(exit_code=1))] * 3)
    result, _ = go(engine, retries=2)
    assert result.exit_code == 1 and engine.calls == 3


def test_other_failures_and_cancels_are_not_retried():
    for lines, res in (
        (["boom"], RunResult(exit_code=1)),
        ([CLASH], RunResult(exit_code=1, cancelled=True)),
        ([CLASH], RunResult(exit_code=1, timed_out=True)),
        ([CLASH], RunResult(error="no strix")),
        ([CLASH], RunResult(exit_code=0)),
    ):
        engine = Scripted((lines, res))
        go(engine, retries=2)
        assert engine.calls == 1
    engine = Scripted(([CLASH], RunResult(exit_code=1)))
    go(engine, retries=2, cancel=lambda: True)
    assert engine.calls == 1


def test_zero_retries_disables_it():
    engine = Scripted(([CLASH], RunResult(exit_code=1)))
    go(engine, retries=0)
    assert engine.calls == 1
