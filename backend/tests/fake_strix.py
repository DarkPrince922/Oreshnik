#!/usr/bin/env python3
"""A stand-in for the `strix` CLI used to exercise LocalRunner with real subprocesses.

The behaviour is chosen by the content of the file passed via --instruction-file
(exactly the way a user's instruction reaches the real CLI):

    ok_clean | ok_findings | fail | argerror | no_artifacts | slow | ignore_sigint | spawn_child
    | secret_echo | ansi
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

argv = sys.argv[1:]
Path("argv.json").write_text(json.dumps({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd()}))

mode = "ok_clean"
for a in argv:
    if a.startswith("--instruction-file="):
        mode = Path(a.split("=", 1)[1]).read_text().strip()

now = lambda: datetime.now(timezone.utc).isoformat()
run_dir = Path("strix_runs") / "fake-run"


def write_run(status, findings=(), cost=0.42, report=True):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "fake-run", "run_name": "fake-run", "status": status, "start_time": now(), "end_time": now(),
        "llm_usage": {"requests": 3, "input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost": cost},
    }))
    (run_dir / "vulnerabilities.json").write_text(json.dumps(list(findings)))
    if report:
        (run_dir / "penetration_test_report.md").write_text("# Security Penetration Test Report\n\nfake\n")


def write_agents():
    import sqlite3
    state = run_dir / ".state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(json.dumps({
        "statuses": {"root": "completed", "child": "completed"}, "names": {"root": "Root", "child": "Child"},
        "parent_of": {"root": None, "child": "root"}, "errors": {}}))
    conn = sqlite3.connect(state / "agents.db", isolation_level=None)
    conn.executescript("""CREATE TABLE IF NOT EXISTS agent_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        message_data TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""")
    key = os.environ.get("LLM_API_KEY")
    rows = [("root", {"role": "user", "content": "scan the target"}),
            ("root", {"type": "function_call", "call_id": "c1", "name": "exec_command", "arguments": json.dumps({"cmd": "echo hi"})}),
            ("root", {"type": "function_call_output", "call_id": "c1", "output": f"hi (key={key})"}),
            ("child", {"role": "assistant", "content": [{"type": "output_text", "text": "child works"}]})]
    for agent, item in rows:
        conn.execute("INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)", (agent, json.dumps(item)))
    conn.close()


FINDINGS = [
    {"id": "vuln-0001", "title": "Weird severity", "severity": "catastrophic", "timestamp": now(), "target": "https://example.com"},
    {"id": "vuln-0002", "title": "Open redirect", "severity": "medium", "timestamp": now(), "target": "https://example.com", "cvss": 4.3},
    {"id": "vuln-0003", "title": "RCE", "severity": "CRITICAL", "timestamp": now(), "target": "https://example.com", "cvss": "9.8"},
]

print("Strix fake 1.7.0 starting", flush=True)

if mode == "ok_clean":
    write_run("completed")
    write_agents()
    print("done: nothing found", flush=True)
    sys.exit(0)
if mode == "ok_findings":
    write_run("completed", FINDINGS)
    print("done: 3 findings", flush=True)
    sys.exit(2)
if mode == "fail":
    print("DOCKER NOT AVAILABLE", flush=True)
    sys.exit(1)
if mode == "argerror":
    print("strix: error: Invalid target", flush=True)
    sys.exit(2)
if mode == "no_artifacts":
    sys.exit(0)
if mode == "secret_echo":
    print(f"debug LLM_API_KEY={os.environ.get('LLM_API_KEY')}", flush=True)
    write_run("completed")
    write_agents()
    sys.exit(0)
if mode == "ansi":
    print("\x1b[31mred\x1b[0m text", flush=True)
    print("progress 10%\rprogress 50%\rprogress 100%", flush=True)
    write_run("completed")
    sys.exit(0)

# long-running modes -------------------------------------------------------
write_run("running")

if mode == "spawn_child":
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(120)"])
    Path("child.pid").write_text(str(child.pid))

if mode in ("ignore_sigint", "spawn_child"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    def on_sigint(*_):
        print("Interrupted, cleaning up", flush=True)
        write_run("interrupted")
        sys.exit(130)
    signal.signal(signal.SIGINT, on_sigint)

i = 0
while True:
    i += 1
    print(f"working {i}", flush=True)
    time.sleep(0.1)
