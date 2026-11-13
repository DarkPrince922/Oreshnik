import { describe, expect, it } from "vitest";

import { canFetchModels, mergeModelOptions } from "./llmProfile";

describe("canFetchModels", () => {
  it("needs base_url for custom, never for the fixed providers", () => {
    expect(canFetchModels({ provider: "custom", baseUrl: "", apiKey: "k", editingProvider: null })).toBe(false);
    expect(canFetchModels({ provider: "custom", baseUrl: "  ", apiKey: "k", editingProvider: null })).toBe(false);
    expect(canFetchModels({ provider: "custom", baseUrl: "http://x/v1", apiKey: "k", editingProvider: null })).toBe(true);
    expect(canFetchModels({ provider: "claude", baseUrl: "", apiKey: "k", editingProvider: null })).toBe(true);
  });

  it("a typed key is always enough, even while creating", () => {
    expect(canFetchModels({ provider: "openai", baseUrl: "", apiKey: "sk-x", editingProvider: null })).toBe(true);
  });

  it("with no typed key, falls back to the profile being edited — only for its own provider", () => {
    expect(canFetchModels({ provider: "claude", baseUrl: "", apiKey: "", editingProvider: "claude" })).toBe(true);
    expect(canFetchModels({ provider: "claude", baseUrl: "", apiKey: "   ", editingProvider: "claude" })).toBe(true);
    // the form was switched to a different provider than the profile being edited: that
    // profile's key is not reusable for it, so nothing is sendable yet
    expect(canFetchModels({ provider: "openai", baseUrl: "", apiKey: "", editingProvider: "claude" })).toBe(false);
  });

  it("with neither a typed key nor a profile being edited, nothing can be sent", () => {
    expect(canFetchModels({ provider: "openai", baseUrl: "", apiKey: "", editingProvider: null })).toBe(false);
  });
});

describe("mergeModelOptions", () => {
  it("keeps both sides, fetched ones first", () => {
    expect(mergeModelOptions(["a", "b"], ["c"])).toEqual(["c", "a", "b"]);
  });

  it("drops duplicates without reordering the first occurrence", () => {
    expect(mergeModelOptions(["a", "b"], ["b", "c"])).toEqual(["b", "c", "a"]);
  });

  it("works with either side empty", () => {
    expect(mergeModelOptions([], ["x"])).toEqual(["x"]);
    expect(mergeModelOptions(["x"], [])).toEqual(["x"]);
    expect(mergeModelOptions([], [])).toEqual([]);
  });
});
