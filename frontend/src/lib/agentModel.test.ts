import { describe, expect, it } from "vitest";

import type { AgentEvent } from "./api";
import { buildFeed, canSend, contextView, formatTokens, goalResumable, goalStatusLabel, lastStepRunning, parseBudget, readComposer, turnFinished, usableProfiles, waitingForUser, type FeedEntry } from "./agentModel";

let seq = 0;
const ev = (kind: string, payload: Record<string, unknown> = {}): AgentEvent => ({ seq: seq++, kind, payload });
const reset = () => {
  seq = 0;
};
const done = (item: Record<string, unknown>) => ev("item.completed", { item });
const msg = (id: string, text: string) => done({ id, type: "agent_message", text });
const types = (feed: FeedEntry[]) => feed.map((f) => f.type);

describe("buildFeed: the history", () => {
  it("shows the user's message, the steps and the answer in order, closed by the usage", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "покажи файлы" }),
      ev("turn.started", { turn_id: "t1" }),
      done({ id: "c1", type: "command_execution", command: "ls", aggregated_output: "a\nb\n", exit_code: 0, status: "completed", source: "agent" }),
      msg("m1", "Готово"),
      ev("turn.completed", { turn_id: "t1", status: "completed", usage: { input_tokens: 120, output_tokens: 30 } }),
    ]);
    expect(types(feed)).toEqual(["user", "command", "message", "usage"]);
    expect(feed[1]).toMatchObject({ command: "ls", output: "a\nb\n", exitCode: 0, running: false, user: false });
    expect(feed[3]).toMatchObject({ inputTokens: 120, outputTokens: 30 });
  });

  it("does not mix up items of different turns that share an id", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "one" }), ev("turn.started", { turn_id: "a" }), msg("m1", "first"), ev("user", { text: "two" }), ev("turn.started", { turn_id: "b" }), msg("m1", "second")]);
    expect(feed.filter((f) => f.type === "message").map((f) => (f as { text: string }).text)).toEqual(["first", "second"]);
  });

  it("an instruction added to a running turn does not start a new one", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "slow" }), ev("turn.started", { turn_id: "a" }), ev("user", { text: "и ещё ура", steer: true }), msg("m1", "ответ")]);
    expect(feed[1]).toMatchObject({ type: "user", steer: true, text: "и ещё ура" });
    expect(feed[2].key).toBe("t1.1:m1");
  });

  it("shows the actions the user took", () => {
    reset();
    const feed = buildFeed([
      ev("action", { action: "compact" }),
      ev("action", { action: "shell", command: "ls -la" }),
      ev("action", { action: "review", target: { type: "uncommittedChanges" } }),
      ev("action", { action: "review", target: { type: "baseBranch", branch: "main" } }),
      ev("action", { action: "review", target: { type: "commit", sha: "abcdef1234567" } }),
      ev("action", { action: "revert" }),
      ev("action", { action: "future-thing" }),
    ]);
    expect(feed.map((f) => (f as { text: string }).text)).toEqual([
      "Сжать контекст", "$ ls -la", "Код-ревью текущих изменений", "Код-ревью относительно main", "Код-ревью коммита abcdef12", "Откатить последний ход", "future-thing",
    ]);
  });

  it("a command the user ran themselves is marked", () => {
    reset();
    const feed = buildFeed([ev("action", { action: "shell", command: "ls" }), done({ id: "s", type: "command_execution", command: "ls", aggregated_output: "x", exit_code: 0, status: "completed", source: "userShell" })]);
    expect(feed[1]).toMatchObject({ type: "command", user: true });
  });

  it("keeps a file change with each file's diff, and the turn's diff and plan", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "x" }),
      done({ id: "f", type: "file_change", status: "completed", changes: [{ path: "a.txt", kind: "update", diff: "@@\n-a\n+b\n" }] }),
      ev("turn.diff", { diff: "diff --git a/a.txt b/a.txt\n" }),
      ev("turn.plan", { steps: [{ text: "один", completed: true, in_progress: false }, { text: "два", completed: false, in_progress: true }], explanation: null }),
    ]);
    expect(feed[1]).toMatchObject({ type: "files", changes: [{ path: "a.txt", kind: "update", diff: "@@\n-a\n+b\n" }], running: false });
    expect(feed[2]).toMatchObject({ type: "diff", live: false });
    expect(feed[3]).toMatchObject({ type: "plan", steps: [{ text: "один", completed: true, inProgress: false }, { text: "два", completed: false, inProgress: true }], live: false });
  });

  it("shows quiet notices for compaction, review, warnings, failures and stops", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "x" }),
      done({ id: "cc", type: "context_compaction", status: "completed" }),
      done({ id: "r1", type: "review_mode", entered: true }),
      done({ id: "r2", type: "review_mode", entered: false }),
      done({ id: "", type: "error", message: "Model metadata for `m` not found" }),
      ev("turn.error", { message: "boom" }),
      ev("turn.stopped", { message: "Остановлено оператором" }),
    ]);
    expect(feed.slice(1).map((f) => [f.type, (f as { tone: string }).tone, (f as { text: string }).text])).toEqual([
      ["notice", "info", "Разговор сжат: старые ходы заменены кратким изложением"], ["notice", "info", "Код-ревью начато"], ["notice", "info", "Код-ревью закончено"],
      ["notice", "info", "Model metadata for `m` not found"], ["notice", "bad", "boom"], ["notice", "warn", "Остановлено оператором"],
    ]);
  });

  it("keeps working with the events stored by the earlier `codex exec` version", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "x" }), ev("thread.started", { thread_id: "t" }), ev("turn.started", {}),
      ev("item.started", { item: { id: "item_1", type: "command_execution", command: "sleep 5", aggregated_output: "", exit_code: null, status: "in_progress" } }),
      ev("item.completed", { item: { id: "item_1", type: "command_execution", command: "sleep 5", aggregated_output: "", exit_code: 0, status: "completed" } }),
      ev("item.completed", { item: { id: "item_2", type: "todo_list", items: [{ text: "шаг", completed: true }] } }),
    ]);
    expect(types(feed)).toEqual(["user", "command", "todo"]);
    expect(feed[1]).toMatchObject({ running: false, exitCode: 0 });
  });

  it("never drops an item type it does not know, and survives malformed payloads", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "x" }), done({ id: "z", type: "other", kind: "collabToolCall" }), ev("item.completed", { item: null }), ev("item.completed", {}), ev("turn.completed", { usage: "x" }), ev("approval.requested", {})]);
    expect(feed[1]).toMatchObject({ type: "tool", title: "collabToolCall" });
    expect(() => buildFeed([ev("turn.diff", {}), ev("turn.plan", {}), ev("thread.reverted", {}), ev("action", {})])).not.toThrow();
  });

  it("is empty for no events", () => {
    expect(buildFeed([])).toEqual([]);
  });
});

