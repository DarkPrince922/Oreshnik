/** Turns what the agent session stored — plus what it is writing right now — into what the page shows: a chat with a feed of steps.
 *
 * Two sources are merged: the history (`agent_events`, in order, never changes) and the live snapshots of items not yet finished (a message
 * so far, a command's output so far, the turn's diff, the plan). An item is keyed by (turn, id); the finished item replaces its snapshot, and
 * the feed keeps the order in which things first appeared. Anything unfamiliar is kept as a generic step rather than dropped: a newer Codex
 * must not make the feed silently lose what the agent did.
 */

import type { AgentEvent } from "./api";

export interface DiffChange {
  path: string;
  kind: string;
  diff: string;
}
export interface ApprovalQuestion {
  id: string;
  header: string;
  question: string;
  secret: boolean;
  other: boolean;
  options: { label: string; description: string }[];
}
export interface Approval {
  kind: "command" | "file" | "permissions" | "input";
  itemId: string;
  command?: string;
  cwd?: string;
  reason?: string | null;
  questions: ApprovalQuestion[];
}

export type FeedEntry =
  | { type: "user"; key: string; text: string; steer: boolean }
  | { type: "action"; key: string; text: string }
  | { type: "message"; key: string; text: string; live: boolean }
  | { type: "reasoning"; key: string; text: string; live: boolean }
  | { type: "command"; key: string; command: string; output: string; exitCode: number | null; running: boolean; status: string; user: boolean }
  | { type: "files"; key: string; changes: DiffChange[]; running: boolean; status: string }
  | { type: "diff"; key: string; diff: string; live: boolean }
  | { type: "plan"; key: string; steps: { text: string; completed: boolean; inProgress: boolean }[]; explanation: string | null; live: boolean }
  | { type: "todo"; key: string; items: { text: string; completed: boolean }[] }
  | { type: "tool"; key: string; title: string; detail: string; running: boolean }
  | { type: "approval"; key: string; requestId: string; approval: Approval; resolved: boolean }
  | { type: "notice"; key: string; tone: "info" | "warn" | "bad"; text: string }
  | { type: "usage"; key: string; inputTokens: number; outputTokens: number };

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

export const DIFF_KEY = "__diff__";
export const PLAN_KEY = "__plan__";

function fromItem(key: string, item: Obj, running: boolean): FeedEntry | null {
  const status = str(item.status);
  const busy = running || status === "in_progress";
  switch (str(item.type)) {
    case "agent_message":
      return { type: "message", key, text: str(item.text), live: running };
    case "reasoning":
      return { type: "reasoning", key, text: str(item.text), live: running };
    case "command_execution":
      return {
        type: "command",
        key,
        command: str(item.command),
        output: str(item.aggregated_output),
        exitCode: typeof item.exit_code === "number" ? item.exit_code : null,
        running: busy,
        status,
        user: str(item.source) === "userShell",
      };
    case "file_change":
      return {
        type: "files",
        key,
        changes: arr(item.changes).map((c) => ({ path: str(obj(c).path), kind: str(obj(c).kind), diff: str(obj(c).diff) })),
        running: busy,
        status,
      };
    case "todo_list":
      return { type: "todo", key, items: arr(item.items).map((i) => ({ text: str(obj(i).text), completed: obj(i).completed === true })) };
    case "plan_text":
      return { type: "message", key, text: str(item.text), live: running };
    case "mcp_tool_call":
      return { type: "tool", key, title: `${str(item.server)}.${str(item.tool)}`.replace(/^\./, ""), detail: JSON.stringify(item.arguments ?? {}), running: busy };
    case "web_search":
      return { type: "tool", key, title: "поиск в сети", detail: str(item.query), running: busy };
    case "context_compaction":
      return { type: "notice", key, tone: "info", text: busy ? "Сжимаю разговор…" : "Разговор сжат: старые ходы заменены кратким изложением" };
    case "review_mode":
      return { type: "notice", key, tone: "info", text: item.entered === true ? "Код-ревью начато" : "Код-ревью закончено" };
    case "error":
      // Codex reports harmless warnings (e.g. "Model metadata for X not found") as error items: show them quietly.
      return { type: "notice", key, tone: "info", text: str(item.message) };
    default:
      return { type: "tool", key, title: str(item.kind) || str(item.type) || "шаг", detail: "", running: busy };
  }
}

