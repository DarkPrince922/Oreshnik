/** Pure logic behind the «Настройки» page: what is typed in a field, what counts as a change, what goes to the server. */

import type { AppSettingItem, AppSettingsGroup, SettingValue } from "./api";

/** What each field holds while it is edited: the text as typed (numbers stay text until sent), or a boolean. */
export type Draft = Record<string, string | boolean>;

export function toDraft(groups: AppSettingsGroup[]): Draft {
  const draft: Draft = {};
  for (const item of groups.flatMap((g) => g.items)) {
    draft[item.key] = item.kind === "bool" ? Boolean(item.value) : String(item.value);
  }
  return draft;
}

/** The value a field holds as the server expects it, or an error to show next to it. */
export function parseField(item: AppSettingItem, raw: string | boolean | undefined): { value: SettingValue } | { error: string } {
  if (item.kind === "bool") return { value: raw === true };
  const text = typeof raw === "string" ? raw.trim() : "";
  if (item.kind === "choice") {
    return item.choices.includes(text.toLowerCase()) ? { value: text.toLowerCase() } : { error: `допустимо: ${item.choices.join(", ")}` };
  }
  if (item.kind === "text") return text ? { value: text } : { error: "не может быть пустым" };
  if (!/^-?\d+(\.\d+)?$/.test(text)) return { error: "нужно число" };
  const n = Number(text);
  if (item.kind === "int" && !Number.isInteger(n)) return { error: "нужно целое число" };
  if (item.minimum !== null && n < item.minimum) return { error: `не меньше ${item.minimum}` };
  if (item.maximum !== null && n > item.maximum) return { error: `не больше ${item.maximum}` };
  return { value: n };
}

export interface Plan {
  /** What to send: a value to set, or null to reset. Only fields that actually differ from what applies now. */
  changes: Record<string, SettingValue | null>;
  /** Field -> what is wrong with it; while there is any, nothing should be sent. */
  errors: Record<string, string>;
}

export function planSave(groups: AppSettingsGroup[], draft: Draft, resets: Set<string>): Plan {
  const changes: Record<string, SettingValue | null> = {};
  const errors: Record<string, string> = {};
  for (const item of groups.flatMap((g) => g.items)) {
    if (resets.has(item.key)) {
      if (item.changed) changes[item.key] = null;
      continue;
    }
    const parsed = parseField(item, draft[item.key]);
    if ("error" in parsed) {
      errors[item.key] = parsed.error;
    } else if (parsed.value !== item.value) {
      changes[item.key] = parsed.value;
    }
  }
  return { changes, errors };
}

/** How a default is shown next to a field. */
export function describeDefault(item: AppSettingItem): string {
  const v = item.kind === "bool" ? (item.default ? "включено" : "выключено") : String(item.default);
  return `${v}${item.unit && item.kind !== "bool" ? " " + item.unit : ""}`;
}

export function restartNote(item: AppSettingItem): string | null {
  if (!item.restart) return null;
  return `Применится после перезапуска (${item.restart}): docker compose restart ${item.restart}`;
}
