"""The sandbox host-port clash: a sandbox network (no published port) and a restart when it happens anyway."""
from types import SimpleNamespace

import pytest

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
    result = run_with_port_retry(engine, SimpleNamespace(scan_id=1), out.append, kw.pop("cancel", lambda: False), sleep=lambda _s: None, **kw)
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


# ── the model was unreachable for a moment ──────────────────────────────

from app.engine.retry import llm_failure_is_transient, run_with_retries  # noqa: E402

# Strix's box as it reaches the log: wrapped at the terminal width, the reason split over lines.
LLM_502 = [
    "╭─ STRIX ─────────────────────────────╮",
    "│  LLM CONNECTION FAILED               │",
    "│  Could not establish connection to the language model. │",
    "│  Please check your configuration and try again.        │",
    "│  Error: Error code: 502 - {'error': {'message': 'openai_error', 'type': │",
    "│  'bad_response_status_code', 'param': '', 'code':      │",
    "│  'bad_response_status_code'}}                          │",
    "╰──────────────────────────────────────╯",
]


def llm_box(reason: str) -> list[str]:
    return ["│  LLM CONNECTION FAILED  │", "│  Could not establish connection to the language model. │", f"│  Error: {reason}  │"]


def go_llm(engine, **kw):
    out: list[str] = []
    waits: list[float] = []
    kw.setdefault("llm_delay", 90)
    result = run_with_retries(engine, SimpleNamespace(scan_id=1), out.append, kw.pop("cancel", lambda: False), sleep=waits.append, **kw)
    return result, out, waits


@pytest.mark.parametrize("reason", [
    "Error code: 502 - {'error': {'type': 'bad_response_status_code'}}", "Error code: 503 - Service Unavailable", "Error code: 504 - Gateway Timeout",
    "Error code: 500 - Internal Server Error", "Error code: 529 - {'type': 'overloaded_error'}", "Error code: 429 - Rate limit reached, try again in 20s",
    "Error code: 524", "Request timed out.", "Connection error.", "litellm.Timeout: timeout after 30s", "Server disconnected without sending a response",
])
def test_reasons_that_pass_by_waiting(reason):
    assert llm_failure_is_transient(reason)


@pytest.mark.parametrize("reason", [
    "Error code: 401 - Incorrect API key provided", "Error code: 403 - permission denied", "Error code: 404 - The model `gpt-9` does not exist",
    "Error code: 429 - You exceeded your current quota, please check your plan and billing details", "Error code: 400 - bad request",
    "Error code: 402 - insufficient_quota", "something entirely unknown",
])
def test_reasons_that_waiting_will_not_fix(reason):
    assert not llm_failure_is_transient(reason)


def test_a_502_from_the_gateway_is_retried_after_the_pause_and_the_second_try_wins():
    engine = Scripted((LLM_502, RunResult(exit_code=1)), (["scan started"], RunResult(exit_code=0)))
    result, out, waits = go_llm(engine, llm_retries=2)
    assert result.exit_code == 0 and engine.calls == 2
    assert any("Модель временно недоступна" in l and "90 с" in l for l in out)
    assert sum(waits) == pytest.approx(90, abs=2) and max(waits) <= 1.0  # waits in short steps, so a stop is noticed


def test_llm_retries_are_bounded_and_can_be_switched_off():
    engine = Scripted(*[(LLM_502, RunResult(exit_code=1))] * 3)
    result, _, _ = go_llm(engine, llm_retries=2, llm_delay=5)
    assert result.exit_code == 1 and engine.calls == 3
    off = Scripted((LLM_502, RunResult(exit_code=1)))
    go_llm(off, llm_retries=0)
    assert off.calls == 1


def test_a_wrong_key_or_an_error_without_strixs_heading_is_reported_at_once():
    for lines in (llm_box("Error code: 401 - Incorrect API key provided"), llm_box("Error code: 429 - insufficient_quota"), ["Error code: 502 somewhere mid-scan"]):
        engine = Scripted((lines, RunResult(exit_code=1)))
        _, out, waits = go_llm(engine, llm_retries=2)
        assert engine.calls == 1 and waits == [] and not any("повторный запуск" in l for l in out)