function approvalFrom(p: Obj): Approval {
  const a = obj(p.approval);
  const kind = str(a.kind);
  return {
    kind: (["command", "file", "permissions", "input"].includes(kind) ? kind : "command") as Approval["kind"],
    itemId: str(a.item_id),
    command: str(a.command) || undefined,
    cwd: str(a.cwd) || undefined,
    reason: str(a.reason) || null,
    questions: arr(a.questions).map((q) => {
      const o = obj(q);
      return {
        id: str(o.id),
        header: str(o.header),
        question: str(o.question),
        secret: o.secret === true,
        other: o.other === true,
        options: arr(o.options).map((x) => ({ label: str(obj(x).label), description: str(obj(x).description) })),
      };
    }),
  };
}

function planFrom(key: string, p: Obj, live: boolean): FeedEntry {
  return {
    type: "plan",
    key,
    steps: arr(p.steps).map((s) => ({ text: str(obj(s).text), completed: obj(s).completed === true, inProgress: obj(s).in_progress === true })),
    explanation: str(p.explanation) || null,
    live,
  };
}

const ACTION_TEXT: Record<string, (p: Obj) => string> = {
  compact: () => "Сжать контекст",
  shell: (p) => `$ ${str(p.command)}`,
  review: (p) => {
    const t = obj(p.target);
    const what = str(t.type) === "baseBranch" ? ` относительно ${str(t.branch)}` : str(t.type) === "commit" ? ` коммита ${str(t.sha).slice(0, 8)}` : str(t.type) === "custom" ? ": " + str(t.instructions) : " текущих изменений";
    return `Код-ревью${what}`;
  },
  revert: () => "Откатить последний ход",
};

export function buildFeed(events: AgentEvent[], live: Record<string, Obj> = {}): FeedEntry[] {
  let order: string[] = [];
  const entries = new Map<string, FeedEntry>();
  const put = (key: string, entry: FeedEntry | null) => {
    if (!entry) return;
    if (!entries.has(key)) order.push(key);
    entries.set(key, entry);
  };

  let turn = 0;
  let inputPos = 0; // where the entry that started the current turn sits in the feed (an undo cuts from here)
  const turnPos = new Map<string, number>();
  for (const event of events) {
    const p = event.payload;
    switch (event.kind) {
      case "user": {
        const steer = p.steer === true;
        if (!steer) {
          turn += 1;
          inputPos = order.length;
        }
        put(`u${event.seq}`, { type: "user", key: `u${event.seq}`, text: str(p.text), steer });
        break;
      }
      case "action": {
        turn += 1;
        inputPos = order.length;
        const text = (ACTION_TEXT[str(p.action)] ?? (() => str(p.action)))(p);
        put(`x${event.seq}`, { type: "action", key: `x${event.seq}`, text });
        break;
      }
      case "turn.started": {
        if (str(p.turn_id)) turnPos.set(str(p.turn_id), inputPos);
        break;
      }
      case "item.started":
      case "item.updated":
      case "item.completed": {
        const item = obj(p.item);
        const key = `t${turn}:${str(item.id) || event.seq}`;
        put(key, fromItem(key, item, event.kind !== "item.completed"));
        break;
      }
      case "turn.diff":
        put(`d${turn}`, { type: "diff", key: `d${turn}`, diff: str(p.diff), live: false });
        break;
      case "turn.plan":
        put(`p${turn}`, planFrom(`p${turn}`, p, false));
        break;
      case "approval.requested": {
        const id = str(p.request_id);
        put(`a${id}`, { type: "approval", key: `a${id}`, requestId: id, approval: approvalFrom(p), resolved: false });
        break;
      }
      case "approval.resolved": {
        const entry = entries.get(`a${str(p.request_id)}`);
        if (entry && entry.type === "approval") entries.set(entry.key, { ...entry, resolved: true });
        break;
      }
      case "turn.completed": {
        const usage = obj(p.usage);
        put(`c${event.seq}`, { type: "usage", key: `c${event.seq}`, inputTokens: num(usage.input_tokens), outputTokens: num(usage.output_tokens) });
        break;
      }
      case "thread.reverted": {
        // The conversation went back to before that turn (the files stay as they are): what it produced leaves the feed.
        const cut = turnPos.get(str(p.turn_id));
        if (cut !== undefined) {
          for (const key of order.slice(cut)) entries.delete(key);
          order = order.slice(0, cut);
        }
        put(`r${event.seq}`, { type: "notice", key: `r${event.seq}`, tone: "warn", text: "Последний ход отменён: агент забыл его. Файлы остались как были." });
        break;
      }
      case "turn.error":
        put(`e${event.seq}`, { type: "notice", key: `e${event.seq}`, tone: "bad", text: str(p.message) });
        break;
      case "turn.stopped":
        put(`s${event.seq}`, { type: "notice", key: `s${event.seq}`, tone: "warn", text: str(p.message) || "Остановлено" });
        break;
      // `turn.failed` / `error` repeat what the panel records as `turn.error`; thread starts carry nothing to show.
      default:
        break;
    }
  }

  // What is being written right now goes after the history. The history wins: an item that has finished is in it, and its snapshot may
  // still be on the way out (the socket sends the event first and says the snapshot is gone a moment later).
  for (const [id, item] of Object.entries(live)) {
    if (id === DIFF_KEY) {
      if (!entries.has(`d${turn}`)) put(`d${turn}`, { type: "diff", key: `d${turn}`, diff: str(item.diff), live: true });
    } else if (id === PLAN_KEY) {
      if (!entries.has(`p${turn}`)) put(`p${turn}`, planFrom(`p${turn}`, item, true));
    } else if (!entries.has(`t${turn}:${id}`)) {
      put(`t${turn}:${id}`, fromItem(`t${turn}:${id}`, item, true));
    }
  }
  return order.map((k) => entries.get(k)!);
}

