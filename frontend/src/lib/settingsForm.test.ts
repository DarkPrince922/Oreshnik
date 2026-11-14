import { describe, expect, it } from "vitest";

import type { AppSettingItem, AppSettingsGroup } from "./api";
import { describeDefault, parseField, planSave, restartNote, toDraft } from "./settingsForm";

const item = (over: Partial<AppSettingItem>): AppSettingItem => ({
  key: "scan_concurrency", env: "SCAN_CONCURRENCY", label: "Сканов одновременно", hint: "", kind: "int", minimum: 1, maximum: 64, choices: [],
  unit: "", restart: "", value: 2, default: 2, changed: false, updated_at: null, ...over,
});
const groups = (...items: AppSettingItem[]): AppSettingsGroup[] => [{ name: "Сканы", items }];

describe("parseField", () => {
  it("accepts a number inside the bounds", () => {
    expect(parseField(item({}), " 5 ")).toEqual({ value: 5 });
  });
  it("refuses text, fractions for an integer, and values outside the bounds", () => {
    expect(parseField(item({}), "abc")).toEqual({ error: "нужно число" });
    expect(parseField(item({}), "")).toEqual({ error: "нужно число" });
    expect(parseField(item({}), "2.5")).toEqual({ error: "нужно целое число" });
    expect(parseField(item({}), "0")).toEqual({ error: "не меньше 1" });
    expect(parseField(item({}), "65")).toEqual({ error: "не больше 64" });
  });
  it("allows a fraction for a float", () => {
    expect(parseField(item({ kind: "float", minimum: 0.5, maximum: 60 }), "1.5")).toEqual({ value: 1.5 });
  });
  it("reads a switch, a choice and a text", () => {
    expect(parseField(item({ kind: "bool" }), true)).toEqual({ value: true });
    expect(parseField(item({ kind: "bool" }), false)).toEqual({ value: false });
    const choice = item({ kind: "choice", choices: ["low", "high"] });
    expect(parseField(choice, " HIGH ")).toEqual({ value: "high" });
    expect(parseField(choice, "mid")).toEqual({ error: "допустимо: low, high" });
    expect(parseField(item({ kind: "text" }), " 1.8.0 ")).toEqual({ value: "1.8.0" });
    expect(parseField(item({ kind: "text" }), " ")).toEqual({ error: "не может быть пустым" });
  });
});

describe("planSave", () => {
  it("sends only what differs from what applies now", () => {
    const g = groups(item({}), item({ key: "max_schedules_per_user", value: 20, default: 20 }));
    const draft = toDraft(g);
    draft.scan_concurrency = "6";
    expect(planSave(g, draft, new Set())).toEqual({ changes: { scan_concurrency: 6 }, errors: {} });
  });
  it("sends nothing when nothing was touched", () => {
    const g = groups(item({}));
    expect(planSave(g, toDraft(g), new Set())).toEqual({ changes: {}, errors: {} });
  });
  it("reports every invalid field and keeps valid ones out of the way", () => {
    const g = groups(item({}), item({ key: "max_schedules_per_user", value: 20, default: 20 }));
    const draft = toDraft(g);
    draft.scan_concurrency = "0";
    draft.max_schedules_per_user = "30";
    const plan = planSave(g, draft, new Set());
    expect(plan.errors).toEqual({ scan_concurrency: "не меньше 1" });
    expect(plan.changes).toEqual({ max_schedules_per_user: 30 });
  });
  it("resets only parameters that were really changed, whatever is typed in them", () => {
    const g = groups(item({ value: 6, changed: true }), item({ key: "max_schedules_per_user", value: 20, default: 20 }));
    const draft = toDraft(g);
    draft.scan_concurrency = "999";
    expect(planSave(g, draft, new Set(["scan_concurrency", "max_schedules_per_user"]))).toEqual({
      changes: { scan_concurrency: null },
      errors: {},
    });
  });
  it("sends a switch that was turned on", () => {
    const g = groups(item({ key: "strix_telemetry", kind: "bool", value: false, default: false, minimum: null, maximum: null }));
    const draft = toDraft(g);
    draft.strix_telemetry = true;
    expect(planSave(g, draft, new Set()).changes).toEqual({ strix_telemetry: true });
  });
});

describe("labels", () => {
  it("shows the default with its unit, and a switch in words", () => {
    expect(describeDefault(item({ unit: "мин", default: 180 }))).toBe("180 мин");
    expect(describeDefault(item({ kind: "bool", default: false }))).toBe("выключено");
  });
  it("only a parameter that needs a restart says so", () => {
    expect(restartNote(item({}))).toBeNull();
    expect(restartNote(item({ restart: "beat" }))).toContain("docker compose restart beat");
  });
});
