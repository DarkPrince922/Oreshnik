import { describe, expect, it } from "vitest";

import { CRON_PRESETS, budgetRequired, describeCron, formatWhen, normalizeCronInput, relativeIn, zoneOptions } from "./schedule";

describe("normalizeCronInput / describeCron", () => {
  it("collapses whitespace", () => {
    expect(normalizeCronInput("  0   3 *  * * ")).toBe("0 3 * * *");
    expect(normalizeCronInput("")).toBe("");
  });
  it("recognises the presets, even with sloppy spacing", () => {
    for (const preset of CRON_PRESETS) expect(describeCron(preset.cron)).toBe(preset.label);
    expect(describeCron("  0  3  * * * ")).toBe("Каждый день в 03:00");
  });
  it("returns null for anything else", () => {
    expect(describeCron("*/7 * * * *")).toBeNull();
    expect(describeCron("")).toBeNull();
  });
  it("has presets with five fields each", () => {
    for (const p of CRON_PRESETS) expect(p.cron.split(" ")).toHaveLength(5);
  });
});

describe("relativeIn", () => {
  const now = Date.parse("2026-11-13T12:00:00Z");
  const at = (secs: number) => new Date(now + secs * 1000).toISOString();
  it("describes the near future", () => {
    expect(relativeIn(at(10), now)).toBe("сейчас");
    expect(relativeIn(at(-60), now)).toBe("сейчас"); // overdue: it is about to fire
    expect(relativeIn(at(5 * 60), now)).toBe("через 5 мин");
    expect(relativeIn(at(3 * 3600), now)).toBe("через 3 ч");
    expect(relativeIn(at(3 * 86400), now)).toBe("через 3 дн");
  });
  it("is empty without a time", () => {
    expect(relativeIn(null, now)).toBe("");
  });
});

describe("formatWhen", () => {
  it("shows a dash without a time and formats in the requested zone", () => {
    expect(formatWhen(null)).toBe("—");
    // 00:00 UTC is 03:00 in Moscow
    expect(formatWhen("2026-01-01T00:00:00Z", "Europe/Moscow")).toContain("03:00");
    expect(formatWhen("2026-01-01T00:00:00Z", "UTC")).toContain("00:00");
  });
  it("does not throw on an unknown zone", () => {
    expect(() => formatWhen("2026-01-01T00:00:00Z", "Nowhere/Land")).not.toThrow();
  });
});

describe("zoneOptions", () => {
  it("lists each zone once and keeps the schedule's own zone available", () => {
    const zones = zoneOptions("Pacific/Auckland");
    expect(zones).toContain("Pacific/Auckland");
    expect(zones).toContain("UTC");
    expect(new Set(zones).size).toBe(zones.length);
  });
});

describe("budgetRequired", () => {
  it("is needed for anything that really calls a model", () => {
    expect(budgetRequired("local")).toBe(true);
    expect(budgetRequired("ssh")).toBe(true);
    expect(budgetRequired("mock")).toBe(false);
    expect(budgetRequired(null)).toBe(false);
    expect(budgetRequired(undefined)).toBe(false);
  });
});
