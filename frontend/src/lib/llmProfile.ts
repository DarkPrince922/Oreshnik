/** Pure logic behind the LLM-profile form's "Загрузить модели" button, kept out of the
 * component so it can be unit-tested the way the rest of src/lib is (the component itself
 * is covered by hand-driven e2e against the real stack, not by a component-test harness). */

import type { Provider } from "./api";

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
