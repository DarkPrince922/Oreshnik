/** Pure logic behind the LLM-profile form's "Загрузить модели" button, kept out of the
 * component so it can be unit-tested the way the rest of src/lib is (the component itself
 * is covered by hand-driven e2e against the real stack, not by a component-test harness). */

import type { Provider } from "./api";

// Mirrors backend/app/services/llm.py::strix_model_id (the LiteLLM route prefix of each provider).
const PREFIX: Record<Provider, string> = { claude: "anthropic", openai: "openai", grok: "xai", custom: "openai" };

/** Whether a fetch can be attempted right now: a typed, not-yet-saved key always works; while
 * editing without retyping one, the profile's own stored key is reused instead — but only for
 * the provider that profile actually belongs to (a provider switched in the form no longer
 * matches the profile whose key would be reused, so the attempt would only earn a 422). */
export function canFetchModels(args: {
  provider: Provider;
  baseUrl: string;
  apiKey: string;
  editingProvider: Provider | null;
}): boolean {
  if (args.provider === "custom" && !args.baseUrl.trim()) return false;
  if (args.apiKey.trim()) return true;
  return args.editingProvider === args.provider;
}

/** Datalist options: the preset's own suggestions plus whatever the provider just reported,
 * deduplicated and in a stable order (fetched models first — they are what is actually
 * available right now) without ever discarding either side. */
export function mergeModelOptions(suggestions: string[], fetched: string[]): string[] {
  return Array.from(new Set([...fetched, ...suggestions]));
}

/** What Strix is given as STRIX_LLM. For "custom" the prefix is always added (the id is whatever the endpoint calls it);
 * for the fixed providers an id already typed fully qualified is left alone. */
export function strixModelId(provider: Provider, model: string): string {
  const prefix = PREFIX[provider];
  if (provider !== "custom" && model.startsWith(prefix + "/")) return model;
  return `${prefix}/${model}`;
}

/** What the provider's API is actually asked for: Strix drops its route prefix (the first "openai/", "anthropic/", ...)
 * before the request goes out — checked against the real Strix 1.7.0 with a logging endpoint — so the prefix in
 * "Для Strix: openai/kimi-k3.1-pro" never reaches the provider. */
export function requestedModelId(provider: Provider, model: string): string {
  const id = strixModelId(provider, model);
  return id.slice(id.indexOf("/") + 1);
}

/** The hint under the model field. Says both, because "openai/…" next to a model that has no such prefix looks like a bug. */
export function modelHint(provider: Provider, model: string): string | undefined {
  const typed = model.trim();
  if (!typed) return undefined;
  return `Для Strix: ${strixModelId(provider, typed)} · провайдеру уйдёт: ${requestedModelId(provider, typed)}`;
}