/** A step that is still in progress at the end of the feed. Only meaningful while the session is running. */
export function lastStepRunning(feed: FeedEntry[]): boolean {
  const last = feed[feed.length - 1];
  if (!last) return false;
  if (last.type === "command" || last.type === "files" || last.type === "tool") return last.running;
  if (last.type === "message" || last.type === "reasoning" || last.type === "diff" || last.type === "plan") return last.live;
  return last.type === "approval" && !last.resolved;
}

/** The turn has reported its end (the usage line is the last thing in the feed). */
export function turnFinished(feed: FeedEntry[]): boolean {
  return feed[feed.length - 1]?.type === "usage";
}

/** An agent question nobody has answered yet: the turn is waiting for the user. */
export function waitingForUser(feed: FeedEntry[]): boolean {
  return feed.some((e) => e.type === "approval" && !e.resolved);
}

export type Composer =
  | { kind: "empty" }
  | { kind: "message"; text: string }
  | { kind: "steer"; text: string }
  | { kind: "shell"; command: string }
  | { kind: "action"; action: "compact" | "review" | "revert" | "fork"; argument: string };

const SLASH: Record<string, "compact" | "review" | "revert" | "fork"> = { "/compact": "compact", "/review": "review", "/undo": "revert", "/fork": "fork" };

/** What the text in the box means: a message (or, while the agent works, an instruction added to its turn), `!command` for a command of your
 * own, or a slash command like Codex's own. */
export function readComposer(raw: string, status: "idle" | "running"): Composer {
  const text = raw.trim();
  if (!text) return { kind: "empty" };
  if (status === "running") return { kind: "steer", text };
  if (text.startsWith("!") && text.slice(1).trim()) return { kind: "shell", command: text.slice(1).trim() };
  const [head, ...rest] = text.split(/\s+/);
  const action = SLASH[head.toLowerCase()];
  if (action) return { kind: "action", action, argument: rest.join(" ") };
  return { kind: "message", text };
}

export function canSend(args: { text: string; status: "idle" | "running"; profileAvailable: boolean }): boolean {
  if (args.text.trim().length === 0) return false;
  return args.status === "running" || args.profileAvailable;
}

/** Profiles the agent can use: Codex speaks the OpenAI Responses API, which Claude's native API does not. */
export function usableProfiles<T extends { provider: string }>(profiles: T[]): T[] {
  return profiles.filter((p) => p.provider !== "claude");
}

export function formatTokens(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k` : String(n);
}
