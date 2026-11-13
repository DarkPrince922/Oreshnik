/** The two limits on a runner's form and card, kept out of the component so they can be unit-tested like the rest of src/lib. */

import type { Runner, RunnerKind } from "./api";

/** The server's own caps (schemas/runner.py): a typo such as 100000 must not silently become "no limit". */
export const MAX_SCANS_PER_USER_CAP = 100;
export const MAX_TARGETS_CAP = 1000;

/** What a new SSH runner's "targets at once" field starts with. A starting point for the administrator to change,
 * not a recommendation: a host's capacity depends on the host. */
export const DEFAULT_SSH_TARGETS = "3";

export type LimitParse = { ok: true; value: number | null } | { ok: false; error: string };

/** Empty means "not set" (the panel default / no limit); anything else must be a whole number from 1 to `max`. */
export function parseLimit(raw: string, max: number, label: string): LimitParse {
  const text = raw.trim();
  if (text === "") return { ok: true, value: null };
  if (!/^\d+$/.test(text)) return { ok: false, error: `${label}: нужно целое число от 1 до ${max}` };
  const value = Number(text);
  if (value < 1 || value > max) return { ok: false, error: `${label}: нужно целое число от 1 до ${max}` };
  return { ok: true, value };
}

/** The value a "targets at once" field starts with for a runner kind — only until the administrator touches it. */
export function defaultTargetsField(kind: RunnerKind): string {
  return kind === "ssh" ? DEFAULT_SSH_TARGETS : "";
}

/** One line for the card, visible to everyone — it explains why a scan was refused. */
export function limitsLine(r: Pick<Runner, "max_scans_per_user" | "effective_max_scans_per_user" | "max_targets">): string {
  const scans = `сканов на аккаунт: ${r.effective_max_scans_per_user}${r.max_scans_per_user === null ? " (по умолчанию)" : ""}`;
  const targets = r.max_targets === null ? "целей одновременно: без лимита" : `целей одновременно: ${r.max_targets}`;
  return `${scans} · ${targets}`;
}
