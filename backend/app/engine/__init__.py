from app.engine.base import Runner, RunResult, ScanSpec
from app.engine.local import LocalRunner
from app.engine.mock import MockRunner
from app.engine.ssh import SshRunner
from app.models.runner import RunnerKind


def get_runner(kind: RunnerKind) -> Runner:
    if kind == RunnerKind.mock:
        return MockRunner()
    if kind == RunnerKind.local:
        return LocalRunner()
    if kind == RunnerKind.ssh:
        return SshRunner()
    raise NotImplementedError(f"Раннеры типа «{kind.value}» пока не поддерживаются")


__all__ = ["Runner", "RunResult", "ScanSpec", "LocalRunner", "MockRunner", "SshRunner", "get_runner"]