describe("buildFeed: what is being written now", () => {
  it("shows an unfinished message, a command's output so far, and the diff and plan so far", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "x" }), ev("turn.started", { turn_id: "a" })], {
      m1: { id: "m1", type: "agent_message", text: "Пишу отв" },
      c1: { id: "c1", type: "command_execution", command: "make", aggregated_output: "step 1\n", exit_code: null, status: "in_progress" },
      __diff__: { diff: "diff --git a/x b/x\n" },
      __plan__: { steps: [{ text: "a", completed: false, in_progress: true }], explanation: null },
    });
    expect(feed.slice(1).map((f) => [f.type, (f as { live?: boolean }).live ?? (f as { running?: boolean }).running])).toEqual([["message", true], ["command", true], ["diff", true], ["plan", true]]);
    expect(feed[1]).toMatchObject({ text: "Пишу отв" });
    expect(feed[2]).toMatchObject({ output: "step 1\n", running: true });
  });

  it("the finished item replaces its snapshot, and a stale snapshot never overrides it", () => {
    reset();
    const history = [ev("user", { text: "x" }), ev("turn.started", { turn_id: "a" }), msg("m1", "Готовый ответ")];
    const feed = buildFeed(history, { m1: { id: "m1", type: "agent_message", text: "Готов" } });
    expect(feed.filter((f) => f.type === "message")).toEqual([{ type: "message", key: "t1.1:m1", text: "Готовый ответ", live: false }]);
  });

  it("a snapshot appears after what the history already shows, in the current turn", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "one" }), ev("turn.started", { turn_id: "a" }), msg("m0", "первое"), ev("user", { text: "two" }), ev("turn.started", { turn_id: "b" })], { m0: { id: "m0", type: "agent_message", text: "второе" } });
    expect(feed.filter((f) => f.type === "message").map((f) => (f as { text: string }).text)).toEqual(["первое", "второе"]);
  });
});

