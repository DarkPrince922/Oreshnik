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

/** Wall-clock run time; ticks while the scan is still running (pass the current time). */
export function scanDuration(scan: Pick<Scan, "started_at" | "finished_at">, now: number): number | null {
  if (!scan.started_at) return null;
  const end = scan.finished_at ? new Date(scan.finished_at).getTime() : now;
  return end - new Date(scan.started_at).getTime();
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
