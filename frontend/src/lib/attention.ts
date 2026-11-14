/** The lights on «Сканы» and «Агент» in the menu: lit when something happened since the section was last opened, off once it is opened.
 *
 * "Last opened" is remembered in this browser (per account), as the SERVER's time, so a wrong clock on the computer does not matter.
 */

import type { Attention } from "./api";

export type Section = "scans" | "agent";
export type Tone = "ok" | "warn" | "bad" | "accent";
export interface Light {
  tone: Tone;
  /** The agent is waiting for an answer: the light pulses. */
  pulse: boolean;
  title: string;
}

export const SECTION_PATH: Record<Section, string> = { scans: "/scans", agent: "/agent" };

export function sectionOf(pathname: string): Section | null {
  if (pathname === "/scans" || pathname.startsWith("/scans/")) return "scans";
  if (pathname === "/agent" || pathname.startsWith("/agent/")) return "agent";
  return null;
}

const key = (userId: number, section: Section) => `oreshnik_seen_${userId}_${section}`;

export function readSeen(userId: number, section: Section, storage: Pick<Storage, "getItem"> | null = safeStorage()): string | null {
  try {
    const value = storage?.getItem(key(userId, section)) ?? null;
    return value && !Number.isNaN(Date.parse(value)) ? value : null;
  } catch {
    return null;
  }
}

export function writeSeen(userId: number, section: Section, when: string, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    storage?.setItem(key(userId, section), when);
  } catch {
    /* storage unavailable: the light simply stays until the page is reloaded */
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

const plural = (n: number, one: string, few: string, many: string) => {
  const d = n % 10;
  const h = n % 100;
  return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
};

/** Which lights are lit. The section the user is looking at right now never has one. */
export function lights(attention: Attention | null, current: Section | null): Partial<Record<Section, Light>> {
  const out: Partial<Record<Section, Light>> = {};
  if (!attention) return out;
  const { scans, agent } = attention;
  if (current !== "scans" && scans.finished > 0) {
    const tone: Tone = scans.failed > 0 ? "bad" : scans.findings > 0 ? "warn" : "ok";
    const parts = [`${scans.finished} ${plural(scans.finished, "скан завершён", "скана завершено", "сканов завершено")}`];
    if (scans.findings > 0) parts.push(`с находками: ${scans.findings}`);
    if (scans.failed > 0) parts.push(`с ошибкой: ${scans.failed}`);
    out.scans = { tone, pulse: false, title: parts.join(", ") };
  }
  if (current !== "agent" && (agent.waiting > 0 || agent.finished > 0)) {
    const parts: string[] = [];
    if (agent.waiting > 0) parts.push(`агент ждёт вашего ответа${agent.waiting > 1 ? ` (${agent.waiting})` : ""}`);
    if (agent.finished > 0) parts.push(`агент закончил${agent.finished > 1 ? ` (${agent.finished})` : ""}`);
    out.agent = { tone: agent.waiting > 0 ? "warn" : "accent", pulse: agent.waiting > 0, title: parts.join(", ") };
  }
  return out;
}