describe("buildFeed: questions to the user", () => {
  const ask = (id: string, approval: Record<string, unknown>) => ev("approval.requested", { request_id: id, approval });

  it("shows a question until it is answered", () => {
    reset();
    const open = buildFeed([ev("user", { text: "x" }), ask("4", { kind: "command", item_id: "c1", command: "rm -rf build", cwd: "." })]);
    expect(open[1]).toMatchObject({ type: "approval", requestId: "4", resolved: false, approval: { kind: "command", command: "rm -rf build", cwd: ".", questions: [] } });
    expect(waitingForUser(open)).toBe(true);
    reset();
    const answered = buildFeed([ev("user", { text: "x" }), ask("4", { kind: "command", command: "x" }), ev("approval.resolved", { request_id: "4" })]);
    expect(answered[1]).toMatchObject({ type: "approval", resolved: true });
    expect(waitingForUser(answered)).toBe(false);
  });

  it("carries the questions the agent asks, with their options", () => {
    reset();
    const feed = buildFeed([ask("7", { kind: "input", item_id: "q", questions: [{ id: "color", header: "Цвет", question: "Какой?", secret: false, other: true, options: [{ label: "красный", description: "d" }] }] })]);
    expect(feed[0]).toMatchObject({ approval: { kind: "input", questions: [{ id: "color", header: "Цвет", question: "Какой?", other: true, options: [{ label: "красный", description: "d" }] }] } });
  });

  it("an answer for an unknown question changes nothing", () => {
    reset();
    expect(buildFeed([ev("approval.resolved", { request_id: "nope" })])).toEqual([]);
  });
});

describe("buildFeed: undoing the last turn", () => {
  const twoTurns = () => [
    ev("user", { text: "one" }), ev("turn.started", { turn_id: "a" }), msg("m1", "first"), ev("turn.completed", { turn_id: "a", usage: {} }),
    ev("user", { text: "two" }), ev("turn.started", { turn_id: "b" }), msg("m2", "second"), ev("turn.completed", { turn_id: "b", usage: {} }),
  ];

  it("removes what the undone turn produced, with the message that started it, and says so", () => {
    reset();
    const feed = buildFeed([...twoTurns(), ev("action", { action: "revert" }), ev("thread.reverted", { turn_id: "b" })]);
    expect(feed.filter((f) => f.type === "user").map((f) => (f as { text: string }).text)).toEqual(["one"]);
    expect(feed.some((f) => f.type === "message" && f.text === "second")).toBe(false);
    expect(feed[feed.length - 1]).toMatchObject({ type: "notice", tone: "warn" });
    expect((feed[feed.length - 1] as { text: string }).text).toContain("Файлы остались как были");
  });

  it("can undo turn after turn: once everything is undone only the last notice remains", () => {
    reset();
    const feed = buildFeed([...twoTurns(), ev("thread.reverted", { turn_id: "b" }), ev("thread.reverted", { turn_id: "a" })]);
    expect(types(feed)).toEqual(["notice"]);
  });

  it("a turn started by an action is undone together with that action", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "one" }), ev("turn.started", { turn_id: "a" }), msg("m1", "first"), ev("action", { action: "compact" }), ev("turn.started", { turn_id: "c" }), done({ id: "cc", type: "context_compaction", status: "completed" }), ev("thread.reverted", { turn_id: "c" })]);
    expect(feed.some((f) => f.type === "action")).toBe(false);
    expect(feed.some((f) => f.type === "message")).toBe(true);
  });

  it("an unknown turn leaves the feed alone", () => {
    reset();
    const feed = buildFeed([...twoTurns(), ev("thread.reverted", { turn_id: "zzz" })]);
    expect(feed.filter((f) => f.type === "message")).toHaveLength(2);
  });
});

