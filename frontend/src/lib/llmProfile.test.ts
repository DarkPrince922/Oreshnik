import { describe, expect, it } from "vitest";

import { canFetchModels, mergeModelOptions, modelHint, requestedModelId, strixModelId } from "./llmProfile";

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

describe("strixModelId / requestedModelId", () => {
  it("for custom, always adds openai/ and sends the typed id untouched — whatever it looks like", () => {
    for (const typed of ["kimi-k3.1-pro", "customprovider/mymodel", "openai/gpt-oss-20b", "a/b/c"]) {
      expect(strixModelId("custom", typed)).toBe(`openai/${typed}`);
      expect(requestedModelId("custom", typed)).toBe(typed);
    }
  });

  it("for the fixed providers, does not prefix twice, and the provider is asked for the bare id", () => {
    expect(strixModelId("claude", "claude-sonnet-4-6")).toBe("anthropic/claude-sonnet-4-6");
    expect(strixModelId("claude", "anthropic/claude-sonnet-4-6")).toBe("anthropic/claude-sonnet-4-6");
    expect(requestedModelId("claude", "anthropic/claude-sonnet-4-6")).toBe("claude-sonnet-4-6");
    expect(strixModelId("grok", "grok-4")).toBe("xai/grok-4");
    expect(requestedModelId("openai", "gpt-5.4")).toBe("gpt-5.4");
  });
});

describe("modelHint", () => {
  it("says what Strix is given AND what the provider receives, so a prefix is not mistaken for what is sent", () => {
    expect(modelHint("custom", "kimi-k3.1-pro")).toBe("Для Strix: openai/kimi-k3.1-pro · провайдеру уйдёт: kimi-k3.1-pro");
  });

  it("has no hint for an empty field", () => {
    expect(modelHint("custom", "")).toBeUndefined();
    expect(modelHint("custom", "   ")).toBeUndefined();
  });
});
