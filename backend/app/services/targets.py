"""Scan target validation.

Strix resolves a target string itself (`strix.interface.utils.infer_target_type`),
and one of its branches is dangerous for a multi-user panel: any string that
happens to be an existing path on the worker is mounted into the sandbox
**writable** and handed to the LLM (`/etc`, `~`, `/root/.ssh` ...).

Relative names (`openapi.yaml`, `app.zip`) look like domains and are handled by Strix as
`https://<name>` because the run's working directory is a private, empty per-scan folder,
so they can never reach a real file.

So targets fall into two classes:

* REMOTE — URL, git repository, domain, IP. Safe for any operator.
* PATH   — anything else, which Strix may treat as a local path. Admin only.

Everything that is accepted is also guaranteed not to start with "-" and to
contain no whitespace/control characters. (The runner additionally passes it as
`--target=<value>`, so it can never be read as an option.)
"""

import enum
import ipaddress
import re
from urllib.parse import urlparse

MAX_TARGET_LEN = 2048

_HOST_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(rf"^{_HOST_LABEL}(\.{_HOST_LABEL})+(:\d{{1,5}})?$")
_HOST_PATH_RE = re.compile(rf"^{_HOST_LABEL}(\.{_HOST_LABEL})+(:\d{{1,5}})?/\S*$")
_GIT_SSH_RE = re.compile(r"^git@[A-Za-z0-9.-]+:[^\s]+$")


class TargetKind(str, enum.Enum):
    remote = "remote"
    path = "path"


def classify_target(raw: str) -> tuple[str, TargetKind]:
    """Return (normalised target, kind) or raise ValueError with a user-facing reason."""
    target = raw.strip() if isinstance(raw, str) else ""
    if not target:
        raise ValueError("цель не может быть пустой")
    if len(target) > MAX_TARGET_LEN:
        raise ValueError(f"цель длиннее {MAX_TARGET_LEN} символов")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in target):
        raise ValueError("цель не должна содержать пробелы и управляющие символы")
    if target.startswith("-"):
        raise ValueError("цель не может начинаться с «-»")

    if _GIT_SSH_RE.match(target) or target.startswith("git://"):
        return target, TargetKind.remote

    # host[:port] and host[:port]/path must be tested before urlparse, which would
    # read "example.com:8080" as scheme "example.com".
    if _DOMAIN_RE.match(target) or _HOST_PATH_RE.match(target):
        # Strix tries `Path(target).exists()` before treating "host.tld/x" as a URL, so a
        # traversal such as "a.b/../../etc" must never ride on this branch.
        if any(seg in ("..", ".") for seg in target.split("/")[1:]):
            raise ValueError("«.» и «..» в пути цели не допускаются")
        return target, TargetKind.remote

    try:
        ipaddress.ip_address(target)
        return target, TargetKind.remote
    except ValueError:
        pass

    parsed = urlparse(target)
    if parsed.scheme in ("http", "https"):
        if not parsed.hostname:
            raise ValueError("в URL не указан хост")
        return target, TargetKind.remote
    if parsed.scheme == "postman":
        raise ValueError("postman:// пока не поддерживается панелью (нужен POSTMAN_API_KEY)")
    if "://" in target:
        raise ValueError(f"схема «{parsed.scheme}://» не поддерживается")

    return target, TargetKind.path


def classify_all(raw_targets: list[str]) -> list[tuple[str, TargetKind]]:
    """Classify every target, dropping exact duplicates while keeping order."""
    seen: set[str] = set()
    out: list[tuple[str, TargetKind]] = []
    for raw in raw_targets:
        target, kind = classify_target(raw)
        if target not in seen:
            seen.add(target)
            out.append((target, kind))
    return out
