import { describe, expect, it } from "vitest";

import type { LLMProfile } from "./api";
import { decodeChoice, defaultChoice, encodeChoice, modelToSend, profileModels, resolveChoice, totalModels, visibleModels } from "./modelChoice";

const profile = (over: Partial<LLMProfile>): LLMProfile => ({
  id: 1, name: "DeepSeek", provider: "custom", model: "deepseek-flash", base_url: "http://gw/v1", models: [], api_key_hint: "…1234",
  strix_model: "openai/deepseek-flash", created_at: "", updated_at: "", ...over,
});

describe("profileModels", () => {
  it("puts the default first, then the saved list, without repeats", () => {
    expect(profileModels(profile({ models: ["deepseek-v4-pro", "deepseek-flash", "x"] }))).toEqual(["deepseek-flash", "deepseek-v4-pro", "x"]);
  });
  it("always offers the default, even with nothing saved", () => {
    expect(profileModels(profile({ models: [] }))).toEqual(["deepseek-flash"]);
  });
  it("copes with a profile that has no list at all", () => {
    expect(profileModels({ model: "m", models: undefined as unknown as string[] })).toEqual(["m"]);
  });
});

describe("encode / decode", () => {
  it("round-trips, including a model id that contains the separator", () => {
    for (const model of ["gpt-5", "x/y:z", "weird|id", "llama3.1:70b"]) {
      expect(decodeChoice(encodeChoice({ profileId: 12, model }))).toEqual({ profileId: 12, model });
    }
  });
  it("refuses a value that is not a choice", () => {
    expect(decodeChoice("")).toBeNull();
    expect(decodeChoice("abc|m")).toBeNull();
    expect(decodeChoice("5|")).toBeNull();
    expect(decodeChoice("nopipe")).toBeNull();
  });
});

describe("defaultChoice / resolveChoice", () => {
  const profiles = [profile({ id: 1, models: ["a", "b"] }), profile({ id: 2, name: "OpenAI", provider: "openai", model: "gpt-5", models: ["o3"] })];
  it("starts with the first profile's own model", () => {
    expect(defaultChoice(profiles)).toEqual({ profileId: 1, model: "deepseek-flash" });
    expect(defaultChoice([])).toBeNull();
  });
  it("keeps an earlier choice that is still on offer, of any provider", () => {
    expect(resolveChoice(profiles, 2, "o3")).toEqual({ profileId: 2, model: "o3" });
    expect(resolveChoice(profiles, 1, "b")).toEqual({ profileId: 1, model: "b" });
  });
  it("falls back to the profile's default when the model is gone or was never chosen", () => {
    expect(resolveChoice(profiles, 2, "gone")).toEqual({ profileId: 2, model: "gpt-5" });
    expect(resolveChoice(profiles, 2, null)).toEqual({ profileId: 2, model: "gpt-5" });
  });
  it("falls back to the first profile when the profile is gone", () => {
    expect(resolveChoice(profiles, 99, "o3")).toEqual({ profileId: 1, model: "deepseek-flash" });
    expect(resolveChoice(profiles, null, null)).toEqual({ profileId: 1, model: "deepseek-flash" });
    expect(resolveChoice([], 1, "a")).toBeNull();
  });
});

describe("modelToSend", () => {
  const profiles = [profile({ id: 1, models: ["a"] })];
  it("sends nothing for the profile's own default, so the scan follows the profile", () => {
    expect(modelToSend(profiles, { profileId: 1, model: "deepseek-flash" })).toBeNull();
    expect(modelToSend(profiles, null)).toBeNull();
  });
  it("sends any other model", () => {
    expect(modelToSend(profiles, { profileId: 1, model: "a" })).toBe("a");
  });
});

describe("totalModels / visibleModels", () => {
  const p = profile({ models: ["deepseek-v4-pro", "llama3.1:70b", "qwen"] });
  it("counts what is on offer across profiles", () => {
    expect(totalModels([p, profile({ id: 2, model: "gpt-5", models: ["o3"] })])).toBe(6);
  });
  it("filters by what is typed, ignoring case", () => {
    expect(visibleModels(p, "DEEP", null, 1)).toEqual(["deepseek-flash", "deepseek-v4-pro"]);
    expect(visibleModels(p, "", null, 1)).toHaveLength(4);
    expect(visibleModels(p, "zzz", null, 1)).toEqual([]);
  });
  it("never hides the model that is currently chosen", () => {
    expect(visibleModels(p, "zzz", { profileId: 1, model: "qwen" }, 1)).toEqual(["qwen"]);
    expect(visibleModels(p, "zzz", { profileId: 2, model: "qwen" }, 1)).toEqual([]);
  });
});