describe("states", () => {
  it("a step in progress at the end of the feed", () => {
    reset();
    expect(lastStepRunning(buildFeed([ev("user", { text: "x" }), ev("item.started", { item: { id: "c", type: "command_execution", command: "ls", status: "in_progress" } })]))).toBe(true);
    expect(lastStepRunning(buildFeed([ev("user", { text: "x" })], { m: { id: "m", type: "agent_message", text: "..." } }))).toBe(true);
    expect(lastStepRunning(buildFeed([ev("user", { text: "x" }), msg("m", "done")]))).toBe(false);
    expect(lastStepRunning([])).toBe(false);
  });

  it("a turn that has reported its end", () => {
    reset();
    const finished = buildFeed([ev("user", { text: "x" }), msg("a", "hi"), ev("turn.completed", { usage: { input_tokens: 1, output_tokens: 1 } })]);
    expect(turnFinished(finished)).toBe(true);
    expect(turnFinished(buildFeed([ev("user", { text: "x" }), msg("a", "hi")]))).toBe(false);
    expect(turnFinished([])).toBe(false);
  });
});

describe("readComposer", () => {
  it("a message when idle, an added instruction while the agent works", () => {
    expect(readComposer("  привет ", "idle")).toEqual({ kind: "message", text: "привет" });
    expect(readComposer("добавь тесты", "running")).toEqual({ kind: "steer", text: "добавь тесты" });
    expect(readComposer("   ", "idle")).toEqual({ kind: "empty" });
  });
  it("`!` runs a command of your own", () => {
    expect(readComposer("!ls -la", "idle")).toEqual({ kind: "shell", command: "ls -la" });
    expect(readComposer("! ", "idle")).toEqual({ kind: "message", text: "!" });
    expect(readComposer("!ls", "running")).toEqual({ kind: "steer", text: "!ls" }); // while the agent works everything is an instruction
  });
  it("understands the slash commands", () => {
    expect(readComposer("/compact", "idle")).toEqual({ kind: "action", action: "compact", argument: "" });
    expect(readComposer("/Review main", "idle")).toEqual({ kind: "action", action: "review", argument: "main" });
    expect(readComposer("/undo", "idle")).toEqual({ kind: "action", action: "revert", argument: "" });
    expect(readComposer("/fork", "idle")).toEqual({ kind: "action", action: "fork", argument: "" });
    expect(readComposer("/unknown thing", "idle")).toEqual({ kind: "message", text: "/unknown thing" });
  });
});

describe("canSend", () => {
  it("needs text, and a usable profile unless the agent is already working (then it only adds an instruction)", () => {
    expect(canSend({ text: "hi", status: "idle", profileAvailable: true })).toBe(true);
    expect(canSend({ text: "  ", status: "idle", profileAvailable: true })).toBe(false);
    expect(canSend({ text: "hi", status: "idle", profileAvailable: false })).toBe(false);
    expect(canSend({ text: "hi", status: "running", profileAvailable: true })).toBe(true);
  });
});

describe("usableProfiles", () => {
  it("drops Claude profiles: Codex cannot use them", () => {
    const list = [{ provider: "claude" }, { provider: "openai" }, { provider: "grok" }, { provider: "custom" }];
    expect(usableProfiles(list).map((p) => p.provider)).toEqual(["openai", "grok", "custom"]);
  });
});

describe("formatTokens", () => {
  it("is compact for big numbers", () => {
    expect(formatTokens(950)).toBe("950");
    expect(formatTokens(12_345)).toBe("12.3k");
    expect(formatTokens(250_000)).toBe("250k");
  });
});

