import { describe, expect, it } from "vitest";

import { agentColor, agentStatus, buildFeed, buildTree, countSteps, offsetLabel } from "./activityModel";
import type { ActivityEvent, AgentInfo } from "./api";

let seq = 0;
const ev = (over: Partial<ActivityEvent>): ActivityEvent => ({
  seq: seq++, ts: "2026-01-01T10:00:00+00:00", agent: "a", kind: "message", role: null, text: null, tool: null, call_id: null, args: null, summary: null, truncated: false, ...over,
});
const agent = (id: string, parent: string | null = null, status = "running"): AgentInfo => ({ id, name: id.toUpperCase(), parent_id: parent, status, error: null });

describe("buildFeed", () => {
  it("merges a call and its output into one step, in order", () => {
    seq = 0;
    const feed = buildFeed([
      ev({ kind: "message", text: "plan" }),
      ev({ kind: "tool_call", tool: "exec_command", call_id: "c1", summary: "nmap x" }),
      ev({ kind: "tool_output", call_id: "c1", tool: "exec_command", text: "80/tcp open" }),
      ev({ kind: "message", text: "next" }),
    ]);
    expect(feed.map((f) => f.type)).toEqual(["text", "step", "text"]);
    const step = feed[1];
    expect(step.type === "step" && step.output).toBe("80/tcp open");
  });

  it("shows a call as still running until its output arrives", () => {
    seq = 0;
    const feed = buildFeed([ev({ kind: "tool_call", tool: "exec_command", call_id: "c1", summary: "sleep 60" })]);
    expect(feed[0].type === "step" && feed[0].output).toBeNull();
  });

  it("does not mix up equal call ids of different agents", () => {
    seq = 0;
    const feed = buildFeed([
      ev({ agent: "a", kind: "tool_call", call_id: "c1", summary: "A" }),
      ev({ agent: "b", kind: "tool_call", call_id: "c1", summary: "B" }),
      ev({ agent: "b", kind: "tool_output", call_id: "c1", text: "out-b" }),
    ]);
    expect(feed.map((f) => f.type === "step" && f.output)).toEqual([null, "out-b"]);
  });

  it("keeps an output whose call was never seen, and unique keys", () => {
    seq = 0;
    const feed = buildFeed([ev({ kind: "tool_output", call_id: "zz", text: "orphan" }), ev({ kind: "tool_call", call_id: "y" })]);
    expect(feed).toHaveLength(2);
    expect(new Set(feed.map((f) => f.key)).size).toBe(2);
  });

  it("carries truncation flags and text kinds", () => {
    seq = 0;
    const feed = buildFeed([ev({ kind: "task", text: "t", truncated: true }), ev({ kind: "system", text: "[URGENT] budget" })]);
    expect(feed[0].type === "text" && [feed[0].kind, feed[0].cut]).toEqual(["task", true]);
    expect(feed[1].type === "text" && feed[1].kind).toBe("system");
  });
});

describe("buildTree", () => {
  it("nests children under their parent with depths", () => {
    const tree = buildTree([agent("root"), agent("c1", "root"), agent("c2", "root"), agent("g", "c1")]);
    expect(tree).toHaveLength(1);
    expect(tree[0].children.map((c) => c.agent.id)).toEqual(["c1", "c2"]);
    expect(tree[0].children[0].children[0]).toMatchObject({ depth: 2 });
  });
  it("makes agents with an unknown parent roots instead of dropping them", () => {
    expect(buildTree([agent("x", "ghost"), agent("y")]).map((n) => n.agent.id)).toEqual(["x", "y"]);
  });
  it("survives cycles and self-parents without looping or losing agents", () => {
    const tree = buildTree([agent("a", "b"), agent("b", "a"), agent("s", "s")]);
    const ids = JSON.stringify(tree);
    for (const id of ["a", "b", "s"]) expect(ids).toContain(`"id":"${id}"`);
  });
  it("handles an empty roster", () => expect(buildTree([])).toEqual([]));
});

describe("helpers", () => {
  it("counts tool calls per agent", () => {
    seq = 0;
    expect(countSteps([ev({ agent: "a", kind: "tool_call" }), ev({ agent: "a", kind: "tool_call" }), ev({ agent: "b", kind: "tool_call" }), ev({ agent: "a", kind: "message" })])).toEqual({ a: 2, b: 1 });
  });
  it("gives every agent a stable colour", () => {
    const roster = [agent("a"), agent("b")];
    expect(agentColor("a", roster)).toBe(agentColor("a", roster));
    expect(agentColor("a", roster)).not.toBe(agentColor("b", roster));
  });
  it("knows Strix's statuses and passes unknown ones through", () => {
    expect(agentStatus("completed").tone).toBe("ok");
    expect(agentStatus("budget_paused").tone).toBe("warn");
    expect(agentStatus("something_new")).toEqual({ label: "something_new", tone: "muted" });
  });
  it("formats time offsets", () => {
    const t0 = "2026-01-01T10:00:00+00:00";
    expect(offsetLabel("2026-01-01T10:00:05+00:00", t0)).toBe("+0:05");
    expect(offsetLabel("2026-01-01T10:12:30+00:00", t0)).toBe("+12:30");
    expect(offsetLabel("2026-01-01T11:02:03+00:00", t0)).toBe("+1:02:03");
    expect(offsetLabel("2025-12-31T10:00:00+00:00", t0)).toBe("+0:00");
    expect(offsetLabel("2026-01-01T10:00:05+00:00", undefined)).toBe("");
  });
});
