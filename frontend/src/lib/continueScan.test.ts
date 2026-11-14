import { describe, expect, it } from "vitest";

import { budgetProblem, canContinueInPlace, continueKind, defaultBudget, instructionRequired, parseBudget } from "./continueScan";

describe("instructionRequired", () => {
  it("is needed after a scan that finished on its own, not after a stopped or failed one", () => {
    expect(instructionRequired({ status: "succeeded" })).toBe(true);
    expect(instructionRequired({ status: "vulns_found" })).toBe(true);
    expect(instructionRequired({ status: "cancelled" })).toBe(false);
    expect(instructionRequired({ status: "failed" })).toBe(false);
  });
});

describe("budgetProblem", () => {
  it("accepts an empty limit and anything above what was spent", () => {
    expect(budgetProblem("", 2)).toBeNull();
    expect(budgetProblem("  ", 2)).toBeNull();
    expect(budgetProblem("2.01", 2)).toBeNull();
    expect(budgetProblem("50", null)).toBeNull();
    expect(budgetProblem("5,5", 1)).toBeNull(); // decimal comma
  });
  it("refuses a limit at or below the spend, because Strix would stop at once", () => {
    expect(budgetProblem("2", 2)).toMatch(/2\.00/);
    expect(budgetProblem("1.5", 2)).toMatch(/уже потрачено/);
  });
  it("refuses nonsense", () => {
    for (const bad of ["0", "-1", "abc", "1e999999"]) expect(budgetProblem(bad, 0)).not.toBeNull();
  });
});

describe("defaultBudget / parseBudget", () => {
  it("re-uses the scan's limit only if it leaves room", () => {
    expect(defaultBudget({ max_budget: 10, cost: 3 })).toBe("10");
    expect(defaultBudget({ max_budget: 3, cost: 3 })).toBe("");
    expect(defaultBudget({ max_budget: null, cost: 3 })).toBe("");
    expect(defaultBudget({ max_budget: 5, cost: null })).toBe("5");
  });
  it("parses the field", () => {
    expect(parseBudget("")).toBeNull();
    expect(parseBudget("7,5")).toBe(7.5);
  });
});

describe("canContinueInPlace", () => {
  it("is for the owner only: the key is personal, so someone else's scan becomes a new scan", () => {
    expect(canContinueInPlace({ user_id: 7 }, 7)).toBe(true);
    expect(canContinueInPlace({ user_id: 7 }, 8)).toBe(false);
  });
  it("is false while the current user is unknown", () => {
    expect(canContinueInPlace({ user_id: 7 }, undefined)).toBe(false);
    expect(canContinueInPlace({ user_id: 7 }, null)).toBe(false);
  });
});

describe("continueKind", () => {
  const scan = (status: string, run_name: string | null, runner_name: string | null = "local") => ({ status, run_name, runner_name }) as Parameters<typeof continueKind>[0];

  it("resumes a scan that has a Strix run", () => {
    for (const status of ["succeeded", "vulns_found", "failed", "cancelled"]) expect(continueKind(scan(status, "run-1"))).toBe("resume");
  });
  it("restarts a scan that failed or was stopped before Strix created a run (e.g. the model was unreachable)", () => {
    expect(continueKind(scan("failed", null))).toBe("restart");
    expect(continueKind(scan("cancelled", null))).toBe("restart");
  });
  it("offers nothing for a scan that finished without a run, or whose runner is gone", () => {
    expect(continueKind(scan("succeeded", null))).toBeNull();
    expect(continueKind(scan("failed", null, null))).toBeNull();
    expect(continueKind(scan("failed", "run-1", null))).toBeNull();
  });
});
