#!/usr/bin/env python3
"""A stand-in for `codex app-server` (stdio JSON-RPC, one JSON object per line) that behaves the way the real Codex 0.169 did in the
recorded exchanges: same message names, same item shapes, deltas while an answer is written, approval requests as server requests.

The prompt of `turn/start` picks the behaviour:
  "run a command"  a command item; asks for approval first when approvalPolicy is "untrusted"
  "patch this"     a file change (+ diff); asks for approval first when "untrusted"
  "plan"           a plan update          "ask me"   a question to the user (request_user_input)
  "slow"           a long answer that can be steered / interrupted        "fail"   the turn fails       "crash"   the process dies
  "report"         answers with a JSON of what this process and its requests were given
"""
import json
import os
import queue
import sys
import threading
import time

out_lock = threading.Lock()
inbox: "queue.Queue[dict]" = queue.Queue()
answers: dict = {}            # server request id -> (event, response)
state = {"turn": None, "interrupt": threading.Event(), "steers": [], "seen": {}, "counter": 0, "goal": None}
threads_file = os.path.join(os.environ.get("CODEX_HOME", "."), "fake_threads.json")


def known_threads() -> dict:
    try:
        return json.load(open(threads_file))
    except (OSError, ValueError):
        return {}


def save_thread(thread_id: str, data: dict) -> None:
    threads = known_threads()
    threads[thread_id] = data
    os.makedirs(os.path.dirname(threads_file), exist_ok=True)
    json.dump(threads, open(threads_file, "w"))


def send(message: dict) -> None:
    with out_lock:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def notify(method: str, params: dict) -> None:
    send({"method": method, "params": params})


def new_id(prefix: str) -> str:
    state["counter"] += 1
    return f"{prefix}-{os.getpid()}-{state['counter']}"


def ask(method: str, params: dict, timeout: float = 30.0):
    """A server request: the process waits for the client's answer."""
    request_id = state["counter"] = state["counter"] + 1
    request_id = f"{request_id}"
    event = threading.Event()
    answers[request_id] = [event, None]
    send({"id": int(request_id), "method": method, "params": params})
    event.wait(timeout)
    reply = answers.pop(request_id)[1]
    notify("serverRequest/resolved", {"threadId": params.get("threadId"), "requestId": int(request_id)})
    return reply


def thread_obj(thread_id: str) -> dict:
    return {"id": thread_id, "cwd": os.getcwd(), "status": {"type": "idle"}, "turns": []}


def item_started(thread, turn, item):
    notify("item/started", {"item": item, "threadId": thread, "turnId": turn})


def item_completed(thread, turn, item):
    notify("item/completed", {"item": item, "threadId": thread, "turnId": turn})