def test_a_stop_during_the_pause_ends_the_scan_instead_of_starting_it_again():
    polls = iter([False, False, True])
    engine = Scripted((LLM_502, RunResult(exit_code=1)))
    result, _, _ = go_llm(engine, llm_retries=2, cancel=lambda: next(polls, True))
    assert engine.calls == 1 and result.cancelled


def test_the_defaults_and_the_settings():
    fresh = Settings(_env_file=None)
    assert fresh.llm_retry_attempts == 2 and fresh.llm_retry_delay_seconds == 90
    with pytest.raises(Exception):
        Settings(_env_file=None, llm_retry_delay_seconds=1)


def test_the_codes_are_a_setting_and_499_is_among_them_by_default(monkeypatch):
    assert llm_failure_is_transient("Error code: 499 - client closed request")
    monkeypatch.setattr(settings, "llm_retry_codes", "502,503")
    assert not llm_failure_is_transient("Error code: 499") and not llm_failure_is_transient("Error code: 504 - Gateway Timeout")
    assert llm_failure_is_transient("Error code: 503") and llm_failure_is_transient("Request timed out.")  # no code named: the words decide
    monkeypatch.setattr(settings, "llm_retry_codes", "401, 404, 418")
    assert llm_failure_is_transient("Error code: 418") and not llm_failure_is_transient("Error code: 401") and not llm_failure_is_transient("Error code: 404")
    monkeypatch.setattr(settings, "llm_retry_codes", "")
    assert not llm_failure_is_transient("Error code: 502") and llm_failure_is_transient("Connection error.")
    with pytest.raises(Exception):
        Settings(_env_file=None, llm_retry_codes="502; drop")


# ── the model went away while the scan was running ──────────────────────

from app.engine import ScanSpec  # noqa: E402
from app.services.scan_files import scan_dir  # noqa: E402
from tests.test_continue import _fake_run_dir  # noqa: E402

MID_502 = ["scanning...", "Error during penetration test: Error code: 502 - {'error': {'message': 'openai_error', 'type':", "'bad_response_status_code'}}", "Traceback (most recent call last):"]


class Recording(Scripted):
    def run(self, spec, on_line, should_cancel):
        self.specs = getattr(self, "specs", []) + [spec]
        return super().run(spec, on_line, should_cancel)


def running_spec(scan_id=7, **kw):
    return ScanSpec(scan_id=scan_id, targets=["https://example.com"], scan_mode="quick", instruction="original task", max_budget=None, work_dir=scan_dir(scan_id), run_label="l", **kw)


def go_mid(engine, spec, **kw):
    out: list[str] = []
    result = run_with_retries(engine, spec, out.append, lambda: False, sleep=lambda _s: None, llm_delay=5, **kw)
    return result, out


def test_a_scan_that_lost_the_model_midway_is_continued_not_started_over():
    _fake_run_dir(7, "run-abc")
    engine = Recording((MID_502, RunResult(exit_code=1)), (["Resumed run-abc"], RunResult(exit_code=0)))
    result, out = go_mid(engine, running_spec(), llm_retries=2)
    assert result.exit_code == 0 and engine.calls == 2
    first, second = engine.specs
    assert first.resume_run is None and first.instruction == "original task"
    assert second.resume_run == "run-abc" and second.instruction is None  # its own run, and the task is not said twice
    assert any("продолжение с того же места" in l for l in out)


def test_without_resumable_state_or_with_a_permanent_reason_it_just_fails():
    engine = Recording((MID_502, RunResult(exit_code=1)))  # no run directory: nothing to pick up
    go_mid(engine, running_spec(scan_id=8), llm_retries=2)
    assert engine.calls == 1
    _fake_run_dir(9, "run-x")
    for lines in (["Error during penetration test: Error code: 401 - Incorrect API key provided"], ["Error during penetration test: list index out of range"]):
        engine = Recording((lines, RunResult(exit_code=1)))
        go_mid(engine, running_spec(scan_id=9), llm_retries=2)
        assert engine.calls == 1


def test_midway_continuations_share_the_same_limit():
    _fake_run_dir(10, "run-y")
    engine = Recording(*[(MID_502, RunResult(exit_code=1))] * 3)
    result, _ = go_mid(engine, running_spec(scan_id=10), llm_retries=2)
    assert result.exit_code == 1 and engine.calls == 3 and engine.specs[2].resume_run == "run-y"


