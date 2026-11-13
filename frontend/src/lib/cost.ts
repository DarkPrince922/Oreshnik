/** Pure helpers for money on screen: the per-scan budget bar and the bar chart scale. */

export type BudgetTone = "ok" | "warn" | "bad";

export interface BudgetState {
  spent: number;
  limit: number;
  /** Real ratio; can exceed 1 while Strix is still winding the scan down after the limit. */
  fraction: number;
  /** Rounded percentage for the label (not clamped). */
  percent: number;
  /** Ratio for the bar's width (clamped to 0..1). */
  barFraction: number;
  tone: BudgetTone;
}

export const WARN_AT = 0.8;

/** State of a scan's budget, or null when it has no limit. Strix compares the limit with the whole run's cost. */
export function budgetState(cost: number | null, limit: number | null): BudgetState | null {
  if (limit === null || !(limit > 0)) return null;
  const spent = Math.max(0, cost ?? 0);
  const fraction = spent / limit;
  return {
    spent,
    limit,
    fraction,
    percent: Math.round(fraction * 100),
    barFraction: Math.min(1, fraction),
    tone: fraction >= 1 ? "bad" : fraction >= WARN_AT ? "warn" : "ok",
  };
}

/** A sentence for the banner, or null when there is nothing to say. */
export function budgetMessage(state: BudgetState | null, active: boolean): string | null {
  if (state === null) return null;
  if (state.tone === "bad") {
    return active
      ? "Лимит бюджета достигнут — Strix сворачивает работу и остановит скан."
      : "Скан дошёл до лимита бюджета, поэтому мог остановиться раньше, чем закончил работу.";
  }
  if (state.tone === "warn" && active) {
    return "Скан подходит к лимиту бюджета (" + state.percent + "%): при достижении Strix остановит его.";
  }
  return null;
}

/** A "nice" upper bound for a chart axis: 1, 2, 5 times a power of ten. 0 for an empty chart gives a unit axis. */
export function niceMax(value: number): number {
  if (!(value > 0)) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const m = value / base;
  const nice = m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10;
  return nice * base;
}

export interface Bar {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Bars for a series inside a `width` x `height` box. Zero values still get no height, never a negative one. */
export function barLayout(values: readonly number[], width: number, height: number, max: number, gap = 0.25): Bar[] {
  if (values.length === 0) return [];
  const slot = width / values.length;
  const barWidth = slot * (1 - gap);
  return values.map((v, i) => {
    const h = max > 0 ? Math.max(0, Math.min(1, v / max)) * height : 0;
    return { x: i * slot + (slot - barWidth) / 2, y: height - h, width: barWidth, height: h };
  });
}

/** Short date label for the chart axis ("13.11"). The input is an ISO date such as 2026-11-13. */
export function shortDate(iso: string): string {
  const [, m, d] = iso.split("-");
  return d && m ? d + "." + m : iso;
}
