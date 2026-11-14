import { describe, expect, it } from "vitest";

import { pageRange, scansSummary, sections, tileCount, TILES } from "./scansList";

const counts = { queued: 1, running: 2, cancelling: 0, succeeded: 22, vulns_found: 14, failed: 3, cancelled: 6 };

describe("tiles", () => {
  it("counts every status, and all of them together", () => {
    expect(tileCount(counts, "all")).toBe(48);
    expect(tileCount(counts, "vulns_found")).toBe(14);
    expect(tileCount({}, "all")).toBe(0);
    expect(tileCount({ running: 1 }, "failed")).toBe(0);
  });
  it("has one tile per thing a user filters by, «Все» first", () => {
    expect(TILES.map((t) => t.value)).toEqual(["all", "running", "queued", "vulns_found", "succeeded", "failed", "cancelled"]);
  });
  it("shows scans in progress as cards and finished ones as history", () => {
    expect(sections("all")).toEqual({ live: true, history: true });
    expect(sections("running")).toEqual({ live: true, history: false });
    expect(sections("queued")).toEqual({ live: true, history: false });
    for (const f of ["vulns_found", "succeeded", "failed", "cancelled"] as const) expect(sections(f)).toEqual({ live: false, history: true });
  });
});

describe("labels", () => {
  it("sums the list up in one line", () => {
    expect(scansSummary(counts)).toBe("48 сканов · 2 выполняются, 1 в очереди");
    expect(scansSummary({ running: 1 })).toBe("1 скан · 1 выполняется");
    expect(scansSummary({ running: 1, cancelling: 1, succeeded: 1 })).toBe("3 скана · 2 выполняются"); // one being stopped still runs
    expect(scansSummary({ succeeded: 5 })).toBe("5 сканов");
    expect(scansSummary({})).toBe("0 сканов");
  });
  it("says which rows a page holds", () => {
    expect(pageRange(0, 15, 45)).toBe("1–15 из 45");
    expect(pageRange(2, 15, 40)).toBe("31–40 из 40");
    expect(pageRange(0, 15, 3)).toBe("1–3 из 3");
    expect(pageRange(0, 15, 0)).toBe("");
  });
});
