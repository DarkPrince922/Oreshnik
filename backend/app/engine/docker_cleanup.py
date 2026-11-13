"""Removal of sandbox containers left behind by a Strix run.

Strix tags every sandbox container with the label `strix-run-id=<STRIX_RUN_ID>`
(strix/runtime/docker_client.py). The runner sets STRIX_RUN_ID to a per-scan label, so
leftovers of a killed/crashed scan can be found precisely and nothing else is touched.
"""

import logging
import shutil
import subprocess

log = logging.getLogger("oreshnik.docker")


def run_label(scan_id: int) -> str:
    return f"oreshnik-scan-{int(scan_id)}"


def cleanup_containers(label: str) -> int:
    """Force-remove all containers carrying the scan label. Best effort; returns the count."""
    docker = shutil.which("docker")
    if not docker:
        return 0
    try:
        listed = subprocess.run(
            [docker, "ps", "-aq", "--filter", f"label=strix-run-id={label}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ids = listed.stdout.split()
        if not ids:
            return 0
        subprocess.run([docker, "rm", "-f", *ids], capture_output=True, timeout=120)
        log.info("removed %d leftover container(s) for %s", len(ids), label)
        return len(ids)
    except Exception as exc:  # docker missing/unreachable — never fail the scan over cleanup
        log.warning("container cleanup for %s failed: %s", label, exc)
        return 0
