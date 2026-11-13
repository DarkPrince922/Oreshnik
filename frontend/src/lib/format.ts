import type { Scan } from "./api";

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} ч ${m} мин`;
  if (m > 0) return `${m} мин ${s} с`;
  return `${s} с`;
}

/**
 * Time the scan was active, in ms; ticks while it is running (pass the current time). A scan continued in place restarts
 * its clock with every run, so the time of the earlier runs (`elapsed_base`) is added and the pauses in between are not.
 */
export function scanDuration(scan: Pick<Scan, "started_at" | "finished_at"> & { elapsed_base?: number }, now: number): number | null {
  const before = Math.max(0, (scan.elapsed_base ?? 0) * 1000);
  if (!scan.started_at) return before > 0 ? before : null;
  const end = scan.finished_at ? new Date(scan.finished_at).getTime() : now;
  return before + Math.max(0, end - new Date(scan.started_at).getTime());
}

export function formatCost(cost: number | null): string {
  if (cost === null) return "—";
  return cost > 0 && cost < 0.01 ? "<$0.01" : `$${cost.toFixed(2)}`;
}

export function formatTokens(n: number | null): string {
  if (n === null) return "—";
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function targetsLabel(targets: string[]): { first: string; more: number } {
  return { first: targets[0] ?? "—", more: Math.max(0, targets.length - 1) };
}

export function formatDateTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "medium" }) : "—";
}

/** "1 раз", "2 раза", "5 раз", "21 раз", "22 раза", "11 раз". */
export function timesRu(n: number): string {
  const last = n % 10;
  const lastTwo = n % 100;
  return last >= 2 && last <= 4 && !(lastTwo >= 12 && lastTwo <= 14) ? `${n} раза` : `${n} раз`;
}
