"""Starting a scan again by itself when it failed for a reason that passes on its own, before anything of it had run.

Two such reasons:

* **Docker could not bind the sandbox's host port.** Strix asks dockerd for a random host port for the sandbox's Caido
  ("127.0.0.1", None); dockerd occasionally hands out one that is already taken, and the container fails to start with
  "failed to bind host port 127.0.0.1:N/tcp: address already in use". A moment later it works.
* **The model was unreachable for a moment.** Before a scan Strix sends the model one tiny request; a gateway that answers 502,
  is overloaded or does not answer in time makes Strix print "LLM CONNECTION FAILED" and exit. A minute or two later it
  usually answers again.

In both cases Strix exits before the scan itself begins, so starting it again is safe. Anything that will not pass by waiting — a
wrong key (401/403), an unknown model (404), a spent quota — is NOT retried: it is reported at once.
"""

import logging
import re
import time
from collections.abc import Callable

from app.core.config import settings
from app.engine.base import RunResult, Runner, ScanSpec

log = logging.getLogger("oreshnik.engine")

PORT_CLASH_RE = re.compile(r"failed to bind host port.*address already in use|port is already allocated", re.IGNORECASE)
PORT_RETRY_DELAY_SECONDS = 3.0

# Strix's own heading for a failed pre-scan request to the model; the reason follows on the next lines of its box.
LLM_FAILED_RE = re.compile(r"LLM CONNECTION FAILED", re.IGNORECASE)
# Reasons that pass by waiting: gateway and server errors, overload, rate limits, timeouts, a dropped connection.
# The HTTP codes among them are a setting (`llm_retry_codes`, editable in the panel); the words below are fixed.
LLM_CODE_RE = re.compile(r"error code:\s*(\d{3})\b", re.IGNORECASE)
LLM_TRANSIENT_RE = re.compile(
    r"bad[ _]gateway|gateway[ _]time-?out|service[ _]unavailable|internal[ _]server[ _]error|bad_response_status_code"
    r"|overloaded|rate[ _-]?limit|too many requests|temporarily|try again"
    r"|timed? ?out|timeout|connection (error|reset|refused|aborted|closed)|connecterror|readerror|remoteprotocolerror"
    r"|server disconnected|name or service not known|temporary failure in name resolution",
    re.IGNORECASE,
)
# Reasons that do not: waiting will not fix a key, a model name or an empty balance (a 429 can be either — these words decide).
LLM_PERMANENT_RE = re.compile(
    r"error code:\s*(400|401|402|403|404|422)\b|insufficient[ _]quota|exceeded your current quota|billing|invalid[ _]api[ _]key"
    r"|incorrect api key|authentication|unauthori[sz]ed|permission|model[ _]not[ _]found|does not exist|not available on",
    re.IGNORECASE,
)
_LLM_REASON_LINES = 14  # how much of Strix's box after the heading is read for the reason


def retry_codes() -> set[str]:
    """The HTTP codes worth another try, as the admin listed them (`llm_retry_codes`: "502, 503, 499 ...")."""
    return set(re.findall(r"\d{3}", settings.llm_retry_codes or ""))


def llm_failure_is_transient(reason: str) -> bool:
    """True when the text Strix printed under "LLM CONNECTION FAILED" names a reason that passes on its own."""
    if LLM_PERMANENT_RE.search(reason):
        return False
    codes = set(LLM_CODE_RE.findall(reason))
    if codes:  # the gateway named a code: the list decides, whatever else the message says
        return bool(codes & retry_codes())
    return bool(LLM_TRANSIENT_RE.search(reason))


def _wait(seconds: float, should_cancel: Callable[[], bool], sleep: Callable[[float], None]) -> bool:
    """Wait, staying stoppable (and keeping the heartbeat: `should_cancel` is also that). False when the scan was stopped."""
    left = seconds
    while True:
        if should_cancel():
            return False
        if left <= 0:
            return True
        step = min(1.0, left)
        sleep(step)
        left -= step


def run_with_retries(
    engine: Runner,
    spec: ScanSpec,
    on_line: Callable[[str], None],
    should_cancel: Callable[[], bool],
    *,
    port_retries: int | None = None,
    llm_retries: int | None = None,
    port_delay: float = PORT_RETRY_DELAY_SECONDS,
    llm_delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    port_left = settings.sandbox_start_retries if port_retries is None else port_retries
    llm_left = settings.llm_retry_attempts if llm_retries is None else llm_retries
    llm_wait = float(settings.llm_retry_delay_seconds if llm_delay is None else llm_delay)
    attempt = 1
    while True:
        clash = False
        llm_lines: list[str] | None = None  # None until Strix's heading is seen, then the lines under it

        def watch(line: str) -> None:
            nonlocal clash, llm_lines
            if PORT_CLASH_RE.search(line):
                clash = True
            if llm_lines is not None and len(llm_lines) < _LLM_REASON_LINES:
                llm_lines.append(line)
            elif llm_lines is None and LLM_FAILED_RE.search(line):
                llm_lines = [line]
            on_line(line)

        result = engine.run(spec, watch, should_cancel)
        failed = result.exit_code != 0 and result.error is None and not result.cancelled and not result.timed_out
        if not failed:
            return result

        if clash and port_left > 0:
            port_left -= 1
            attempt += 1
            on_line(f"[oreshnik] Docker не смог занять порт хоста для sandbox (адрес уже используется) — повторный запуск, попытка {attempt}")
            log.warning("scan %s: sandbox port clash, starting again (attempt %d)", spec.scan_id, attempt)
            if not _wait(port_delay, should_cancel, sleep):
                result.cancelled = True
                return result
            continue

        # Strix wraps its box at the terminal width, so the reason is read from the lines joined back together.
        if llm_lines is not None and llm_left > 0 and llm_failure_is_transient(" ".join(llm_lines)):
            llm_left -= 1
            attempt += 1
            on_line(f"[oreshnik] Модель временно недоступна — повторный запуск через {llm_wait:g} с, попытка {attempt}")
            log.warning("scan %s: the model was unreachable, starting again in %gs (attempt %d)", spec.scan_id, llm_wait, attempt)
            if not _wait(llm_wait, should_cancel, sleep):
                result.cancelled = True
                return result
            continue

        return result


# The earlier name, kept for callers and tests written against it.
def run_with_port_retry(engine, spec, on_line, should_cancel, *, retries=None, delay=PORT_RETRY_DELAY_SECONDS, sleep=time.sleep):
    return run_with_retries(engine, spec, on_line, should_cancel, port_retries=retries, llm_retries=0, port_delay=delay, sleep=sleep)
