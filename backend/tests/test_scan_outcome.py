import pytest

from app.engine.base import RunResult
from app.models import ScanStatus
from app.services.artifacts import ParsedFinding, ParsedRun
from app.services.scan_outcome import decide


def finding():
    return ParsedFinding("v1", "t", "high", None, None, None, None, None, None, None, {})


def run(status="completed", n=0):
    return ParsedRun(run_status=status, findings=[finding() for _ in range(n)])


@pytest.mark.parametrize("result,parsed,early,expected", [
    (RunResult(exit_code=0), run(), None, ScanStatus.succeeded),
    (RunResult(exit_code=0), run(n=2), None, ScanStatus.vulns_found),
    (RunResult(exit_code=2), run(n=2), None, ScanStatus.vulns_found),
    (RunResult(exit_code=2), run(n=0), None, ScanStatus.failed),        # argparse also exits 2!
    (RunResult(exit_code=2), ParsedRun(), None, ScanStatus.failed),
    (RunResult(exit_code=1), run(n=3), None, ScanStatus.failed),
    (RunResult(exit_code=None), run(), None, ScanStatus.failed),
    (RunResult(cancelled=True, exit_code=130), run(n=1), None, ScanStatus.cancelled),
    (RunResult(timed_out=True, exit_code=-2), run(), None, ScanStatus.failed),
    (RunResult(error="no binary"), run(), None, ScanStatus.failed),
    (RunResult(exit_code=0), run(), "preflight problem", ScanStatus.failed),
])
def test_status_table(result, parsed, early, expected):
    assert decide(result, parsed, early, 180)[0] == expected


def test_exit_2_without_findings_explains_itself():
    status, detail = decide(RunResult(exit_code=2), ParsedRun(), None, 180, "strix: error: Invalid target")
    assert status == ScanStatus.failed and "кодом 2" in detail and "Invalid target" in detail


def test_clean_exit_with_unexpected_run_status_is_flagged_as_possibly_incomplete():
    status, detail = decide(RunResult(exit_code=0), run(status="stopped"), None, 180)
    assert status == ScanStatus.succeeded and "stopped" in detail and "неполным" in detail


def test_clean_exit_without_artifacts_is_flagged():
    status, detail = decide(RunResult(exit_code=0), ParsedRun(), None, 180)
    assert status == ScanStatus.succeeded and "артефакты" in detail


def test_timeout_message_mentions_the_limit():
    assert "30" in decide(RunResult(timed_out=True), run(), None, 30)[1]


def test_cancel_wins_over_everything_else():
    assert decide(RunResult(cancelled=True, timed_out=True, error="x"), run(), "early", 180)[0] == ScanStatus.cancelled
