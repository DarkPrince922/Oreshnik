"""Mapping of a finished run to a scan status.

Strix exit codes (headless): 0 finished, 1 error (preflight/model/Docker/unhandled), 2 findings
at/above --fail-on (default: any). Careful: argparse also exits 2 on bad arguments, so "2" is
only believed when findings were actually read back from the run directory.
"""

from app.engine.base import RunResult
from app.models.scan import ScanStatus
from app.services.artifacts import ParsedRun


def decide(
    result: RunResult,
    parsed: ParsedRun,
    early_error: str | None,
    timeout_minutes: int,
    last_line: str = "",
    results_are_fresh: bool = True,
) -> tuple[ScanStatus, str | None]:
    if result.cancelled:
        return ScanStatus.cancelled, "Остановлен пользователем"
    if early_error:
        return ScanStatus.failed, early_error
    if result.error:
        return ScanStatus.failed, result.error
    if result.timed_out:
        return ScanStatus.failed, f"Превышен лимит времени ({timeout_minutes} мин)"

    code = result.exit_code
    found = len(parsed.findings)

    if code == 0:
        if parsed.run_status is None:
            detail = "Strix завершился с кодом 0, но артефакты прогона не найдены"
        elif parsed.run_status != "completed":
            # Strix can stop cleanly on a budget/turn limit; don't present that as a full result.
            detail = f"Прогон завершился со статусом «{parsed.run_status}» — результат может быть неполным"
        else:
            detail = None
        return (ScanStatus.vulns_found if found else ScanStatus.succeeded), detail

    # `results_are_fresh` is False for a continued scan whose run.json was never touched: the findings in
    # the directory are the parent's, so an exit code 2 (which argparse also uses for startup errors) must
    # not be taken as "this run found vulnerabilities".
    if code == 2 and found and results_are_fresh:
        return ScanStatus.vulns_found, None

    detail = f"Strix завершился с кодом {code}"
    if last_line:
        detail += f": {last_line[:400]}"
    if "LLM_PREFLIGHT_TIMEOUT" in last_line:
        # Strix's one test request to the model got no answer in time. The panel and the runner are different machines:
        # a Base URL the panel can reach (the model list loads) may still be out of reach, or too slow, from the runner.
        detail += (
            " — модель не ответила на проверочный запрос. Проверьте, что до Base URL профиля достаёт именно раннер "
            "(не только панель) и что модель отвечает быстро; медленной модели можно дать больше времени "
            "настройкой LLM_PREFLIGHT_TIMEOUT."
        )
    return ScanStatus.failed, detail
