/** Choosing "profile + model" for a scan, a schedule or a continuation: any model of any of the user's profiles (providers).
 *
 * A profile has a default model and the list saved by «Загрузить модели». The default is always selectable (first), then the
 * saved ones, no repeats. A choice is the pair (profile, model); the select holds it as "<profile id>|<model>".
 */

import type { LLMProfile } from "./api";

export interface Choice {
  profileId: number;
  model: string;
}

type ModelSource = Pick<LLMProfile, "model" | "models">;

export function profileModels(p: ModelSource): string[] {
  return Array.from(new Set([p.model, ...(p.models ?? [])].filter((m) => typeof m === "string" && m !== "")));
}

export const encodeChoice = (c: Choice): string => `${c.profileId}|${c.model}`;

export function decodeChoice(value: string): Choice | null {
  const cut = value.indexOf("|"); // a profile id has no "|", a model id may
  if (cut < 1) return null;
  const profileId = Number(value.slice(0, cut));
  const model = value.slice(cut + 1);
  return Number.isInteger(profileId) && model !== "" ? { profileId, model } : null;
}

/** The first profile with its own default model. */
export function defaultChoice(profiles: LLMProfile[]): Choice | null {
  const first = profiles[0];
  return first ? { profileId: first.id, model: first.model } : null;
}

/** A choice made earlier (for a scan being continued, a schedule being edited), kept if it still exists: the same profile with
 * the same model; else the same profile's default; else the first profile's. Never a pair that is not on offer. */
export function resolveChoice(profiles: LLMProfile[], profileId: number | null | undefined, model: string | null | undefined): Choice | null {
  const profile = profiles.find((p) => p.id === profileId);
  if (!profile) return defaultChoice(profiles);
  return { profileId: profile.id, model: model && profileModels(profile).includes(model) ? model : profile.model };
}

/** What to send as `llm_model`: nothing when it is the profile's own default (so the scan follows the profile). */
export function modelToSend(profiles: LLMProfile[], choice: Choice | null): string | null {
  if (!choice) return null;
  const profile = profiles.find((p) => p.id === choice.profileId);
  return profile && choice.model !== profile.model ? choice.model : null;
}

export function totalModels(profiles: LLMProfile[]): number {
  return profiles.reduce((n, p) => n + profileModels(p).length, 0);
}

/** The models of a profile that match what is typed in the search box (the current choice always stays on the list). */
export function visibleModels(p: ModelSource, query: string, current: Choice | null, profileId: number): string[] {
  const q = query.trim().toLowerCase();
  return profileModels(p).filter((m) => !q || m.toLowerCase().includes(q) || (current?.profileId === profileId && current.model === m));
}
