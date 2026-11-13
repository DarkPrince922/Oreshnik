import type { Scan } from "./api";

/**
 * A scan continues in place (same scan, same id) only for its owner. The key is personal: an admin continuing somebody
 * else's scan would run it on their own key but bill the owner, so for them it always becomes a new scan.
 */
export const canContinueInPlace = (scan: Pick<Scan, "user_id">, userId: number | null | undefined): boolean =>
  userId !== null && userId !== undefined && scan.user_id === userId;

/** A scan that finished on its own has nothing left to do without a new instruction; a stopped or failed one can just be picked up again. */
export const instructionRequired = (scan: Pick<Scan, "status">): boolean =>
  scan.status === "succeeded" || scan.status === "vulns_found";

/**
 * Strix compares the budget with the cost of the WHOLE run (it is restored from the run being continued), so a limit at or
 * below what was already spent would stop the continuation immediately. Returns a message, or null if the input is fine.
 */
export function budgetProblem(input: string, spent: number | null): string | null {
  const text = input.trim();
  if (text === "") return null; // no limit
  const value = Number(text.replace(",", "."));
  if (!Number.isFinite(value) || value <= 0) return "Укажите положительное число";
  if (spent !== null && value <= spent) return `Лимит общий для всего прогона, а уже потрачено $${spent.toFixed(2)}. Укажите больше.`;
  return null;
}

/** Pre-fill the limit with the scan's own only if it still leaves room. */
export const defaultBudget = (scan: Pick<Scan, "max_budget" | "cost">): string =>
  scan.max_budget !== null && scan.max_budget > (scan.cost ?? 0) ? String(scan.max_budget) : "";

export const parseBudget = (input: string): number | null => (input.trim() === "" ? null : Number(input.trim().replace(",", ".")));