def run_turn(thread: str, turn_id: str, text: str, params: dict) -> None:
    state["turn"] = turn_id
    state["interrupt"].clear()
    cwd = params.get("cwd") or os.getcwd()
    notify("turn/started", {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress", "items": []}})
    if state["seen"].get("model") == "unknown-model":
        notify("warning", {"threadId": thread, "message": "Model metadata for `unknown-model` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."})
    item_started(thread, turn_id, {"type": "userMessage", "id": new_id("um"), "content": [{"type": "text", "text": text}]})
    status, error = "completed", None
    policy = state["seen"].get("approvalPolicy")

    if "crash" in text:
        os._exit(3)
    if "report" in text:
        body = json.dumps({"argv": sys.argv[1:], "key_env": os.environ.get("ORESHNIK_AGENT_API_KEY"), "codex_home": os.environ.get("CODEX_HOME"), "cwd": os.getcwd(),
                           "leaked": sorted(k for k in os.environ if k in ("JWT_SECRET", "SECRET_ENCRYPTION_KEY", "POSTGRES_PASSWORD")), "seen": state["seen"], "pid": os.getpid()})
        item_completed(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": body})
    elif "run a command" in text:
        command = "echo hi-from-agent"
        item = {"type": "commandExecution", "id": "c1", "command": f'/bin/bash -lc "{command}"', "cwd": cwd, "status": "inProgress",
                "commandActions": [{"type": "unknown", "command": command}], "aggregatedOutput": None, "exitCode": None}
        item_started(thread, turn_id, item)
        decision = "accept"
        if policy == "untrusted":
            reply = ask("item/commandExecution/requestApproval", {"threadId": thread, "turnId": turn_id, "itemId": "c1", "command": item["command"], "cwd": cwd,
                                                                  "commandActions": item["commandActions"]})
            decision = (reply or {}).get("decision", "decline")
        if decision in ("accept", "acceptForSession"):
            notify("item/commandExecution/outputDelta", {"threadId": thread, "turnId": turn_id, "itemId": "c1", "delta": "hi-from-agent\n"})
            item_completed(thread, turn_id, {**item, "status": "completed", "aggregatedOutput": "hi-from-agent\n", "exitCode": 0})
        else:
            item_completed(thread, turn_id, {**item, "status": "declined"})
    elif "patch this" in text:
        changes = [{"path": os.path.join(cwd, "hello.txt"), "kind": {"type": "add"}, "diff": "hi\nthere\n"}]
        item = {"type": "fileChange", "id": "f1", "changes": changes, "status": "inProgress"}
        item_started(thread, turn_id, item)
        decision = "accept"
        if policy == "untrusted":
            reply = ask("item/fileChange/requestApproval", {"threadId": thread, "turnId": turn_id, "itemId": "f1", "reason": None, "grantRoot": None})
            decision = (reply or {}).get("decision", "decline")
        if decision in ("accept", "acceptForSession"):
            open(os.path.join(cwd, "hello.txt"), "w").write("hi\nthere\n")
            item_completed(thread, turn_id, {**item, "status": "completed"})
            notify("turn/diff/updated", {"threadId": thread, "turnId": turn_id, "diff": "diff --git a/hello.txt b/hello.txt\nnew file mode 100644\n--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1,2 @@\n+hi\n+there\n"})
        else:
            item_completed(thread, turn_id, {**item, "status": "declined"})
    elif "bigout" in text:
        item = {"type": "commandExecution", "id": "big1", "command": "/bin/bash -lc yes", "cwd": cwd, "status": "inProgress", "commandActions": [{"type": "unknown", "command": "yes"}]}
        item_started(thread, turn_id, item)
        item_completed(thread, turn_id, {**item, "status": "completed", "aggregatedOutput": "y\n" * 50000, "exitCode": 0})
    elif "plan" in text:
        notify("turn/plan/updated", {"threadId": thread, "turnId": turn_id, "explanation": None,
                                     "plan": [{"step": "Прочитать код", "status": "completed"}, {"step": "Написать тест", "status": "inProgress"}, {"step": "Починить", "status": "pending"}]})
        item_completed(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": "план готов"})
    elif "ask me" in text:
        reply = ask("item/tool/requestUserInput", {"threadId": thread, "turnId": turn_id, "itemId": "q1", "isBlocking": True,
                                                   "questions": [{"id": "color", "header": "Цвет", "question": "Какой цвет?", "isOther": True, "isSecret": False,
                                                                  "options": [{"label": "красный", "description": ""}, {"label": "синий", "description": ""}]}]})
        picked = ((reply or {}).get("answers") or {}).get("color", {}).get("answers", ["никакой"])
        item_completed(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": f"выбрано: {picked[0]}"})
    elif "fail" in text:
        status, error = "failed", {"message": "model exploded"}
    elif "slow" in text:
        item_started(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": ""})
        spoken = ""
        for i in range(200 if "slower" in text else 60):
            if state["interrupt"].is_set():
                status = "interrupted"
                break
            piece = f"слово{i} "
            spoken += piece
            notify("item/agentMessage/delta", {"threadId": thread, "turnId": turn_id, "itemId": "m1", "delta": piece})
            time.sleep(0.1)
        if state["steers"]:
            spoken += "(учёл: " + "; ".join(state["steers"]) + ")"
        if status == "completed":
            item_completed(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": spoken})
    else:
        item_started(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": ""})
        full = ""
        for word in ("Привет! ", "Это ", "ответ."):
            full += word
            notify("item/agentMessage/delta", {"threadId": thread, "turnId": turn_id, "itemId": "m1", "delta": word})
            time.sleep(0.02)
        item_completed(thread, turn_id, {"type": "agentMessage", "id": "m1", "text": full})

    for _ in range(2):
        notify("thread/tokenUsage/updated", {"threadId": thread, "turnId": turn_id, "tokenUsage": {
            "total": {"totalTokens": 100 * (state["counter"] + 1), "inputTokens": 80 * (state["counter"] + 1), "cachedInputTokens": 0, "outputTokens": 20, "reasoningOutputTokens": 0},
            "last": {"totalTokens": 62, "inputTokens": 50, "cachedInputTokens": 0, "outputTokens": 12, "reasoningOutputTokens": 0},
            "modelContextWindow": 258400}})
        break
    state["turn"] = None
    notify("turn/completed", {"threadId": thread, "turn": {"id": turn_id, "status": status, "error": error, "items": []}})


def goal_out(thread: str) -> dict:
    g = state["goal"]
    return {"threadId": thread, "objective": g["objective"], "status": g["status"], "tokenBudget": g.get("budget"), "tokensUsed": g["used"],
            "timeUsedSeconds": 1, "createdAt": 1, "updatedAt": 2}


def goal_changed(thread: str) -> None:
    save_thread(thread, {"goal": state["goal"]})
    notify("thread/goal/updated", {"threadId": thread, "turnId": state["turn"], "goal": goal_out(thread)})


def goal_loop(thread: str) -> None:
    """Like the real Codex: while the goal is active, turn follows turn without anybody asking. Done after 3 steps ("forever": never),
    or when the token budget is spent; "silent": active, but no turn ever starts."""
    step = 0
    time.sleep(0.05)
    while state.get("goal") and state["goal"]["status"] == "active" and "silent" not in state["goal"]["objective"]:
        step += 1
        turn_id = new_id("turn")
        state["turn"] = turn_id
        state["interrupt"].clear()
        notify("turn/started", {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress", "items": []}})
        time.sleep(0.25)
        if state["interrupt"].is_set():
            state["turn"] = None
            notify("turn/completed", {"threadId": thread, "turn": {"id": turn_id, "status": "interrupted", "error": None, "items": []}})
            return
        item_completed(thread, turn_id, {"type": "agentMessage", "id": "g1", "text": f"шаг {step} к цели"})
        if state.get("goal"):
            goal = state["goal"]
            goal["used"] += 62
            total = goal["used"]
            notify("thread/tokenUsage/updated", {"threadId": thread, "turnId": turn_id, "tokenUsage": {
                "total": {"totalTokens": total, "inputTokens": total - 12 * step, "cachedInputTokens": 0, "outputTokens": 12 * step, "reasoningOutputTokens": 0},
                "last": {"totalTokens": 62, "inputTokens": 50, "cachedInputTokens": 0, "outputTokens": 12, "reasoningOutputTokens": 0}, "modelContextWindow": 1000}})
            if goal.get("budget") and goal["used"] >= goal["budget"]:
                goal["status"] = "budgetLimited"
                goal_changed(thread)
            elif step >= 3 and "forever" not in goal["objective"]:
                goal["status"] = "complete"
                goal_changed(thread)
        state["turn"] = None
        notify("turn/completed", {"threadId": thread, "turn": {"id": turn_id, "status": "completed", "error": None, "items": []}})
        time.sleep(0.05)


def handle(message: dict) -> None:
    method, params, rid = message.get("method"), message.get("params") or {}, message.get("id")
    if rid is None:  # a notification (e.g. `initialized`): nothing to answer
        return
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake", "codexHome": os.environ.get("CODEX_HOME", ""), "platformFamily": "unix", "platformOs": "linux"}})
    elif method in ("thread/start", "thread/resume"):
        if method == "thread/resume":
            thread = params["threadId"]
            if thread.startswith("gone") or thread not in known_threads():
                send({"id": rid, "error": {"code": -32600, "message": f"no rollout found for thread id {thread}"}})
                return
        else:
            thread = new_id("thr")
        state["seen"].update({k: params.get(k) for k in ("approvalPolicy", "sandbox", "developerInstructions", "model", "cwd")})
        state["seen"]["method"] = method
        state["goal"] = known_threads().get(thread, {}).get("goal") if method == "thread/resume" else None
        save_thread(thread, {"goal": state["goal"]})
        state["thread"] = thread
        send({"id": rid, "result": {"thread": thread_obj(thread)}})
        notify("thread/started", {"thread": thread_obj(thread)})
        if state["goal"] and state["goal"]["status"] == "active":  # a goal left active goes on by itself the moment the thread is back
            threading.Thread(target=goal_loop, args=(thread,), daemon=True).start()
    elif method in ("thread/goal/set", "thread/goal/get", "thread/goal/clear"):
        if "goals" in " ".join(sys.argv) and "--disable" in sys.argv and sys.argv[sys.argv.index("goals") - 1] == "--disable":
            send({"id": rid, "error": {"code": -32600, "message": "goals feature is disabled"}})
            return
        thread = params["threadId"]
        if method == "thread/goal/get":
            send({"id": rid, "result": {"goal": goal_out(thread) if state.get("goal") else None}})
        elif method == "thread/goal/clear":
            state["goal"] = None
            save_thread(thread, {"goal": None})
            send({"id": rid, "result": {"cleared": True}})
            notify("thread/goal/cleared", {"threadId": thread})
        else:
            goal = state.get("goal")
            if params.get("objective"):
                goal = {"objective": params["objective"], "status": "active", "budget": None, "used": 0}
            if goal is None:
                send({"id": rid, "error": {"code": -32600, "message": "no goal to update"}})
                return
            was_active = goal["status"] == "active" and state.get("goal") is goal
            goal["status"] = params.get("status") or goal["status"]
            if params.get("tokenBudget") is not None:
                goal["budget"] = params["tokenBudget"]
            state["goal"] = goal
            state["seen"]["goalSet"] = params
            send({"id": rid, "result": {"goal": goal_out(thread)}})
            goal_changed(thread)
            if goal["status"] == "active" and not was_active and state["turn"] is None:
                threading.Thread(target=goal_loop, args=(thread,), daemon=True).start()
    elif method == "turn/start":
        text = "".join(i.get("text", "") for i in params.get("input", []))
        for k in ("approvalPolicy", "effort", "model"):
            if params.get(k) is not None:
                state["seen"][k] = params[k]
        turn_id = new_id("turn")
        state["seen"]["lastPrompt"] = text
        state["steers"] = []
        send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        threading.Thread(target=run_turn, args=(params["threadId"], turn_id, text, params), daemon=True).start()
    elif method == "turn/steer":
        if state["turn"] != params.get("expectedTurnId"):
            send({"id": rid, "error": {"code": -32600, "message": "expected turn id does not match the active turn"}})
            return
        state["steers"].append("".join(i.get("text", "") for i in params.get("input", [])))
        send({"id": rid, "result": {"turnId": state["turn"]}})
    elif method == "turn/interrupt":
        state["interrupt"].set()
        send({"id": rid, "result": {}})
    elif method == "thread/compact/start":
        turn_id = new_id("turn")
        send({"id": rid, "result": {}})
        notify("turn/started", {"threadId": params["threadId"], "turn": {"id": turn_id, "status": "inProgress", "items": []}})
        item_completed(params["threadId"], turn_id, {"type": "contextCompaction", "id": "cc1"})
        notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": turn_id, "status": "completed", "error": None, "items": []}})
    elif method == "thread/shellCommand":
        turn_id = new_id("turn")
        send({"id": rid, "result": {}})
        notify("turn/started", {"threadId": params["threadId"], "turn": {"id": turn_id, "status": "inProgress", "items": []}})
        item = {"type": "commandExecution", "id": "sh1", "command": f"/bin/bash -lc '{params['command']}'", "source": "userShell", "status": "inProgress"}
        item_started(params["threadId"], turn_id, item)
        item_completed(params["threadId"], turn_id, {**item, "status": "completed", "aggregatedOutput": f"ran: {params['command']}\n", "exitCode": 0})
        notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": turn_id, "status": "completed", "error": None, "items": []}})
    elif method == "review/start":
        turn_id = new_id("turn")
        send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        notify("turn/started", {"threadId": params["threadId"], "turn": {"id": turn_id, "status": "inProgress", "items": []}})
        item_completed(params["threadId"], turn_id, {"type": "enteredReviewMode", "id": "r1", "review": json.dumps(params.get("target"))})
        item_completed(params["threadId"], turn_id, {"type": "agentMessage", "id": "m1", "text": "Замечаний нет."})
        item_completed(params["threadId"], turn_id, {"type": "exitedReviewMode", "id": "r2", "review": "Замечаний нет."})
        notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": turn_id, "status": "completed", "error": None, "items": []}})
    elif method == "thread/revert":
        send({"id": rid, "result": {"thread": thread_obj(params["threadId"])}})
        notify("thread/reverted", {"threadId": params["threadId"]})
    else:
        send({"id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


def main() -> None:
    def reader() -> None:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "method" not in message and "id" in message and str(message["id"]) in answers:
                entry = answers[str(message["id"])]
                entry[1] = message.get("result")
                entry[0].set()
            else:
                inbox.put(message)
        os._exit(0)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        message = inbox.get()
        if "method" in message:
            handle(message)


if __name__ == "__main__":
    main()
