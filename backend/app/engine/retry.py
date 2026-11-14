"""Starting a scan again when Docker could not bind the sandbox's host port.

Strix asks dockerd for a random host port for the sandbox's Caido ("127.0.0.1", None); dockerd occasionally hands out one that
is already taken (the same range is used for outgoing connections), and the container then fails to start with
"failed to bind host port 127.0.0.1:N/tcp: address already in use". Nothing of the scan has run at that point, so starting it
again is safe and almost always succeeds.
"""

import logging
import re
import time
from collections.abc import Callable

from app.core.config import settings
from app.engine.base import RunResult, Runner, ScanSpec

log = logging.getLogger("oreshnik.engine")

PORT_CLASH_RE = re.compile(r"failed to bind host port.*address already in use|port is already allocated", re.IGNORECASE)
RETRY_DELAY_SECONDS = 3.0


def run_with_port_retry(
    engine: Runner,
    spec: ScanSpec,
    on_line: Callable[[str], None],
    should_cancel: Callable[[], bool],
    *,
    retries: int | None = None,
    delay: float = RETRY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    left = settings.sandbox_start_retries if retries is None else retries
    attempt = 1
    while True:
        clash = False

        def watch(line: str) -> None:
            nonlocal clash
            if PORT_CLASH_RE.search(line):
                clash = True
            on_line(line)

        result = engine.run(spec, watch, should_cancel)
        failed = result.exit_code != 0 and result.error is None
        if not (clash and failed) or result.cancelled or result.timed_out or left <= 0 or should_cancel():
            return result
        left -= 1
        attempt += 1
        on_line(f"[oreshnik] Docker не смог занять порт хоста для sandbox (адрес уже используется) — повторный запуск, попытка {attempt}")
        log.warning("scan %s: sandbox port clash, starting again (attempt %d)", spec.scan_id, attempt)
        sleep(delay)
