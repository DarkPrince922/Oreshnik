import { describe, expect, it } from "vitest";

import { barLayout, budgetMessage, budgetState, niceMax, shortDate } from "./cost";

describe("budgetState", () => {
  it("is null when there is no limit", () => {
    expect(budgetState(1.2, null)).toBeNull();
    expect(budgetState(1.2, 0)).toBeNull();
    expect(budgetState(1.2, -5)).toBeNull();
  });

  it("goes ok -> warn at 80% -> bad at 100%", () => {
    expect(budgetState(1, 10)?.tone).toBe("ok");
    expect(budgetState(7.99, 10)?.tone).toBe("ok");
    expect(budgetState(8, 10)?.tone).toBe("warn");
    expect(budgetState(9.99, 10)?.tone).toBe("warn");
    expect(budgetState(10, 10)?.tone).toBe("bad");
    expect(budgetState(12, 10)?.tone).toBe("bad");
  });

  it("keeps the label honest but clamps the bar", () => {
    const over = budgetState(12.5, 10)!;
    expect(over.percent).toBe(125);
    expect(over.barFraction).toBe(1);
    expect(budgetState(2.5, 10)!.barFraction).toBe(0.25);
  });

  it("treats an unknown or negative cost as nothing spent", () => {
    expect(budgetState(null, 10)).toMatchObject({ spent: 0, percent: 0, tone: "ok" });
    expect(budgetState(-3, 10)!.spent).toBe(0);
  });
});

describe("budgetMessage", () => {
  it("says nothing while all is well", () => {
    expect(budgetMessage(budgetState(1, 10), true)).toBeNull();
    expect(budgetMessage(null, true)).toBeNull();
  });
  it("warns a running scan close to its limit, and only then", () => {
    expect(budgetMessage(budgetState(8.5, 10), true)).toMatch(/подходит к лимиту.*85%/);
    expect(budgetMessage(budgetState(8.5, 10), false)).toBeNull(); // a finished scan under its limit needs no warning
  });
  it("reports a reached limit differently while running and afterwards", () => {
    expect(budgetMessage(budgetState(10, 10), true)).toMatch(/достигнут/);
    expect(budgetMessage(budgetState(10, 10), false)).toMatch(/мог остановиться раньше/);
  });
});

describe("niceMax", () => {
  it("rounds up to 1, 2, 5 times a power of ten", () => {
    expect(niceMax(0.7)).toBe(1);
    expect(niceMax(1.3)).toBe(2);
    expect(niceMax(3.2)).toBe(5);
    expect(niceMax(5.01)).toBe(10);
    expect(niceMax(120)).toBe(200);
    expect(niceMax(0.03)).toBeCloseTo(0.05);
  });
  it("never returns zero, so a chart of zeros still has an axis", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(-1)).toBe(1);
  });
});

describe("barLayout", () => {
  it("scales bars into the box and keeps them inside it", () => {
    const bars = barLayout([0, 5, 10], 300, 100, 10);
    expect(bars).toHaveLength(3);
    expect(bars.map((b) => b.height)).toEqual([0, 50, 100]);
    expect(bars.every((b) => b.y >= 0 && b.y + b.height <= 100 && b.x >= 0 && b.x + b.width <= 300)).toBe(true);
    expect(bars[2].y).toBe(0);
  });
  it("copes with a zero maximum, negatives and values above the maximum", () => {
    expect(barLayout([0, 0], 100, 50, 0).map((b) => b.height)).toEqual([0, 0]);
    expect(barLayout([-4, 20], 100, 50, 10).map((b) => b.height)).toEqual([0, 50]);
    expect(barLayout([], 100, 50, 10)).toEqual([]);
  });
});

describe("shortDate", () => {
  it("formats an ISO date for an axis", () => {
    expect(shortDate("2026-11-13")).toBe("13.11");
    expect(shortDate("junk")).toBe("junk");
  });
});
