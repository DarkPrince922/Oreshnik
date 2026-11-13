"""Continuing a finished scan with `strix --resume`.

Strix resumes a run by name from `./strix_runs/<name>/` of the working directory. It needs three things
there (verified in strix-agent 1.7.0, `load_resume_state` and `core/runner.py`):

    run.json                 targets, scan mode, usage so far
    .state/agents.json       the agent tree (needs a root agent)
    .state/agents.db         every agent's conversation history

The continuation is a NEW scan with its own working directory: the parent's run directory is *copied*
into it, so the parent keeps its results untouched and one scan can be continued in several directions.
"""

import re
import shutil
from pathlib import Path

from app.services.scan_files import scan_dir

RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
REQUIRED = ("run.json", ".state/agents.json", ".state/agents.db")


class ResumeError(Exception):
    """A user-facing reason why a scan cannot be continued."""


def _source(parent_id: int, run_name: str) -> Path:
    if not RUN_NAME_RE.match(run_name or ""):
        raise ResumeError("Недопустимое имя прогона")
    base = (scan_dir(parent_id) / "strix_runs").resolve()
    src = (base / run_name).resolve()
    if base not in src.parents:  # defence in depth; the regex already forbids separators
        raise ResumeError("Недопустимое имя прогона")
    return src


def check_resumable(parent_id: int, run_name: str | None) -> None:
    """Raise ResumeError unless the parent's run data is all there."""
    if not run_name:
        raise ResumeError("У скана нет данных прогона: он не дошёл до запуска Strix")
    src = _source(parent_id, run_name)
    missing = [name for name in REQUIRED if not (src / name).is_file()]
    if missing:
        raise ResumeError(
            "Продолжить нельзя: нет файлов прогона (" + ", ".join(missing) + "). "
            "Скан удалён, или Strix не успел записать состояние агентов."
        )


def stage_run(parent_id: int, run_name: str, work_dir: Path) -> Path:
    """Copy the parent's run directory into `work_dir/strix_runs/<run_name>` and return it."""
    check_resumable(parent_id, run_name)
    src = _source(parent_id, run_name)
    dst = work_dir / "strix_runs" / run_name
    if dst.exists():
        raise ResumeError("Каталог прогона уже существует")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # symlinks=True: copy links as links, never follow one out of the run directory.
    shutil.copytree(src, dst, symlinks=True)
    return dst