def test_the_scan_time_limits_reach_strix_and_default_to_its_own(monkeypatch):
    from app.engine.base import strix_llm_timeout_env
    assert strix_llm_timeout_env()["LLM_TIMEOUT"] == "300" and strix_llm_timeout_env()["LLM_STREAM_IDLE_TIMEOUT"] == "300"
    monkeypatch.setattr(settings, "llm_timeout", 900)
    monkeypatch.setattr(settings, "llm_stream_idle_timeout", 0)
    assert strix_llm_timeout_env()["LLM_TIMEOUT"] == "900" and strix_llm_timeout_env()["LLM_STREAM_IDLE_TIMEOUT"] == "0"


# ── the rest of Strix's tuning ──────────────────────────────────────────

def test_untouched_tuning_passes_only_strixs_own_defaults(tmp_path):
    from app.engine.base import strix_tuning_env
    env = strix_tuning_env()
    assert env == {
        "STRIX_SANDBOX_LOG_MAX_SIZE": "50m", "STRIX_SANDBOX_LOG_MAX_FILE": "3", "STRIX_PROMPT_CACHE": "true", "LLM_DISABLE_STREAMING": "false",
        "STRIX_FORCE_REQUIRED_TOOL_CHOICE": "false", "LLM_MAX_TOOL_CALLS_PER_TURN": "32", "STRIX_CONTEXT_AUTO_COMPACT": "true",
        "STRIX_CONTEXT_FALLBACK_TOKENS": "200000", "STRIX_TOOL_OUTPUT_MAX_TOKENS": "8000", "STRIX_MAX_CONTEXT_IMAGES": "3",
        "STRIX_WEB_SEARCH_PROVIDER": "auto", "STRIX_DEBUG": "false",
    }  # no limits, no headers, no keys: nothing that is not set is sent


def test_set_tuning_reaches_both_runners_and_a_scan_cannot_override_it(tmp_path, monkeypatch):
    for key, value in {"strix_sandbox_mem_limit": "4g", "strix_sandbox_cpus": "1.5", "strix_sandbox_pids_limit": 512, "llm_disable_streaming": True,
                       "llm_extra_headers": '{"X-Team": "red"}', "strix_debug": True, "exa_api_key": "exa-secret-1"}.items():
        monkeypatch.setattr(settings, key, value)
    env = LocalRunner(strix_bin="strix").build_env(spec(tmp_path, env={"STRIX_SANDBOX_MEM_LIMIT": "64g", "LLM_DISABLE_STREAMING": "false"}))
    assert env["STRIX_SANDBOX_MEM_LIMIT"] == "4g" and env["STRIX_SANDBOX_CPUS"] == "1.5" and env["STRIX_SANDBOX_PIDS_LIMIT"] == "512"
    assert env["LLM_DISABLE_STREAMING"] == "true" and env["STRIX_DEBUG"] == "true" and env["EXA_API_KEY"] == "exa-secret-1"
    assert env["LLM_EXTRA_HEADERS"] == '{"X-Team": "red"}'
    script = build_script(spec(tmp_path), ["strix"], "oreshnik/scan-1")
    assert "export STRIX_SANDBOX_MEM_LIMIT=4g" in script and "export LLM_DISABLE_STREAMING=true" in script


def test_tuning_values_are_validated(monkeypatch):
    from app.core import runtime_settings as rs
    assert rs.parse(rs.BY_KEY["strix_sandbox_mem_limit"], "4g") == "4g" and rs.parse(rs.BY_KEY["strix_sandbox_mem_limit"], "") == ""
    for key, bad in (("strix_sandbox_mem_limit", "4g; id"), ("strix_sandbox_cpus", "two"), ("strix_sandbox_log_max_size", ""), ("strix_dedupe_model", "a b"),
                     ("llm_extra_headers", "not json"), ("llm_extra_headers", '["a"]'), ("llm_extra_headers", '{"a": 1}'), ("strix_web_search_provider", "google")):
        with pytest.raises(ValueError):
            rs.parse(rs.BY_KEY[key], bad)
    assert rs.parse(rs.BY_KEY["llm_extra_headers"], '{"X-Team": "red"}') == '{"X-Team": "red"}'
    assert "perplexity_api_key" not in rs.BY_KEY and "exa_api_key" not in rs.BY_KEY  # secrets stay in .env
