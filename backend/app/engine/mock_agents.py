"""Writes simulated agent activity in Strix's real on-disk format.

`<run>/.state/agents.json` and `<run>/.state/agents.db` (table `agent_messages`, WAL) are exactly what
strix-agent 1.7.0 produces — the DDL below was taken from a database written by the real library — so
the activity tailer, API and UI are exercised the same way as with a real scan.
"""

import json
import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES agent_sessions (session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session_id ON agent_messages (session_id, id);
"""

TERMINAL = {"completed", "stopped", "crashed", "failed"}


class MockAgents:
    def __init__(self, run_dir: Path):
        self.state_dir = run_dir / ".state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.state_dir / "agents.db", isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self.statuses: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.parent_of: dict[str, str | None] = {}
        self.errors: dict[str, str] = {}
        # Continuing an existing run: pick up its roster and keep call ids unique.
        roster = self.state_dir / "agents.json"
        if roster.is_file():
            data = json.loads(roster.read_text(encoding="utf-8"))
            self.statuses.update(data.get("statuses") or {})
            self.names.update(data.get("names") or {})
            self.parent_of.update(data.get("parent_of") or {})
            self.errors.update(data.get("errors") or {})
        self._calls = self._conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    # -- roster
    def _flush_roster(self) -> None:
        data = {"statuses": self.statuses, "names": self.names, "parent_of": self.parent_of, "errors": self.errors}
        tmp = self.state_dir / "agents.json.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.state_dir / "agents.json")

    def spawn(self, agent_id: str, name: str, parent: str | None, task: str) -> None:
        self.statuses[agent_id], self.names[agent_id], self.parent_of[agent_id] = "running", name, parent
        self._flush_roster()
        self._conn.execute("INSERT OR IGNORE INTO agent_sessions (session_id) VALUES (?)", (agent_id,))
        self._add(agent_id, {"role": "user", "content": task})  # an agent's first user turn is its task

    def set_status(self, agent_id: str, status: str, error: str | None = None) -> None:
        if self.statuses.get(agent_id) in TERMINAL:
            return
        self.statuses[agent_id] = status
        if error:
            self.errors[agent_id] = error
        self._flush_roster()

    def reopen(self, agent_id: str) -> None:
        """A finished agent picks work up again (what `--resume` does to the root agent)."""
        self.statuses[agent_id] = "running"
        self.errors.pop(agent_id, None)
        self._flush_roster()

    def user(self, agent_id: str, text: str) -> None:
        """A message typed by the user — Strix stores it as a plain user turn."""
        self._add(agent_id, {"role": "user", "content": text})

    def stop_all(self, status: str, error: str | None = None) -> None:
        for agent_id in list(self.statuses):
            self.set_status(agent_id, status, error)

    # -- transcript
    def _add(self, agent_id: str, item: dict) -> None:
        self._conn.execute("INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)", (agent_id, json.dumps(item)))

    def say(self, agent_id: str, text: str) -> None:
        self._add(agent_id, {"role": "assistant", "content": [{"type": "output_text", "text": text}]})

    def call(self, agent_id: str, tool: str, args: dict) -> str:
        self._calls += 1
        call_id = f"call_mock_{self._calls}"
        self._add(agent_id, {"type": "function_call", "call_id": call_id, "name": tool, "arguments": json.dumps(args)})
        return call_id

    def output(self, agent_id: str, call_id: str, text: str) -> None:
        self._add(agent_id, {"type": "function_call_output", "call_id": call_id, "output": text})

    def run_command(self, agent_id: str, cmd: str, result: str) -> None:
        self.output(agent_id, self.call(agent_id, "exec_command", {"cmd": cmd, "tty": False}), result)
