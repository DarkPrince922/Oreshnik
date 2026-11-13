"""Operational tasks: a goal in plain words, turned by an LLM into a POSIX shell script, run as root over SSH on
one or more existing (SSH) runners — by the owner's explicit choice, with no approval step before it runs. Two
things are still enforced, not optional, and both independent of that choice:

  * The exact script is always stored and shown — nothing the LLM wrote is ever invisible.
  * A short, high-precision guard refuses the handful of one-liners that destroy a whole machine outright
    (a fork bomb, `rm -rf /`, formatting or overwriting a raw disk). It is not a review step and does not
    second-guess ordinary admin commands (reinstalling a package, restarting a service, deleting a directory
    under /var/log) — only the handful of patterns that have no legitimate "oops, that was on purpose" reading.
"""

import logging
import re
import shlex

from app.core.crypto import decrypt
from app.models.llm_profile import LLMProfile
from app.models.runner import Runner
from app.services.llm import LlmCallError, chat_once
from app.services.ssh import CommandResult, SshConnection, SshError, SshTarget
from app.services.ssh_runner import target_for

log = logging.getLogger("oreshnik.ops")

# Capped like the installation log (services/provision.py): long enough to read a real failure, short enough that
# a chatty command cannot fill the database.
OUTPUT_LIMIT = 200_000

DRAFT_SYSTEM_PROMPT = """You write a single POSIX `sh` script for a Linux sysadmin task. Output ONLY the script \
itself: no markdown code fences, no explanation before or after, no comments about what you are about to do.

Rules for the script:
- Start with `#!/bin/sh` and `set -eu`.
- It runs as root, non-interactively, with no terminal attached: never prompt for input or wait on a confirmation.
- Prefer idempotent commands (safe to run twice) and check before you act (e.g. test a package is installed \
before installing it, test a service exists before restarting it).
- Print what you are doing as you go (plain `echo`), so the operator can read what happened from the output alone.
- If a precondition the task needs is not met, print why and exit non-zero rather than guessing.
- Never run a destructive command on an entire disk or filesystem, and never disable the only way to reach the \
host (its firewall's default policy, its own SSH access) — scope every action to what the task actually asked for.
"""


class OpsError(Exception):
    """A human-readable reason the task could not even start (no host was touched)."""


def build_user_prompt(goal: str, runner_names: list[str]) -> str:
    plural = "хосте" if len(runner_names) == 1 else f"{len(runner_names)} хостах"
    return f"Задача: {goal}\n\nСкрипт выполнится на {plural}: {', '.join(runner_names)}."


def strip_code_fence(text: str) -> str:
    """Models are told not to wrap the script in a fence, and mostly don't — but some do anyway."""
    body = text.strip()
    match = re.match(r"^```[A-Za-z0-9_-]*\n(.*)\n```\s*$", body, re.DOTALL)
    return match.group(1).strip() if match else body


def draft_script(profile: LLMProfile, goal: str, runner_names: list[str]) -> str:
    """Ask the profile's model to write the script. Raises `OpsError` with a message safe to show the operator."""
    try:
        reply = chat_once(
            profile.provider,
            profile.base_url,
            decrypt(profile.api_key_enc),
            profile.model,
            system=DRAFT_SYSTEM_PROMPT,
            user=build_user_prompt(goal, runner_names),
        )
    except LlmCallError as exc:
        raise OpsError(f"Модель не смогла написать скрипт: {exc}") from exc
    script = strip_code_fence(reply)
    if not script.strip():
        raise OpsError("Модель вернула пустой скрипт")
    return script


# ── the safety guard ───────────────────────────────────────────────────────
#
# Deliberately short and high-precision: each pattern below has no legitimate "oops, meant to do that" reading on
# a real host, so false positives on ordinary admin scripts should be ~zero. This is not a substitute for review —
# there is none — only a backstop against the single worst class of mistake (a hallucinated or mistyped one-liner
# that destroys the whole machine instead of the part the task was actually about).

_CATASTROPHIC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork-бомба"),
    (re.compile(r"\bmkfs(\.\w+)?\s"), "форматирование диска (mkfs)"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(disk\d|[sh]d[a-z]|nvme\d+n\d+|vd[a-z]|xvd[a-z])\b"), "побайтовая перезапись целого диска (dd of=/dev/...)"),
    (re.compile(r">\s*/dev/(disk\d|[sh]d[a-z]|nvme\d+n\d+|vd[a-z]|xvd[a-z])\b"), "перезапись целого диска (> /dev/...)"),
    (re.compile(r"\bwipefs\b"), "стирание разметки диска (wipefs)"),
)

_CMD_SEP_RE = re.compile(r"[;&|]+")
_ROOT_PATH_RE = re.compile(r"^/+\*?$")  # "/", "//", "/*", ... — the whole filesystem, not a path under it


def _candidate_commands(script: str) -> list[str]:
    """A rough split into one-command chunks — good enough for the guard below, not a shell parser."""
    commands = []
    for line in script.splitlines():
        code = line.split("#", 1)[0]  # drop a line comment (misses one inside quotes, which costs nothing here)
        commands += [part.strip() for part in _CMD_SEP_RE.split(code) if part.strip()]
    return commands


def _rm_targets_whole_filesystem(command: str) -> bool:
    """`rm` with both -r and -f (any spelling, any order, short-bundled or long) whose only remaining argument is
    "/" (or "/*", "//", ...) — or that carries --no-preserve-root at all, which exists for no other reason."""
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        return False
    if not tokens or tokens[0].rsplit("/", 1)[-1] != "rm":
        return False
    recursive = force = no_preserve_root = False
    paths: list[str] = []
    for tok in tokens[1:]:
        if tok == "--":
            continue
        if tok == "--recursive":
            recursive = True
        elif tok == "--force":
            force = True
        elif tok == "--no-preserve-root":
            no_preserve_root = True
        elif tok.startswith("-") and len(tok) > 1 and not tok.startswith("--"):
            recursive = recursive or "r" in tok or "R" in tok
            force = force or "f" in tok
        elif not tok.startswith("--"):
            paths.append(tok)
    if not (recursive and force):
        return False
    if no_preserve_root and paths:
        return True
    return any(_ROOT_PATH_RE.match(p) for p in paths)


def looks_catastrophic(script: str) -> str | None:
    """A human-readable reason to refuse `script` outright, or None if the guard has nothing to say about it."""
    for pattern, reason in _CATASTROPHIC_PATTERNS:
        if pattern.search(script):
            return reason
    if any(_rm_targets_whole_filesystem(cmd) for cmd in _candidate_commands(script)):
        return "удаление корня файловой системы (rm -rf /)"
    return None


# ── running the approved script on one host ─────────────────────────────────

HOST_TIMEOUT_SECONDS = 600.0


def run_on_host(runner: Runner, script: str, *, timeout: float = HOST_TIMEOUT_SECONDS) -> tuple[str | None, int | None, str]:
    """Run `script` on `runner` over SSH. Returns (status, exit_code, output); `status` is one of
    "succeeded" / "failed" / "unreachable" — never raises for a reachable host that simply failed its script."""
    try:
        target: SshTarget = target_for(runner)
        with SshConnection(target) as conn:
            result: CommandResult = conn.run("sh -s", input=script, timeout=timeout)
    except SshError as exc:
        return "unreachable", None, exc.message

    output = result.stdout
    if result.stderr:
        output += ("\n" if output and not output.endswith("\n") else "") + "--- stderr ---\n" + result.stderr
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT] + "\n[вывод обрезан]\n"
    status = "succeeded" if result.status == 0 else "failed"
    return status, result.status, output