describe("goals", () => {
  it("the feed says when a goal stops being worked on, and why — not after every turn of it", () => {
    reset();
    const goal = (status: string, used = 0) => ev("goal.updated", { goal: { objective: "зелёные тесты", status, token_budget: 100000, tokens_used: used } });
    const feed = buildFeed([ev("action", { action: "goal", objective: "зелёные тесты", token_budget: 100000 }), goal("active"), msg("g1", "шаг 1"), goal("budgetLimited", 100200), ev("action", { action: "goal", resume: true }), goal("active"), goal("complete", 120000), ev("action", { action: "goal_clear" }), ev("goal.cleared")]);
    expect(feed.filter((f) => f.type === "action").map((f) => (f as { text: string }).text)).toEqual(["Цель: зелёные тесты", "Продолжить работу над целью", "Снять цель"]);
    expect(feed.filter((f) => f.type === "notice").map((f) => [(f as { tone: string }).tone, (f as { text: string }).text])).toEqual([
      ["warn", "Цель остановлена: бюджет токенов исчерпан · потрачено 100k из 100k токенов"],
      ["info", "Цель достигнута · потрачено 120k из 100k токенов"],
      ["info", "Цель снята"],
    ]);
  });
  it("keeps the steps of every self-started turn of a goal, though their ids repeat", () => {
    reset();
    const step = (text: string) => [ev("turn.started", { turn_id: text }), msg("g1", text), ev("turn.completed", { turn_id: text, usage: {} })];
    const feed = buildFeed([ev("action", { action: "goal", objective: "x", token_budget: 100000 }), ...step("шаг 1"), ...step("шаг 2"), ...step("шаг 3")], { g1: { id: "g1", type: "agent_message", text: "шаг 4 пишет" } });
    expect(feed.filter((f) => f.type === "message").map((f) => (f as { text: string }).text)).toEqual(["шаг 1", "шаг 2", "шаг 3"]);
  });
  it("labels every status and keeps an unknown one readable", () => {
    for (const s of ["active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"]) expect(goalStatusLabel(s)).not.toBe(s);
    expect(goalStatusLabel("somethingNew")).toBe("somethingNew");
  });
  it("only a goal that is neither reached nor running can be set going again", () => {
    expect(goalResumable({ status: "paused" }, "idle")).toBe(true);
    expect(goalResumable({ status: "budgetLimited" }, "idle")).toBe(true);
    expect(goalResumable({ status: "complete" }, "idle")).toBe(false);
    expect(goalResumable({ status: "paused" }, "running")).toBe(false);
    expect(goalResumable(null, "idle")).toBe(false);
  });
  it("reads a budget typed by a person", () => {
    expect(parseBudget("500000")).toBe(500000);
    expect(parseBudget(" 500k ")).toBe(500000);
    expect(parseBudget("1.5m")).toBe(1500000);
    expect(parseBudget("2М")).toBe(2000000);
    expect(parseBudget("0,5м")).toBe(500000);
    expect(parseBudget("5000")).toBeNull(); // too small to be meant
    expect(parseBudget("много")).toBeNull();
    expect(parseBudget("")).toBeNull();
  });
  it("/goal is a command of the box", () => {
    expect(readComposer("/goal довести тесты до зелёного", "idle")).toEqual({ kind: "action", action: "goal", argument: "довести тесты до зелёного" });
  });
});

describe("contextView", () => {
  it("is unknown until a turn has reported it", () => {
    expect(contextView(null, 258400)).toEqual({ percent: null, tone: "ok", suggestCompact: false });
    expect(contextView(100, null)).toEqual({ percent: null, tone: "ok", suggestCompact: false });
    expect(contextView(100, 0).percent).toBeNull();
  });
  it("turns into a percentage with a warning as it fills up", () => {
    expect(contextView(25840, 258400)).toEqual({ percent: 10, tone: "ok", suggestCompact: false });
    expect(contextView(190000, 258400)).toMatchObject({ percent: 74, tone: "warn", suggestCompact: true });
    expect(contextView(250000, 258400)).toMatchObject({ percent: 97, tone: "bad", suggestCompact: true });
    expect(contextView(999999, 258400).percent).toBe(100);
  });
});
