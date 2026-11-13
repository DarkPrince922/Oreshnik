#!/usr/bin/env python3
"""A stand-in for `codex exec [resume <thread>] --json ... -- <prompt>`: prints the same JSONL events the real Codex 0.169
printed in the check against a fake model (thread.started, item.completed/agent_message, turn.completed, turn.failed).

The prompt picks the behaviour:  "hang" = start a command and never finish (until SIGINT), "fail" = turn.failed + exit 1,
"report" = write what this process was given (argv, the env vars that matter) into an agent_message.
"""
import json
import os
import signal
import sys
import time

argv = sys.argv[1:]
prompt = argv[argv.index("--") + 1] if "--" in argv else ""
resume = "resume" in argv[:2]
thread = None


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


if resume:
    # `-c key=value` pairs precede the thread id: find the first bare token that is not the value of a -c
    rest = argv[argv.index("resume") + 1:]
    skip = False
    for tok in rest:
        if skip:
            skip = False
            continue
        if tok == "-c":
            skip = True
            continue
        if tok.startswith("-"):
            continue
        thread = tok
        break
else:
    thread = "thread-" + str(os.getpid())

emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})

if prompt == "hang":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    emit({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "sleep 1000", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    time.sleep(60)
elif prompt == "fail":
    emit({"type": "error", "message": "boom"})
    emit({"type": "turn.failed", "error": {"message": "boom"}})
    sys.exit(1)
elif prompt == "report":
    text = json.dumps({"argv": argv, "key_env": os.environ.get("ORESHNIK_AGENT_API_KEY"), "codex_home": os.environ.get("CODEX_HOME"),
                       "cwd": os.getcwd(), "leaked": sorted(k for k in os.environ if k in ("JWT_SECRET", "SECRET_ENCRYPTION_KEY", "POSTGRES_PASSWORD"))})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
elif prompt == "bigout":
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "yes", "aggregated_output": "y\n" * 50000, "exit_code": 0, "status": "completed"}})
else:
    emit({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "echo hi", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "echo hi", "aggregated_output": "hi\n", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": f"echo: {prompt}"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 3, "reasoning_output_tokens": 0}})
