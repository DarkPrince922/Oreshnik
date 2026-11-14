import { describe, expect, it } from "vitest";

import type { AgentEvent } from "./api";
import { buildFeed, canSend, formatTokens, lastStepRunning, turnFinished, usableProfiles } from "./agentModel";

let seq = 0;
const ev = (kind: string, payload: Record<string, unknown> = {}): AgentEvent => ({ seq: seq++, kind, payload });
const reset = () => {
  seq = 0;
};

describe("buildFeed", () => {
  it("shows the user's message, the agent's steps and its answer in order", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "покажи файлы" }),
      ev("thread.started", { thread_id: "t1" }),
      ev("turn.started"),
      ev("item.started", { item: { id: "item_1", type: "command_execution", command: "ls", aggregated_output: "", exit_code: null, status: "in_progress" } }),
      ev("item.completed", { item: { id: "item_1", type: "command_execution", command: "ls", aggregated_output: "a\nb\n", exit_code: 0, status: "completed" } }),
      ev("item.completed", { item: { id: "item_2", type: "agent_message", text: "Готово" } }),
      ev("turn.completed", { usage: { input_tokens: 120, output_tokens: 30 } }),
    ]);
    expect(feed.map((f) => f.type)).toEqual(["user", "command", "message", "usage"]);
    expect(feed[1]).toMatchObject({ command: "ls", output: "a\nb\n", exitCode: 0, running: false });
    expect(feed[3]).toMatchObject({ inputTokens: 120, outputTokens: 30 });
  });

  it("updates a step in place instead of repeating it", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "x" }),
      ev("item.started", { item: { id: "item_1", type: "command_execution", command: "sleep 5", aggregated_output: "", exit_code: null, status: "in_progress" } }),
    ]);
    expect(feed).toHaveLength(2);
    expect(lastStepRunning(feed)).toBe(true);
  });

  it("does not mix up items of different turns: ids restart at every turn", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "one" }),
      ev("item.completed", { item: { id: "item_1", type: "agent_message", text: "first answer" } }),
      ev("user", { text: "two" }),
      ev("item.completed", { item: { id: "item_1", type: "agent_message", text: "second answer" } }),
    ]);
    expect(feed.filter((f) => f.type === "message").map((f) => (f as { text: string }).text)).toEqual(["first answer", "second answer"]);
  });

  it("shows panel notes about failed or stopped turns once, not Codex's duplicate error events", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "x" }),
      ev("error", { message: "boom" }),
      ev("turn.failed", { error: { message: "boom" } }),
      ev("turn.error", { message: "boom" }),
      ev("turn.stopped", { message: "Остановлено оператором" }),
    ]);
    expect(feed.filter((f) => f.type === "notice")).toEqual([
      { type: "notice", key: "e3", tone: "bad", text: "boom" },
      { type: "notice", key: "s4", tone: "warn", text: "Остановлено оператором" },
    ]);
  });

  it("keeps a harmless Codex warning as a quiet notice", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "x" }), ev("item.completed", { item: { id: "item_0", type: "error", message: "Model metadata for `m` not found" } })]);
    expect(feed[1]).toMatchObject({ type: "notice", tone: "info" });
  });

  it("understands file changes and plans", () => {
    reset();
    const feed = buildFeed([
      ev("user", { text: "x" }),
      ev("item.completed", { item: { id: "a", type: "file_change", changes: [{ path: "src/a.py", kind: "update" }], status: "completed" } }),
      ev("item.updated", { item: { id: "b", type: "todo_list", items: [{ text: "шаг 1", completed: true }, { text: "шаг 2", completed: false }] } }),
    ]);
    expect(feed[1]).toMatchObject({ type: "files", changes: [{ path: "src/a.py", kind: "update" }], running: false });
    expect(feed[2]).toMatchObject({ type: "todo", items: [{ text: "шаг 1", completed: true }, { text: "шаг 2", completed: false }] });
  });

  it("never drops an item type it does not know", () => {
    reset();
    const feed = buildFeed([ev("user", { text: "x" }), ev("item.completed", { item: { id: "z", type: "collab_tool_call" } })]);
    expect(feed[1]).toMatchObject({ type: "tool", title: "collab_tool_call" });
  });

  it("survives malformed payloads", () => {
    reset();
    expect(() => buildFeed([ev("item.completed", { item: null }), ev("item.completed", {}), ev("turn.completed", { usage: "x" })])).not.toThrow();
  });

  it("is empty for no events", () => {
    expect(buildFeed([])).toEqual([]);
  });
});

describe("canSend", () => {
  it("needs text, an idle session and a usable profile", () => {
    expect(canSend({ text: "hi", status: "idle", profileAvailable: true })).toBe(true);
    expect(canSend({ text: "  ", status: "idle", profileAvailable: true })).toBe(false);
    expect(canSend({ text: "hi", status: "running", profileAvailable: true })).toBe(false);
    expect(canSend({ text: "hi", status: "idle", profileAvailable: false })).toBe(false);
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

describe("turnFinished", () => {
  it("is true once the usage line closes the feed, so no spinner is shown for a turn that is over", () => {
    reset();
    const done = buildFeed([ev("user", { text: "x" }), ev("item.completed", { item: { id: "a", type: "agent_message", text: "hi" } }), ev("turn.completed", { usage: { input_tokens: 1, output_tokens: 1 } })]);
    expect(turnFinished(done)).toBe(true);
    const working = buildFeed([ev("user", { text: "x" }), ev("item.completed", { item: { id: "a", type: "agent_message", text: "hi" } })]);
    expect(turnFinished(working)).toBe(false);
    expect(turnFinished([])).toBe(false);
  });
});
