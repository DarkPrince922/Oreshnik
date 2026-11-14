/** Pure helpers behind the «Агент» page: the session list (search, filters, labels) and the grouping of the agent's steps in the feed. */
import type { FeedEntry } from "./agentModel";
import type { AgentSession } from "./api";

export type SessionFilter = "all" | "running" | "error";
type ListItem = Pick<AgentSession, "title" | "status" | "last_error" | "llm_model">;

const isRunning = (s: Pick<AgentSession, "status">) => s.status === "running";
const hasError = (s: Pick<AgentSession, "last_error">) => Boolean(s.last_error);

export function sessionCounts(sessions: readonly ListItem[]): Record<SessionFilter, number> {
  return { all: sessions.length, running: sessions.filter(isRunning).length, error: sessions.filter(hasError).length };
}

/** The sessions a filter and a search line leave: the search looks in the title and the model, ignoring case. */
export function filterSessions<T extends ListItem>(sessions: readonly T[], filter: SessionFilter, query: string): T[] {
  const q = query.trim().toLowerCase();
  return sessions.filter(
    (s) =>
      (filter === "all" || (filter === "running" ? isRunning(s) : hasError(s))) &&
      (q === "" || s.title.toLowerCase().includes(q) || (s.llm_model ?? "").toLowerCase().includes(q))
  );
}

/** Russian plural: `pluralRu(2, ["ход", "хода", "ходов"])` → "хода". */
export function pluralRu(n: number, forms: readonly [string, string, string]): string {
  const last = Math.abs(n) % 10;
  const lastTwo = Math.abs(n) % 100;
  if (lastTwo >= 11 && lastTwo <= 14) return forms[2];
  return last === 1 ? forms[0] : last >= 2 && last <= 4 ? forms[1] : forms[2];
}

export const turnsRu = (n: number): string => `${n} ${pluralRu(n, ["ход", "хода", "ходов"])}`;

/** The line under the page title: how many sessions work and how many wait. Empty when there are none. */
export function sessionsSummary(sessions: readonly ListItem[]): string {
  if (sessions.length === 0) return "";
  const running = sessions.filter(isRunning).length;
  const idle = sessions.length - running;
  const parts: string[] = [];
  if (running > 0) parts.push(`${running} ${pluralRu(running, ["сессия работает", "сессии работают", "сессий работают"])}`);
  if (idle > 0) parts.push(running > 0 ? `${idle} ${pluralRu(idle, ["ждёт", "ждут", "ждут"])}` : `${idle} ${pluralRu(idle, ["сессия ждёт", "сессии ждут", "сессий ждут"])}`);
  return parts.join(", ");
}

/** "только что", "5 мин назад", "2 ч назад", "вчера", "3 дн назад", then the date. */
export function agoRu(iso: string | null | undefined, now: number): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const minutes = Math.floor((now - then) / 60_000);
  if (minutes < 1) return "только что";
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "вчера";
  if (days < 7) return `${days} дн назад`;
  return new Date(then).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

type StepEntry = Extract<FeedEntry, { type: "command" | "files" | "tool" }>;
export type FeedBlock = { type: "single"; key: string; entry: Exclude<FeedEntry, StepEntry> } | { type: "steps"; key: string; entries: StepEntry[] };

const isStep = (e: FeedEntry): e is StepEntry => e.type === "command" || e.type === "files" || e.type === "tool";

/** Commands, file changes and tool calls that follow one another are shown as ONE block of steps; everything else stays on its own. */
export function groupSteps(feed: readonly FeedEntry[]): FeedBlock[] {
  const blocks: FeedBlock[] = [];
  for (const entry of feed) {
    const last = blocks[blocks.length - 1];
    if (isStep(entry)) {
      if (last?.type === "steps") last.entries.push(entry);
      else blocks.push({ type: "steps", key: `g:${entry.key}`, entries: [entry] });
    } else {
      blocks.push({ type: "single", key: entry.key, entry });
    }
  }
  return blocks;
}

/** Whose side of the chat a block is on: the user's own input, or the agent's. */
export const isUserBlock = (block: FeedBlock): boolean => block.type === "single" && (block.entry.type === "user" || block.entry.type === "action");

/**
 * What the message box holds after a quick-command button: `/goal` and the like go in front of what is typed (replacing a command
 * that is already there), `!` turns the text into a command of your own.
 */
export function applyQuick(text: string, quick: string): string {
  const rest = text.trim().replace(/^(\/\S*|!)\s*/, "");
  return quick === "!" ? `!${rest}` : `${quick} ${rest}`;
}
