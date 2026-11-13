"""MockRunner: simulates a Strix run without calling any LLM or starting any container.

It writes the same artifacts, in the same format, as real Strix 1.7.0 (run.json,
vulnerabilities.json, vulnerabilities/<id>.md, penetration_test_report.md, findings.sarif and the
multi-agent transcript in .state/), so the parser, API and UI are exercised exactly as with a real
scan. Every finding is clearly marked "[MOCK]" so simulated data can never be mistaken for a real result.

Behaviour can be steered from the scan's instruction text (handy for UI development):

    MOCK:clean   no findings           (exit 0)
    MOCK:fail    run fails early       (exit 1)
    MOCK:slow    long run (~60 steps)  — to try cancelling
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import settings
from app.engine.base import RunResult, ScanSpec
from app.engine.mock_agents import MockAgents

_DIRECTIVE_RE = re.compile(r"MOCK:(\w+)", re.IGNORECASE)

# (severity, title, endpoint, method, cwe, cvss)
_TEMPLATES = [
    ("critical", "SQL injection in login endpoint", "/api/login", "POST", "CWE-89", 9.8),
    ("high", "Stored XSS in comment field", "/comments", "POST", "CWE-79", 7.4),
    ("high", "IDOR exposes other users' invoices", "/api/invoices/{id}", "GET", "CWE-639", 7.5),
    ("medium", "Missing rate limiting on password reset", "/api/password/reset", "POST", "CWE-307", 5.3),
    ("medium", "Verbose error messages leak stack traces", "/api/search", "GET", "CWE-209", 5.0),
    ("low", "Missing security headers (CSP, X-Frame-Options)", "/", "GET", "CWE-693", 3.1),
    ("info", "Server version disclosed in response headers", "/", "GET", "CWE-200", 0.0),
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "target"


def _host(target: str) -> str:
    return urlparse(target if "://" in target else f"//{target}").hostname or target


def _pick_templates(targets: list[str]) -> list[tuple]:
    """Deterministic 3-5 findings derived from the targets."""
    digest = hashlib.sha256("|".join(targets).encode()).digest()
    count = 3 + digest[0] % 3
    start = digest[1] % len(_TEMPLATES)
    return [_TEMPLATES[(start + i) % len(_TEMPLATES)] for i in range(count)]


class MockRunner:
    def run(
        self,
        spec: ScanSpec,
        on_line: Callable[[str], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        directives = {m.lower() for m in _DIRECTIVE_RE.findall(spec.instruction or "")}
        delay = settings.mock_delay_seconds * (2 if "slow" in directives else 1)
        started = datetime.now(timezone.utc)
        run_name = f"mock-{_slug(spec.targets[0])}-{hashlib.sha1(str(spec.scan_id).encode()).hexdigest()[:4]}"
        run_dir = spec.work_dir / "strix_runs" / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        agents = MockAgents(run_dir)

        def pause() -> bool:
            """Sleep one step in small slices; True if cancellation was requested."""
            end = time.monotonic() + delay
            while True:
                if should_cancel():
                    return True
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))

        def say(line: str) -> bool:
            on_line(line)
            return pause()

        record = {
            "run_id": run_name,
            "run_name": run_name,
            "start_time": started.isoformat(),
            "end_time": None,
            "status": "running",
            "auth_mode": "mock",
            "targets_info": [{"original": t} for t in spec.targets],
            "llm_usage": {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0},
        }
        self._write_json(run_dir / "run.json", record)

        try:
            return self._run(spec, directives, record, run_dir, agents, say, pause, on_line)
        finally:
            agents.close()

    def _run(self, spec, directives, record, run_dir, agents: MockAgents, say, pause, on_line) -> RunResult:
        t0 = spec.targets[0]
        host = _host(t0)

        def interrupted() -> RunResult:
            agents.stop_all("stopped")
            return self._interrupted(run_dir, record, on_line)

        steps = [
            "[mock] Oreshnik mock runner — no LLM calls, no containers",
            f"Strix (simulated) mode={spec.scan_mode} targets={len(spec.targets)}",
            "Preflight: model connection ... ok (simulated)",
            "Starting sandbox ... ok (simulated)",
        ]
        for step in steps:
            if say(step):
                return interrupted()

        # Root agent: the task it was launched with, a plan, and first reconnaissance.
        agents.spawn("agent_root", "Root Agent", None,
                     f"[MOCK] You are the root agent of a simulated penetration test.\nTargets: {', '.join(spec.targets)}")
        agents.say("agent_root", f"[MOCK] Plan: fingerprint {host}, delegate reconnaissance, then validate each suspicious lead with a specialist agent.")
        for t in spec.targets:
            on_line(f"Target: {t}")
            agents.run_command("agent_root", f"nmap -sV -Pn {_host(t)}",
                               f"Starting Nmap 7.94 ( https://nmap.org )\nNmap scan report for {_host(t)}\nPORT    STATE SERVICE VERSION\n"
                               "80/tcp  open  http    nginx 1.18.0\n443/tcp open  ssl/http nginx 1.18.0\n[MOCK] simulated output")
            if say(f"Agent root: nmap -sV -Pn {_host(t)}"):
                return interrupted()

        on_line(f"Agent root: mapping attack surface of {t0}")
        call = agents.call("agent_root", "create_agent", {
            "name": "Recon Specialist", "task": f"Enumerate endpoints, parameters and technologies of {t0}", "skills": ["reconnaissance"]})
        agents.spawn("agent_recon", "Recon Specialist", "agent_root", f"[MOCK] Enumerate endpoints, parameters and technologies of {t0}")
        agents.output("agent_root", call, "Agent 'Recon Specialist' created (id: agent_recon). It runs in parallel.")
        if pause():
            return interrupted()

        recon_commands = [
            (f"curl -sI {t0}", "HTTP/1.1 200 OK\nServer: nginx/1.18.0\nContent-Type: text/html; charset=UTF-8\n[MOCK] simulated output"),
            (f"ffuf -u {t0.rstrip('/')}/FUZZ -w /usr/share/wordlists/common.txt -mc 200,301", "api    [Status: 301]\nadmin  [Status: 403]\nlogin  [Status: 200]\n[MOCK] simulated output"),
        ]
        if "slow" in directives:
            recon_commands = [(f"curl -s '{t0.rstrip('/')}/page/{i}'", f"<html>… simulated page {i} …</html>") for i in range(1, 61)]
        for i, (cmd, result) in enumerate(recon_commands, 1):
            if i == 1:
                agents.say("agent_recon", "[MOCK] Starting with the cheap, quiet probes.")
            agents.run_command("agent_recon", cmd, result)
            label = f"({i}/60)" if "slow" in directives else ""
            if say(f"Agent recon-{i}: crawling endpoints {label}".rstrip()):
                return interrupted()

        if "fail" in directives:
            on_line("Error: model connection failed (simulated MOCK:fail)")
            agents.set_status("agent_recon", "failed", "model connection failed (simulated)")
            agents.set_status("agent_root", "failed", "model connection failed (simulated)")
            record.update(status="failed", end_time=datetime.now(timezone.utc).isoformat())
            self._write_json(run_dir / "run.json", record)
            return RunResult(exit_code=1)

        agents.run_command("agent_recon", "echo 'recon complete'", "recon complete")
        agents.set_status("agent_recon", "completed")
        agents.say("agent_root", "[MOCK] Recon finished. Spawning validators for the promising leads.")

        findings: list[dict] = []
        if "clean" not in directives:
            for i, (sev, title, endpoint, method, cwe, cvss) in enumerate(_pick_templates(spec.targets), 1):
                target = spec.targets[(i - 1) % len(spec.targets)]
                finding = self._finding(i, target, sev, title, endpoint, method, cwe, cvss)
                worker = f"agent_validator_{i}"
                call = agents.call("agent_root", "create_agent", {"name": f"Validator {i}", "task": f"Validate: {title} on {endpoint}"})
                agents.spawn(worker, f"Validator {i}", "agent_root", f"[MOCK] Validate: {title} on {endpoint}")
                agents.output("agent_root", call, f"Agent 'Validator {i}' created (id: {worker}).")
                agents.say(worker, f"[MOCK] Testing {method} {endpoint} for: {title}")
                agents.run_command(worker, f"curl -i -X {method} '{target.rstrip('/')}{endpoint}'",
                                   f"HTTP/1.1 200 OK\n\n[MOCK] simulated response demonstrating: {title}")
                report = agents.call("agent_root", "create_vulnerability_report", {"title": finding["title"], "severity": sev, "target": target})
                findings.append(finding)
                self._write_artifacts(run_dir, record, findings, final=False)
                agents.output("agent_root", report, f"Report {finding['id']} recorded")
                agents.set_status(worker, "completed")
                if say(f"Finding {finding['id']} [{sev.upper()}] {finding['title']}"):
                    return interrupted()

        on_line(f"Writing report to strix_runs/{run_dir.name}/")
        agents.say("agent_root", f"[MOCK] All validators finished. {len(findings)} finding(s) recorded; writing the final report.")
        agents.call("agent_root", "finish", {"summary": f"{len(findings)} simulated finding(s)"})
        agents.stop_all("completed")
        record.update(status="completed", end_time=datetime.now(timezone.utc).isoformat())
        self._write_artifacts(run_dir, record, findings, final=True)
        on_line(f"Scan completed. {len(findings)} finding(s).")
        return RunResult(exit_code=2 if findings else 0)

    # ── helpers ──────────────────────────────────────────────────────────

    def _interrupted(self, run_dir: Path, record: dict, on_line: Callable[[str], None]) -> RunResult:
        on_line("Interrupted — cleaning up (simulated)")
        record.update(status="interrupted", end_time=datetime.now(timezone.utc).isoformat())
        self._write_json(run_dir / "run.json", record)
        return RunResult(exit_code=130, cancelled=True)

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _finding(n: int, target: str, sev: str, title: str, endpoint: str, method: str, cwe: str, cvss: float) -> dict:
        return {
            "id": f"vuln-{n:04d}",
            "title": f"[MOCK] {title}",
            "severity": sev,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "description": f"Simulated finding produced by the Oreshnik mock runner. Nothing was tested on {target}.",
            "impact": "None — this is simulated data.",
            "target": target,
            "technical_analysis": "Simulated.",
            "poc_description": "Simulated proof of concept.",
            "poc_script_code": f"```bash\ncurl -i -X {method} '{target.rstrip('/')}{endpoint}'\n```",
            "remediation_steps": "Simulated remediation advice.",
            "evidence": "Simulated evidence.",
            "confidence": "medium",
            "fix_effort": "low",
            "cvss": cvss,
            "endpoint": endpoint,
            "method": method,
            "cve": None,
            "cwe": cwe,
            "code_locations": [],
        }

    def _write_artifacts(self, run_dir: Path, record: dict, findings: list[dict], *, final: bool) -> None:
        (run_dir / "vulnerabilities").mkdir(exist_ok=True)
        for f in findings:
            (run_dir / "vulnerabilities" / f"{f['id']}.md").write_text(
                f"# {f['title']}\n\n**ID:** {f['id']}\n**Severity:** {f['severity'].upper()}\n\n"
                f"## Description\n\n{f['description']}\n",
                encoding="utf-8",
            )
        self._write_json(run_dir / "vulnerabilities.json", findings)
        self._write_json(run_dir / "run.json", record)
        if final:
            lines = ["# Security Penetration Test Report", "", "**[MOCK] simulated report — no real testing took place.**", ""]
            lines += ["## Targets", ""] + [f"- {t['original']}" for t in record["targets_info"]] + [""]
            lines += [f"## Findings ({len(findings)})", ""]
            lines += [f"- **{f['severity'].upper()}** {f['title']} (`{f['id']}`)" for f in findings] or ["No findings."]
            (run_dir / "penetration_test_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            sarif = {
                "version": "2.1.0",
                "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                "runs": [
                    {
                        "tool": {"driver": {"name": "Strix (mock)", "rules": []}},
                        "results": [
                            {
                                "ruleId": f["id"],
                                "level": {"critical": "error", "high": "error", "medium": "warning"}.get(f["severity"], "note"),
                                "message": {"text": f["title"]},
                            }
                            for f in findings
                        ],
                    }
                ],
            }
            self._write_json(run_dir / "findings.sarif", sarif)
