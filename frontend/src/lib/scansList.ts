/** Pure helpers behind the «Сканы» page: the tiles, what to load for a tile, and the labels. */
import { pluralRu } from "./agentSessions";
import type { ScanStatus } from "./api";

export type ScanFilter = ScanStatus | "all";
export type Counts = Partial<Record<ScanStatus, number>>;

export const TILES: { value: ScanFilter; label: string; tone: "accent" | "bad" | "ok" | "muted" }[] = [
  { value: "all", label: "Все", tone: "muted" },
  { value: "running", label: "Выполняются", tone: "accent" },
  { value: "queued", label: "В очереди", tone: "muted" },
  { value: "vulns_found", label: "С уязвимостями", tone: "bad" },
  { value: "succeeded", label: "Без находок", tone: "ok" },
  { value: "failed", label: "Ошибки", tone: "bad" },
  { value: "cancelled", label: "Отменённые", tone: "muted" },
];

const ALL: ScanStatus[] = ["queued", "running", "cancelling", "succeeded", "vulns_found", "failed", "cancelled"];

export const tileCount = (counts: Counts, tile: ScanFilter): number => (tile === "all" ? ALL.reduce((n, s) => n + (counts[s] ?? 0), 0) : counts[tile] ?? 0);

/** Which lists a tile shows: the scans in progress (cards on top), the finished ones (the history table), or both. */
export function sections(filter: ScanFilter): { live: boolean; history: boolean } {
  if (filter === "all") return { live: true, history: true };
  const live = filter === "running" || filter === "queued" || filter === "cancelling";
  return { live, history: !live };
}

/** The line under the title: "48 сканов · 2 выполняются, 1 в очереди". */
export function scansSummary(counts: Counts): string {
  const total = tileCount(counts, "all");
  const head = `${total} ${pluralRu(total, ["скан", "скана", "сканов"])}`;
  const running = (counts.running ?? 0) + (counts.cancelling ?? 0);
  const queued = counts.queued ?? 0;
  const parts: string[] = [];
  if (running > 0) parts.push(`${running} ${pluralRu(running, ["выполняется", "выполняются", "выполняются"])}`);
  if (queued > 0) parts.push(`${queued} в очереди`);
  return parts.length ? `${head} · ${parts.join(", ")}` : head;
}

/** "1–15 из 45" for a page of a list; "" when the list is empty. */
export function pageRange(page: number, size: number, total: number): string {
  if (total <= 0) return "";
  const first = page * size + 1;
  return `${first}–${Math.min(total, first + size - 1)} из ${total}`;
}

export const SEVERITY_LETTER: Record<string, string> = { critical: "C", high: "H", medium: "M", low: "L", info: "I", unknown: "?" };
