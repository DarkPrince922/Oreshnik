/** Pure helpers behind «Личности»: which personas fit a section, and how one is labelled. */
import type { Persona, PersonaScope } from "./api";

export const SCOPE_LABEL: Record<PersonaScope, string> = { both: "Код и Агент", code: "только Код", agent: "только Агент" };

/** The personas that can be picked in a section, the user's own first, then the shared ones, each by name. */
export function personasFor(list: readonly Persona[], where: "code" | "agent"): Persona[] {
  return list
    .filter((p) => p.scope === "both" || p.scope === where)
    .sort((a, b) => Number(b.mine) - Number(a.mine) || a.name.localeCompare(b.name, "ru"));
}

/** The persona a new session starts with when the user picks none: their own default, if it fits the section. */
export function defaultPersona(list: readonly Persona[], where: "code" | "agent"): Persona | null {
  return personasFor(list, where).find((p) => p.is_default && p.mine) ?? null;
}

/** The letter on a persona's badge. */
export const personaInitial = (name: string): string => (name.trim()[0] ?? "?").toUpperCase();
