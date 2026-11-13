from app.engine.base import Runner, RunResult, ScanSpec
from app.engine.local import LocalRunner
from app.engine.mock import MockRunner
from app.models.runner import RunnerKind


def get_runner(kind: RunnerKind) -> Runner:
    if kind == RunnerKind.mock:
        return MockRunner()
    if kind == RunnerKind.local:
        return LocalRunner()
    raise NotImplementedError(f"Раннеры типа «{kind.value}» пока не поддерживаются")


__all__ = ["Runner", "RunResult", "ScanSpec", "LocalRunner", "MockRunner", "get_runner"]
