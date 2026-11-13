import { describe, expect, it } from "vitest";

import { budgetProblem, canContinueInPlace, defaultBudget, instructionRequired, parseBudget } from "./continueScan";

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
