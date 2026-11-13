/** Turns the stored event list of an agent session into what the page shows: a chat with a feed of steps.
 *
 * Codex (`exec --json`, 0.169) emits `item.started` / `item.updated` / `item.completed` for the same item id as a step
 * progresses, and ids restart at every turn ("item_0", "item_1", …) — so an item is keyed by (turn, id), a later event
 * replaces an earlier one, and the feed keeps the order in which items first appeared. Anything unfamiliar is kept as a
 * generic step rather than dropped: a newer Codex must not make the feed silently lose what the agent did.
 */

import type { AgentEvent } from "./api";

export type FeedEntry =
  | { type: "user"; key: string; text: string }
  | { type: "message"; key: string; text: string }
  | { type: "reasoning"; key: string; text: string }
  | { type: "command"; key: string; command: string; output: string; exitCode: number | null; running: boolean }
  | { type: "files"; key: string; changes: { path: string; kind: string }[]; running: boolean }
  | { type: "todo"; key: string; items: { text: string; completed: boolean }[] }
  | { type: "tool"; key: string; title: string; detail: string; running: boolean }
  | { type: "notice"; key: string; tone: "info" | "warn" | "bad"; text: string }
  | { type: "usage"; key: string; inputTokens: number; outputTokens: number };

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

function fromItem(key: string, item: Obj, started: boolean): FeedEntry | null {
  const status = str(item.status);
  const running = started || status === "in_progress";
  switch (str(item.type)) {
    case "agent_message":
      return { type: "message", key, text: str(item.text) };
    case "reasoning":
      return { type: "reasoning", key, text: str(item.text) };
    case "command_execution":
      return {
        type: "command",
        key,
        command: str(item.command),
        output: str(item.aggregated_output),
        exitCode: typeof item.exit_code === "number" ? item.exit_code : null,
        running,
      };
    case "file_change": {
      const raw = Array.isArray(item.changes) ? item.changes : [];
      return { type: "files", key, changes: raw.map((c) => ({ path: str(obj(c).path), kind: str(obj(c).kind) })), running };
    }
    case "todo_list": {
      const raw = Array.isArray(item.items) ? item.items : [];
      return { type: "todo", key, items: raw.map((i) => ({ text: str(obj(i).text), completed: obj(i).completed === true })) };
    }
    case "mcp_tool_call":
      return { type: "tool", key, title: `${str(item.server)}.${str(item.tool)}`.replace(/^\./, ""), detail: JSON.stringify(item.arguments ?? {}), running };
    case "web_search":
      return { type: "tool", key, title: "поиск в сети", detail: str(item.query), running };
    case "error":
      // Codex reports harmless warnings (e.g. "Model metadata for X not found") as error items: show them quietly.
      return { type: "notice", key, tone: "info", text: str(item.message) };
    default:
      return { type: "tool", key, title: str(item.type) || "шаг", detail: "", running };
  }
}

export function buildFeed(events: AgentEvent[]): FeedEntry[] {
  const order: string[] = [];
  const entries = new Map<string, FeedEntry>();
  const put = (key: string, entry: FeedEntry | null) => {
    if (!entry) return;
    if (!entries.has(key)) order.push(key);
    entries.set(key, entry);
  };

  let turn = 0;
  for (const event of events) {
    const p = event.payload;
    switch (event.kind) {
      case "user":
        turn += 1;
        put(`u${event.seq}`, { type: "user", key: `u${event.seq}`, text: str(p.text) });
        break;
      case "item.started":
      case "item.updated":
      case "item.completed": {
        const item = obj(p.item);
        const key = `t${turn}:${str(item.id) || event.seq}`;
        put(key, fromItem(key, item, event.kind !== "item.completed"));
        break;
      }
      case "turn.completed": {
        const usage = obj(p.usage);
        put(`c${event.seq}`, { type: "usage", key: `c${event.seq}`, inputTokens: num(usage.input_tokens), outputTokens: num(usage.output_tokens) });
        break;
      }
      case "turn.error":
        put(`e${event.seq}`, { type: "notice", key: `e${event.seq}`, tone: "bad", text: str(p.message) });
        break;
      case "turn.stopped":
        put(`s${event.seq}`, { type: "notice", key: `s${event.seq}`, tone: "warn", text: str(p.message) || "Остановлено" });
        break;
      // `turn.failed` and `error` repeat what the panel records as `turn.error`; thread/turn starts carry nothing to show.
      default:
        break;
    }
  }
  return order.map((k) => entries.get(k)!);
}

/** A step that is still in progress at the end of the feed. Only meaningful while the session is running. */
export function lastStepRunning(feed: FeedEntry[]): boolean {
  const last = feed[feed.length - 1];
  return !!last && (last.type === "command" || last.type === "files" || last.type === "tool") && last.running;
}

export function canSend(args: { text: string; status: "idle" | "running"; profileAvailable: boolean }): boolean {
  return args.text.trim().length > 0 && args.status === "idle" && args.profileAvailable;
}

/** Profiles the agent can use: Codex speaks the OpenAI Responses API, which Claude's native API does not. */
export function usableProfiles<T extends { provider: string }>(profiles: T[]): T[] {
  return profiles.filter((p) => p.provider !== "claude");
}

export function formatTokens(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k` : String(n);
}
