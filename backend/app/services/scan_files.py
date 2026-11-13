"""On-disk layout and log handling for scans.

    <STRIX_RUNS_DIR>/scan-<id>/
        oreshnik.log        panel-side log (Strix stdout, ANSI-stripped, secrets redacted)
        instruction.txt     custom instructions handed to Strix via --instruction-file
        home/               private HOME for the Strix process (no config shared between scans)
        strix_runs/<name>/  Strix's own run directory (run.json, vulnerabilities.json, ...)

The API and the worker share STRIX_RUNS_DIR (a volume in docker-compose). The log
file is the single source of truth for live streaming: the worker appends, the API
tails — no extra broker involved.
"""

import re
import shutil
from pathlib import Path

from app.core.config import settings

LOG_NAME = "oreshnik.log"
INSTRUCTION_NAME = "instruction.txt"
ACTIVITY_NAME = "activity.jsonl"   # normalised agent events (see services/activity.py)
AGENTS_NAME = "agents.json"        # normalised agent roster + statuses
MAX_LINE_CHARS = 8000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Rich draws panels with box characters; they carry no information for a status message.
_BOX_RE = re.compile(r"[│┃╭╮╰╯─━┌┐└┘├┤┬┴┼║╔╗╚╝═]+")


def scan_dir(scan_id: int) -> Path:
    # Built from the integer id only — never from user-controlled strings.
    return Path(settings.strix_runs_dir) / f"scan-{int(scan_id)}"


def log_path(scan_id: int) -> Path:
    return scan_dir(scan_id) / LOG_NAME


def activity_path(scan_id: int) -> Path:
    return scan_dir(scan_id) / ACTIVITY_NAME


def agents_path(scan_id: int) -> Path:
    return scan_dir(scan_id) / AGENTS_NAME


def find_run_dir(work_dir: Path) -> Path | None:
    """The newest `strix_runs/<name>` directory that contains a run.json."""
    base = work_dir / "strix_runs"
    if not base.is_dir():
        return None
    candidates = [c for c in base.iterdir() if (c / "run.json").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c / "run.json").stat().st_mtime)


def remove_scan_dir(scan_id: int) -> None:
    shutil.rmtree(scan_dir(scan_id), ignore_errors=True)


def clean_line(line: str) -> str:
    """Strip ANSI escapes and control characters; keep the last `\\r` segment (progress bars)."""
    line = _ANSI_RE.sub("", line)
    if "\r" in line:
        line = line.rsplit("\r", 1)[-1] or line.replace("\r", "")
    line = _CTRL_RE.sub("", line)
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + " …[обрезано]"
    return line


# How many lines of the last paragraph the failure message of a scan may carry.
MESSAGE_LINES = 5


class LogWriter:
    """Appends cleaned, redacted lines to the scan's log file (flushed per line)."""

    def __init__(self, scan_id: int, secrets: list[str] | None = None):
        path = log_path(scan_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8", errors="replace", buffering=1)
        # Redact anything that looks like a credential we injected; ignore tiny values.
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 6]
        self._bytes = path.stat().st_size
        self._truncated = False
        self.lines = 0
        self.last_line = ""
        # The last paragraph of meaningful lines (blank lines and box borders end one), a few lines at most. Strix prints its
        # failures in a bordered box wrapped at the terminal width, so the last *line* is often a tail like "reachable."
        self.last_message = ""
        self._paragraph: list[str] = []

    def add_secrets(self, secrets: list[str]) -> None:
        self._secrets += [s for s in secrets if s and len(s) >= 6]

    def write(self, raw: str) -> None:
        if self._truncated:
            return
        line = clean_line(raw)
        for secret in self._secrets:
            line = line.replace(secret, "***")
        data = line + "\n"
        if self._bytes + len(data.encode("utf-8")) > settings.scan_log_max_bytes:
            self._truncated = True
            data = "[лог обрезан: достигнут лимит размера]\n"
        self._fh.write(data)
        self._bytes += len(data.encode("utf-8"))
        self.lines += 1
        meaningful = _BOX_RE.sub(" ", line).strip()
        if re.search(r"\w{3}", meaningful):  # skip blank lines and pure borders
            self.last_line = meaningful
            self._paragraph = (self._paragraph + [meaningful])[-MESSAGE_LINES:]
            self.last_message = " ".join(self._paragraph)
        else:
            self._paragraph = []

    def close(self) -> None:
        self._fh.close()


def read_log_lines(scan_id: int, offset: int = 0, limit: int = 2000) -> tuple[list[str], int]:
    """Lines [offset, offset+limit) of the log and the next offset."""
    path = log_path(scan_id)
    if not path.is_file():
        return [], offset
    out: list[str] = []
    index = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if index >= offset:
                out.append(raw.rstrip("\n"))
                if len(out) >= limit:
                    index += 1
                    break
            index += 1
    return out, offset + len(out)


def tail_from(path: Path, position: int, max_bytes: int | None = None) -> tuple[list[str], int]:
    """Complete lines written after byte `position`; returns (lines, new position).

    `max_bytes` bounds how much is read in one call (lines are small, so at least one complete
    line always fits); the caller simply asks again from the returned position.
    """
    if not path.is_file():
        return [], position
    with path.open("rb") as fh:
        fh.seek(position)
        chunk = fh.read(max_bytes) if max_bytes else fh.read()
    end = chunk.rfind(b"\n")
    if end == -1:
        return [], position  # only a partial line so far
    complete = chunk[: end + 1]
    lines = complete.decode("utf-8", errors="replace").split("\n")[:-1]
    return lines, position + len(complete)
