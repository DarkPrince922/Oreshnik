"""Repository targets of a run that is continued: their clone, put back when it is gone.

Strix 1.7.0 clones a repository target into `$TMPDIR/strix_repos/<run>/<name>` and records that path in run.json
(`targets_info[].details.cloned_repo_path`). On `--resume` it does not clone again: it refuses with "cloned repo at … is missing"
when the directory is gone. And it is gone often: the worker container was rebuilt (`./install.sh`), the server restarted, or the scan
ran on an SSH runner, whose scan directory is cleared before every run. So before Strix is started on a continuation, this script
clones every missing repository again (into the scan's own temporary directory) and points run.json at the new copy.

It is a standalone script (run with the panel's python locally, with the host's python3 over SSH), so both runners do the same thing.
Arguments: the run directory, the temporary directory. It never fails the scan: what it cannot do it reports and leaves to Strix.
"""

RESTORE_CLONES_PY = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path

run_dir, tmp = Path(sys.argv[1]), Path(sys.argv[2])
record_path = run_dir / "run.json"
try:
    record = json.loads(record_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
changed = False
for target in record.get("targets_info") or []:
    if not isinstance(target, dict) or target.get("type") != "repository":
        continue
    details = target.get("details") or {}
    url, old = details.get("target_repo"), details.get("cloned_repo_path")
    if not url or (old and Path(old).expanduser().exists()):
        continue
    name = details.get("workspace_subdir") or (Path(url).stem if url.endswith(".git") else Path(url).name)
    if not name or "/" in name or name in (".", ".."):
        continue
    dest = tmp / "strix_repos" / run_dir.name / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[oreshnik] Копии репозитория {url} нет (сервер перезапускался или скан на другом хосте) — скачиваю заново", flush=True)
    done = subprocess.run(["git", "clone", "--", url, str(dest)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        print(f"[oreshnik] Не удалось скачать {url}: {(done.stderr or '').strip()[-300:]}", flush=True)
        continue
    details["cloned_repo_path"] = str(dest.resolve())
    changed = True
if changed:
    fresh = record_path.with_name("run.json.oreshnik")
    fresh.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(fresh, record_path)
'''
