import { describe, expect, it } from "vitest";

import { DEFAULT_SSH_TARGETS, defaultTargetsField, limitsLine, parseLimit } from "./runnerLimits";

describe("parseLimit", () => {
  it("treats an empty field as 'not set'", () => {
    expect(parseLimit("", 100, "x")).toEqual({ ok: true, value: null });
    expect(parseLimit("   ", 100, "x")).toEqual({ ok: true, value: null });
  });

  it("accepts whole numbers from 1 to the cap", () => {
    expect(parseLimit("1", 100, "x")).toEqual({ ok: true, value: 1 });
    expect(parseLimit(" 7 ", 100, "x")).toEqual({ ok: true, value: 7 });
    expect(parseLimit("100", 100, "x")).toEqual({ ok: true, value: 100 });
  });

  it("refuses zero, negatives, decimals, text and anything over the cap — never silently turning them into 'no limit'", () => {
    for (const bad of ["0", "-1", "2.5", "1e3", "abc", "101", "١٢"]) {
      const r = parseLimit(bad, 100, "Целей");
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toContain("Целей");
    }
  });
});

describe("defaultTargetsField", () => {
  it("prefills only for an SSH host", () => {
    expect(defaultTargetsField("ssh")).toBe(DEFAULT_SSH_TARGETS);
    expect(defaultTargetsField("local")).toBe("");
    expect(defaultTargetsField("mock")).toBe("");
  });
});

describe("limitsLine", () => {
  it("says when the per-account number is only the default", () => {
    expect(limitsLine({ max_scans_per_user: null, effective_max_scans_per_user: 3, max_targets: null })).toBe(
      "сканов на аккаунт: 3 (по умолчанию) · целей одновременно: без лимита"
    );
  });

  it("shows explicit values as they are", () => {
    expect(limitsLine({ max_scans_per_user: 5, effective_max_scans_per_user: 5, max_targets: 4 })).toBe(
      "сканов на аккаунт: 5 · целей одновременно: 4"
    );
  });
});
