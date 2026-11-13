/** Pure model behind the "Агенты" tab: turns the raw event stream into a feed and an agent tree. */
import type { ActivityEvent, AgentInfo } from "./api";

export type TextKind = "message" | "task" | "agent_message" | "system";

export interface StepItem {
  type: "step";
  key: string;
  seq: number;
  ts: string;
  agent: string;
  tool: string;
  summary: string;
  args: Record<string, unknown> | null;
  argsCut: boolean;
  output: string | null; // null while the tool is still running
  outputCut: boolean;
}
export interface TextItem {
  type: "text";
  key: string;
  seq: number;
  ts: string;
  agent: string;
  kind: TextKind;
  role: string | null;
  text: string;
  cut: boolean;
}
export type FeedItem = StepItem | TextItem;

/** Pairs every tool call with its output (by call_id) and keeps everything in event order. */
export function buildFeed(events: readonly ActivityEvent[]): FeedItem[] {
  const feed: FeedItem[] = [];
  const open = new Map<string, StepItem>(); // keyed by agent + call id: ids are only unique per agent
  for (const e of events) {
    if (e.kind === "tool_call") {
      const step: StepItem = {
        type: "step",
        key: `s${e.seq}`,
        seq: e.seq,
        ts: e.ts,
        agent: e.agent,
        tool: e.tool ?? "tool",
        summary: e.summary ?? "",
        args: e.args,
        argsCut: e.truncated,
        output: null,
        outputCut: false,
      };
      feed.push(step);
      if (e.call_id) open.set(`${e.agent}\u0000${e.call_id}`, step);
    } else if (e.kind === "tool_output") {
      const step = e.call_id ? open.get(`${e.agent}\u0000${e.call_id}`) : undefined;
      if (step) {
        step.output = e.text ?? "";
        step.outputCut = e.truncated;
      } else {
        // An output whose call we never saw (e.g. resumed run): still show it.
        feed.push({ type: "step", key: `s${e.seq}`, seq: e.seq, ts: e.ts, agent: e.agent, tool: e.tool ?? "tool", summary: "", args: null, argsCut: false, output: e.text ?? "", outputCut: e.truncated });
      }
    } else {
      feed.push({ type: "text", key: `t${e.seq}`, seq: e.seq, ts: e.ts, agent: e.agent, kind: e.kind as TextKind, role: e.role, text: e.text ?? "", cut: e.truncated });
    }
  }
  return feed;
}

export interface AgentNode {
  agent: AgentInfo;
  children: AgentNode[];
  depth: number;
}

/** Parent/child tree. Agents whose parent is unknown become roots; cycles cannot loop forever. */
export function buildTree(agents: readonly AgentInfo[]): AgentNode[] {
  const byId = new Map(agents.map((a) => [a.id, a]));
  const children = new Map<string | null, AgentInfo[]>();
  for (const a of agents) {
    const parent = a.parent_id && byId.has(a.parent_id) && a.parent_id !== a.id ? a.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), a]);
  }
  const seen = new Set<string>();
  const walk = (list: AgentInfo[], depth: number): AgentNode[] =>
    list.flatMap((a) => {
      if (seen.has(a.id)) return [];
      seen.add(a.id);
      return [{ agent: a, depth, children: walk(children.get(a.id) ?? [], depth + 1) }];
    });
  const tree = walk(children.get(null) ?? [], 0);
  // Anything unreachable (a pure cycle) is still shown, flat, rather than vanishing.
  const rest = agents.filter((a) => !seen.has(a.id));
  return [...tree, ...walk(rest, 0)];
}

export function countSteps(events: readonly ActivityEvent[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const e of events) if (e.kind === "tool_call") counts[e.agent] = (counts[e.agent] ?? 0) + 1;
  return counts;
}

const PALETTE = ["#9d86ff", "#3ddc97", "#ffb020", "#5ba4ff", "#ff6fb5", "#2dd4bf", "#fb923c", "#a3e635"];
/** A stable colour per agent: its position in the roster, so it never changes while the scan runs. */
export function agentColor(agentId: string, agents: readonly AgentInfo[]): string {
  const i = agents.findIndex((a) => a.id === agentId);
  return PALETTE[(i < 0 ? agentId.length : i) % PALETTE.length];
}

export const AGENT_STATUS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "muted" | "run" }> = {
  running: { label: "работает", tone: "run" },
  waiting: { label: "ожидает", tone: "muted" },
  budget_paused: { label: "пауза: бюджет", tone: "warn" },
  completed: { label: "завершён", tone: "ok" },
  stopped: { label: "остановлен", tone: "muted" },
  crashed: { label: "сбой", tone: "bad" },
  failed: { label: "ошибка", tone: "bad" },
};
export const agentStatus = (status: string) => AGENT_STATUS[status] ?? { label: status, tone: "muted" as const };

/** "+m:ss" / "+h:mm:ss" since the scan's first event. */
export function offsetLabel(ts: string, startTs: string | undefined): string {
  if (!startTs) return "";
  const secs = Math.max(0, Math.round((new Date(ts).getTime() - new Date(startTs).getTime()) / 1000));
  if (!Number.isFinite(secs)) return "";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0 ? `+${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `+${m}:${String(s).padStart(2, "0")}`;
}
