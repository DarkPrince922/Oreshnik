import json
from datetime import timezone

from app.services.artifacts import normalize_severity, parse_run_dir


def write(run_dir, name, data):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / name).write_text(data if isinstance(data, str) else json.dumps(data))


def test_missing_directory_and_none_yield_empty_result(tmp_path):
    assert parse_run_dir(None).findings == []
    assert parse_run_dir(tmp_path / "nope").run_status is None


def test_parses_a_realistic_strix_run(tmp_path):
    write(tmp_path, "run.json", {"run_name": "r", "status": "completed",
                                 "llm_usage": {"cost": 1.25, "total_tokens": 1234, "requests": 9}})
    write(tmp_path, "vulnerabilities.json", [
        {"id": "vuln-0002", "title": "B", "severity": "low", "timestamp": "2026-01-01T10:00:00Z", "cvss": 3.1},
        {"id": "vuln-0001", "title": "A", "severity": "CRITICAL", "timestamp": "2026-01-01T09:00:00+00:00",
         "target": "https://x", "endpoint": "/a", "method": "POST", "cwe": "CWE-89", "cvss": "9.8"},
    ])
    write(tmp_path, "penetration_test_report.md", "# Report\n")
    run = parse_run_dir(tmp_path)
    assert (run.run_status, run.cost, run.total_tokens) == ("completed", 1.25, 1234)
    assert [f.vuln_id for f in run.findings] == ["vuln-0001", "vuln-0002"]  # most severe first
    first = run.findings[0]
    assert first.severity == "critical" and first.cvss == 9.8 and first.method == "POST"
    assert first.found_at.tzinfo is not None
    assert first.data["id"] == "vuln-0001"  # the full original record is preserved
    assert run.report_markdown == "# Report\n"


def test_unknown_severity_is_kept_not_dropped():
    assert normalize_severity("Catastrophic") == "unknown"
    assert normalize_severity(None) == "unknown"
    assert normalize_severity(" High ") == "high"


def test_naive_and_garbage_timestamps_do_not_break_sorting(tmp_path):
    write(tmp_path, "vulnerabilities.json", [
        {"id": "a", "title": "x", "severity": "high", "timestamp": "2026-01-01T10:00:00"},
        {"id": "b", "title": "y", "severity": "high", "timestamp": "not a date"},
        {"id": "c", "title": "z", "severity": "high"},
    ])
    run = parse_run_dir(tmp_path)
    assert len(run.findings) == 3
    assert run.findings[0].found_at.tzinfo == timezone.utc


def test_corrupt_and_malformed_files_are_reported_not_raised(tmp_path):
    write(tmp_path, "run.json", "{ not json")
    write(tmp_path, "vulnerabilities.json", {"oops": "an object, not a list"})
    run = parse_run_dir(tmp_path)
    assert run.findings == [] and run.run_status is None
    assert len(run.warnings) == 2


def test_non_dict_entries_and_missing_fields_are_tolerated(tmp_path):
    write(tmp_path, "vulnerabilities.json", ["string", 5, None, {"severity": "low"}])
    run = parse_run_dir(tmp_path)
    assert len(run.findings) == 1
    assert run.findings[0].title == "Без названия" and run.findings[0].vuln_id.startswith("vuln-")
