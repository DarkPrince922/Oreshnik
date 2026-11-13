"""Parsing of the files a Strix run leaves in its run directory.

Format verified against strix-agent 1.7.0 (`strix/report/writer.py`, `report/state.py`):

* run.json                    {run_id, run_name, start_time, end_time, status, targets_info,
                               llm_usage: {requests, input_tokens, output_tokens, total_tokens, cost}, ...}
                              status: running | completed | interrupted | failed | stopped
* vulnerabilities.json        list of {id: "vuln-0001", title, severity, timestamp, description, impact,
                               target, endpoint, method, cve, cwe, cvss, remediation_steps, ...}
* penetration_test_report.md  executive report
* findings.sarif              SARIF 2.1.0

Everything here is defensive: files may be missing (scan failed early), partial
(scan interrupted) or malformed. Finding text is attacker-influenced (it quotes the
scanned target), so callers must treat it as untrusted when rendering.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("oreshnik.artifacts")

MAX_FILE_BYTES = 20_000_000
MAX_FINDINGS = 1000
KNOWN_SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_ORDER = {s: i for i, s in enumerate(KNOWN_SEVERITIES)} | {"unknown": len(KNOWN_SEVERITIES)}


@dataclass
class ParsedFinding:
    vuln_id: str
    title: str
    severity: str
    target: str | None
    endpoint: str | None
    method: str | None
    cve: str | None
    cwe: str | None
    cvss: float | None
    found_at: datetime | None
    data: dict[str, Any]


@dataclass
class ParsedRun:
    run_name: str | None = None
    run_status: str | None = None
    cost: float | None = None
    total_tokens: int | None = None
    findings: list[ParsedFinding] = field(default_factory=list)
    report_markdown: str | None = None
    warnings: list[str] = field(default_factory=list)


def normalize_severity(value: Any) -> str:
    sev = str(value or "").strip().lower()
    return sev if sev in KNOWN_SEVERITIES else "unknown"


def _read_json(path: Path, warnings: list[str]) -> Any:
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            warnings.append(f"{path.name}: слишком большой файл, пропущен")
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"{path.name}: не удалось прочитать ({exc.__class__.__name__})")
        return None


def _str(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps are assumed UTC so that every value is comparable.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finding(raw: Any, index: int) -> ParsedFinding | None:
    if not isinstance(raw, dict):
        return None
    return ParsedFinding(
        vuln_id=_str(raw.get("id"), 64) or f"vuln-{index + 1:04d}",
        title=_str(raw.get("title"), 500) or "Без названия",
        severity=normalize_severity(raw.get("severity")),
        target=_str(raw.get("target"), 2048),
        endpoint=_str(raw.get("endpoint"), 2048),
        method=_str(raw.get("method"), 16),
        cve=_str(raw.get("cve"), 64),
        cwe=_str(raw.get("cwe"), 64),
        cvss=_float(raw.get("cvss")),
        found_at=_datetime(raw.get("timestamp")),
        data=raw,
    )


def parse_run_dir(run_dir: Path | None) -> ParsedRun:
    parsed = ParsedRun()
    if run_dir is None or not run_dir.is_dir():
        return parsed
    parsed.run_name = run_dir.name

    record = _read_json(run_dir / "run.json", parsed.warnings)
    if isinstance(record, dict):
        parsed.run_status = _str(record.get("status"), 32)
        usage = record.get("llm_usage")
        if isinstance(usage, dict):
            parsed.cost = _float(usage.get("cost"))
            parsed.total_tokens = _int(usage.get("total_tokens"))

    vulns = _read_json(run_dir / "vulnerabilities.json", parsed.warnings)
    if isinstance(vulns, list):
        if len(vulns) > MAX_FINDINGS:
            parsed.warnings.append(f"находок больше {MAX_FINDINGS}, сохранены первые {MAX_FINDINGS}")
        for i, raw in enumerate(vulns[:MAX_FINDINGS]):
            finding = _finding(raw, i)
            if finding:
                parsed.findings.append(finding)
    elif vulns is not None:
        parsed.warnings.append("vulnerabilities.json: ожидался список")

    latest = datetime.max.replace(tzinfo=timezone.utc)
    parsed.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.found_at or latest))

    report = run_dir / "penetration_test_report.md"
    if report.is_file():
        try:
            if report.stat().st_size <= MAX_FILE_BYTES:
                parsed.report_markdown = report.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            parsed.warnings.append(f"penetration_test_report.md: {exc.__class__.__name__}")

    return parsed
