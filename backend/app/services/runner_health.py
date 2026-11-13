"""Runner health checks.

Executed by the Celery worker (see app/worker/tasks.py), because the worker is
the process that actually launches scans — checking from the API container
would report the wrong machine.
"""

import os
import shutil

from app.models.runner import Runner, RunnerKind, RunnerStatus

DOCKER_SOCKET = "/var/run/docker.sock"


def check_runner(runner: Runner) -> tuple[RunnerStatus, str]:
    if runner.kind == RunnerKind.mock:
        return RunnerStatus.online, "Симуляция запуска Strix — токены не тратятся"

    if runner.kind == RunnerKind.local:
        missing = []
        if not shutil.which("strix"):
            missing.append("strix CLI")
        if not shutil.which("docker"):
            missing.append("docker CLI")
        if not os.path.exists(DOCKER_SOCKET):
            missing.append(f"docker socket ({DOCKER_SOCKET})")
        if missing:
            return RunnerStatus.offline, "Не найдено: " + ", ".join(missing)
        return RunnerStatus.online, "strix и docker доступны"

    # RunnerKind.ssh — a real connection: host key, login, Docker, Strix.
    from app.services.ssh_runner import check_ssh_runner

    result = check_ssh_runner(runner)
    return result.status, result.detail
