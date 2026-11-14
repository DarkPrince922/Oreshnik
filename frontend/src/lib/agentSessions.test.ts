import { describe, expect, it } from "vitest";

import type { FeedEntry } from "./agentModel";
import { agoRu, applyQuick, filterSessions, groupSteps, isUserBlock, pluralRu, sessionCounts, sessionsSummary, turnsRu } from "./agentSessions";

const s = (title: string, status: "idle" | "running" = "idle", last_error: string | null = null, llm_model: string | null = "gpt-5-codex") => ({ title, status, last_error, llm_model });
const list = [s("Разбор скана #41", "running"), s("NTLM relay", "idle", null, "grok-4"), s("Аудит nginx.conf", "idle", "502"), s("Листенер 4444")];

describe("session list", () => {
  it("counts what each filter would show", () => {
    expect(sessionCounts(list)).toEqual({ all: 4, running: 1, error: 1 });
    expect(sessionCounts([])).toEqual({ all: 0, running: 0, error: 0 });
  });
  it("filters by state", () => {
    expect(filterSessions(list, "all", "").length).toBe(4);
    expect(filterSessions(list, "running", "").map((x) => x.title)).toEqual(["Разбор скана #41"]);
    expect(filterSessions(list, "error", "").map((x) => x.title)).toEqual(["Аудит nginx.conf"]);
  });
  it("searches the title and the model, ignoring case and outer spaces", () => {
    expect(filterSessions(list, "all", "  nginx ").map((x) => x.title)).toEqual(["Аудит nginx.conf"]);
    expect(filterSessions(list, "all", "GROK").map((x) => x.title)).toEqual(["NTLM relay"]);
    expect(filterSessions(list, "all", "скана").map((x) => x.title)).toEqual(["Разбор скана #41"]);
    expect(filterSessions(list, "running", "nginx")).toEqual([]); // both conditions apply
  });
  it("copes with a session that has no model", () => {
    expect(filterSessions([s("x", "idle", null, null)], "all", "gpt")).toEqual([]);
  });
});

describe("labels", () => {
  it("declines Russian words by number", () => {
    const forms = ["ход", "хода", "ходов"] as const;
    expect([1, 2, 5, 11, 12, 21, 22, 25, 111].map((n) => pluralRu(n, forms))).toEqual(["ход", "хода", "ходов", "ходов", "ходов", "ход", "хода", "ходов", "ходов"]);
    expect(turnsRu(3)).toBe("3 хода");
    expect(turnsRu(0)).toBe("0 ходов");
  });
  it("sums the sessions up in one line", () => {
    expect(sessionsSummary(list)).toBe("1 сессия работает, 3 ждут");
    expect(sessionsSummary([s("a"), s("b")])).toBe("2 сессии ждут");
    expect(sessionsSummary([s("a")])).toBe("1 сессия ждёт");
    expect(sessionsSummary([s("a", "running"), s("b", "running")])).toBe("2 сессии работают");
    expect(sessionsSummary([])).toBe("");
  });
  it("says how long ago", () => {
    const now = Date.parse("2026-11-14T12:00:00Z");
    const at = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
    expect(agoRu(at(0), now)).toBe("только что");
    expect(agoRu(at(5), now)).toBe("5 мин назад");
    expect(agoRu(at(125), now)).toBe("2 ч назад");
    expect(agoRu(at(60 * 30), now)).toBe("вчера");
    expect(agoRu(at(60 * 24 * 3), now)).toBe("3 дн назад");
    expect(agoRu(at(60 * 24 * 30), now)).not.toContain("назад"); // a date
    expect(agoRu(at(-10), now)).toBe("только что"); // a clock a little ahead is not "in the future"
    expect(agoRu(null, now)).toBe("");
    expect(agoRu("not a date", now)).toBe("");
  });
});

describe("groupSteps", () => {
  const cmd = (key: string): FeedEntry => ({ type: "command", key, command: "ls", output: "", exitCode: 0, running: false, status: "completed", user: false });
  const files = (key: string): FeedEntry => ({ type: "files", key, changes: [], running: false, status: "completed" });
  const tool = (key: string): FeedEntry => ({ type: "tool", key, title: "web", detail: "", running: false });
  const msg = (key: string): FeedEntry => ({ type: "message", key, text: "hi", live: false });
  const user = (key: string): FeedEntry => ({ type: "user", key, text: "do it", steer: false });

  it("joins steps that follow one another and leaves everything else alone", () => {
    const blocks = groupSteps([user("u1"), msg("m1"), cmd("c1"), files("f1"), tool("t1"), msg("m2"), cmd("c2")]);
    expect(blocks.map((b) => (b.type === "steps" ? b.entries.map((e) => e.key) : b.entry.key))).toEqual(["u1", "m1", ["c1", "f1", "t1"], "m2", ["c2"]]);
  });
  it("gives every block a stable key of its own", () => {
    const blocks = groupSteps([cmd("c1"), cmd("c2"), msg("m1"), cmd("c3")]);
    expect(blocks.map((b) => b.key)).toEqual(["g:c1", "m1", "g:c3"]);
    expect(new Set(blocks.map((b) => b.key)).size).toBe(blocks.length);
  });
  it("does not change the feed it was given", () => {
    const feed = [cmd("c1"), cmd("c2")];
    groupSteps(feed);
    expect(feed.map((e) => e.key)).toEqual(["c1", "c2"]);
    expect(groupSteps([])).toEqual([]);
  });
  it("tells the user's side of the chat from the agent's", () => {
    const [u, a, m, g] = groupSteps([user("u1"), { type: "action", key: "x1", text: "Сжать контекст" }, msg("m1"), cmd("c1")]);
    expect([u, a, m, g].map(isUserBlock)).toEqual([true, true, false, false]);
  });
});

describe("applyQuick", () => {
  it("puts a slash command in front of what is typed", () => {
    expect(applyQuick("", "/goal")).toBe("/goal ");
    expect(applyQuick("зелёные тесты", "/goal")).toBe("/goal зелёные тесты");
  });
  it("replaces a command that is already there instead of stacking them", () => {
    expect(applyQuick("/review main", "/goal")).toBe("/goal main");
    expect(applyQuick("!ls -la", "/compact")).toBe("/compact ls -la");
    expect(applyQuick("/goal x", "!")).toBe("!x");
  });
  it("turns the text into a command of your own", () => {
    expect(applyQuick("ls -la", "!")).toBe("!ls -la");
    expect(applyQuick("", "!")).toBe("!");
    expect(applyQuick("!whoami", "!")).toBe("!whoami");
  });
});
