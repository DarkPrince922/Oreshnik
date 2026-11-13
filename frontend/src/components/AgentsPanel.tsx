import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { useLayoutEffect, useMemo, useRef, useState } from "react";

import type { ActivityEvent, AgentInfo } from "../lib/api";
import {
  agentColor,
  agentStatus,
  buildFeed,
  buildTree,
  countSteps,
  offsetLabel,
  type AgentNode,
  type FeedItem,
  type StepItem,
  type TextItem,
} from "../lib/activityModel";
import { EmptyState, ErrorNote, Spinner } from "./ui";

const WINDOW = 300;
type Filter = "all" | "steps" | "talk";

// ── pieces ───────────────────────────────────────────────────────────────

function StatusDot({ status }: { status: string }) {
  const tone = agentStatus(status).tone;
  const color = { ok: "bg-signal-ok", warn: "bg-signal-warn", bad: "bg-signal-bad", muted: "bg-slate-500", run: "bg-accent-soft" }[tone];
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0">
      {tone === "run" && <span className="absolute inline-flex h-full w-full rounded-full bg-accent-soft animate-pulse-ring" />}
      <span className={clsx("relative inline-flex h-2 w-2 rounded-full", color)} />
    </span>
  );
}

function AgentChip({ id, agents }: { id: string; agents: AgentInfo[] }) {
  const name = agents.find((a) => a.id === id)?.name ?? id;
  const color = agentColor(id, agents);
  return (
    <span className="inline-flex max-w-[12rem] shrink-0 items-center gap-1.5 rounded-md border border-white/10 bg-white/5 px-2 py-0.5 text-[11px] font-medium" style={{ color }}>
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: color }} />
      <span className="truncate">{name}</span>
    </span>
  );
}

function TreeRow({ node, agents, counts, selected, onSelect }: { node: AgentNode; agents: AgentInfo[]; counts: Record<string, number>; selected: string | null; onSelect: (id: string | null) => void }) {
  const a = node.agent;
  const st = agentStatus(a.status);
  const active = selected === a.id;
  return (
    <>
      <li>
        <button
          onClick={() => onSelect(active ? null : a.id)}
          style={{ paddingLeft: `${0.75 + node.depth * 1.1}rem` }}
          className={clsx("flex w-full items-center gap-2.5 rounded-lg py-2 pr-3 text-left transition-colors", active ? "bg-accent/15" : "hover:bg-white/5")}
          title={a.error ?? undefined}
        >
          <StatusDot status={a.status} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm" style={{ color: agentColor(a.id, agents) }}>{a.name}</span>
            <span className="block text-[11px] text-slate-500">
              {st.label}
              {counts[a.id] ? ` · команд: ${counts[a.id]}` : ""}
            </span>
          </span>
        </button>
      </li>
      {node.children.map((child) => (
        <TreeRow key={child.agent.id} node={child} agents={agents} counts={counts} selected={selected} onSelect={onSelect} />
      ))}
    </>
  );
}

const TOOL_GLYPH: Record<string, string> = { exec_command: "$", write_stdin: "↵", create_agent: "⎇", agent_finish: "■", send_message_to_agent: "✉", wait_for_agents: "…", create_vulnerability_report: "⚑", finish: "■" };

/** Plain text only — everything here quotes the scanned target and may be hostile. */
function Output({ text, cut }: { text: string; cut: boolean }) {
  const lines = text.split("\n");
  const [open, setOpen] = useState(false);
  const long = lines.length > 6 || text.length > 700;
  const shown = open || !long ? text : lines.slice(0, 6).join("\n").slice(0, 700);
  return (
    <div className="mt-2">
      <pre className={clsx("overflow-auto whitespace-pre-wrap rounded-lg border border-white/5 bg-ink-950 p-3 font-mono text-[11px] leading-relaxed text-slate-400 [overflow-wrap:anywhere]", open ? "max-h-96" : "max-h-40")}>{shown || "(пусто)"}</pre>
      {(long || cut) && (
        <button onClick={() => setOpen((v) => !v)} className="mt-1 text-[11px] text-accent-soft hover:underline">
          {open ? "Свернуть" : "Показать весь вывод"}
        </button>
      )}
      {cut && open && <span className="ml-3 text-[11px] text-slate-600">вывод сокращён панелью</span>}
    </div>
  );
}

function Step({ item, agents, start, running }: { item: StepItem; agents: AgentInfo[]; start: string | undefined; running: boolean }) {
  const pending = item.output === null;
  const isSpawn = item.tool === "create_agent";
  const spawnName = typeof item.args?.name === "string" ? item.args.name : "";
  const spawnTask = typeof item.args?.task === "string" ? item.args.task : "";
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 w-4 shrink-0 text-center font-mono text-sm text-accent-soft">{TOOL_GLYPH[item.tool] ?? "▸"}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <AgentChip id={item.agent} agents={agents} />
            <span className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{item.tool}</span>
            <span className="ml-auto flex items-center gap-2 text-[11px] text-slate-600">
              {pending && running ? <Spinner className="h-3 w-3" /> : pending ? <span title="вывод не получен">—</span> : <span className="text-signal-ok">✓</span>}
              {offsetLabel(item.ts, start)}
            </span>
          </div>
          {isSpawn ? (
            <p className="mt-2 text-sm text-slate-200 [overflow-wrap:anywhere]">
              Запущен агент <span className="font-semibold text-white">{spawnName || "—"}</span>
              {spawnTask && <span className="block whitespace-pre-wrap pt-1 text-xs text-slate-400">Задача: {spawnTask}</span>}
            </p>
          ) : (
            <code className="mt-2 block whitespace-pre-wrap font-mono text-[13px] leading-relaxed text-slate-100 [overflow-wrap:anywhere]">
              {item.summary || "(без аргументов)"}
            </code>
          )}
          {item.output !== null && !(isSpawn && item.output.length < 200) && <Output text={item.output} cut={item.outputCut} />}
          {isSpawn && item.output !== null && item.output.length < 200 && <p className="mt-1.5 text-[11px] text-slate-500">{item.output}</p>}
        </div>
      </div>
    </div>
  );
}

const TEXT_META: Record<TextItem["kind"], { label: string; box: string }> = {
  message: { label: "", box: "border-transparent" },
  task: { label: "Задача агента", box: "border-dashed border-white/15" },
  agent_message: { label: "Сообщение другого агента", box: "border-accent/20 bg-accent/5" },
  system: { label: "Система", box: "border-signal-warn/25 bg-signal-warn/5" },
};

// A plain user turn after an agent's task is what `strix --resume --instruction` injects into the root agent.
const USER_INSTRUCTION = { label: "Инструкция пользователя", box: "border-accent/40 bg-accent/10" };

function Talk({ item, agents, start }: { item: TextItem; agents: AgentInfo[]; start: string | undefined }) {
  const [open, setOpen] = useState(false);
  const meta = item.kind === "message" && item.role === "user" ? USER_INSTRUCTION : TEXT_META[item.kind];
  const long = item.text.length > 600 || item.text.split("\n").length > 8;
  return (
    <div className={clsx("rounded-xl border px-4 py-3", meta.box)}>
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        <AgentChip id={item.agent} agents={agents} />
        {meta.label && <span>{meta.label}</span>}
        <span className="ml-auto text-slate-600">{offsetLabel(item.ts, start)}</span>
      </div>
      <p className={clsx("mt-2 whitespace-pre-wrap text-sm leading-relaxed text-slate-300 [overflow-wrap:anywhere]", long && !open && "line-clamp-6")}>{item.text}</p>
      {(long || item.cut) && (
        <button onClick={() => setOpen((v) => !v)} className="mt-1 text-[11px] text-accent-soft hover:underline">
          {open ? "Свернуть" : "Показать полностью"}
        </button>
      )}
    </div>
  );
}

// ── panel ────────────────────────────────────────────────────────────────

interface Props {
  events: ActivityEvent[];
  agents: AgentInfo[];
  loaded: boolean;
  error: string | null;
  active: boolean;
}

export default function AgentsPanel({ events, agents, loaded, error, active }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [extra, setExtra] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [behind, setBehind] = useState(false);

  const feed = useMemo(() => buildFeed(events), [events]);
  const tree = useMemo(() => buildTree(agents), [agents]);
  const counts = useMemo(() => countSteps(events), [events]);
  const start = events[0]?.ts;

  const visible = useMemo(
    () =>
      feed.filter((f) => (selected ? f.agent === selected : true) && (filter === "all" ? true : filter === "steps" ? f.type === "step" : f.type === "text")),
    [feed, selected, filter]
  );
  const shown = visible.slice(-(WINDOW + extra));
  const hidden = visible.length - shown.length;

  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    if (stick.current) box.scrollTop = box.scrollHeight;
    else setBehind(true);
  });
  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (stick.current) setBehind(false);
  };
  const jump = () => {
    stick.current = true;
    setBehind(false);
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight, behavior: "smooth" });
  };

  if (!loaded) return <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>;
  if (events.length === 0 && agents.length === 0) {
    return (
      <>
        <ErrorNote message={error} />
        <EmptyState
          title={active ? "Агенты ещё не начали" : "Истории действий нет"}
          text={active ? "Strix поднимает песочницу и запускает корневого агента. Первые шаги появятся здесь через несколько секунд." : "Скан не успел записать действия агентов (например, упал на проверке Docker или модели)."}
        />
      </>
    );
  }

  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: "Всё" },
    { id: "steps", label: "Команды" },
    { id: "talk", label: "Сообщения" },
  ];

  return (
    <div className="grid gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <aside className="lg:sticky lg:top-4 lg:self-start">
        <div className="glass rounded-2xl p-3">
          <div className="flex items-center justify-between px-2 pb-2 pt-1">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Агенты · {agents.length}</h3>
            {selected && <button onClick={() => setSelected(null)} className="text-[11px] text-accent-soft hover:underline">Все</button>}
          </div>
          <ul className="max-h-[calc(100vh-26rem)] min-h-[10rem] space-y-0.5 overflow-y-auto">
            {tree.map((n) => <TreeRow key={n.agent.id} node={n} agents={agents} counts={counts} selected={selected} onSelect={setSelected} />)}
          </ul>
        </div>
      </aside>

      <section className="min-w-0">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {filters.map((f) => (
            <button key={f.id} onClick={() => setFilter(f.id)} className={clsx("rounded-full border px-3 py-1 text-xs transition-colors", filter === f.id ? "border-accent/40 bg-accent/15 text-white" : "border-white/10 bg-white/5 text-slate-400 hover:text-slate-200")}>
              {f.label}
            </button>
          ))}
          <span className="ml-auto text-xs text-slate-600">{visible.length} шагов{active ? " · обновляется" : ""}</span>
        </div>
        <ErrorNote message={error} />
        <div className="relative">
          <div ref={boxRef} onScroll={onScroll} className="h-[calc(100vh-29rem)] min-h-[32rem] max-h-[64rem] space-y-2.5 overflow-y-auto rounded-2xl border border-white/10 bg-ink-950/60 p-3">
            {hidden > 0 && (
              <button onClick={() => setExtra((n) => n + WINDOW)} className="mx-auto block rounded-full border border-white/10 px-4 py-1.5 text-xs text-accent-soft hover:bg-white/5">
                Показать более ранние ({hidden})
              </button>
            )}
            <AnimatePresence initial={false}>
              {shown.map((item: FeedItem) => (
                <motion.div key={item.key} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}>
                  {item.type === "step" ? <Step item={item} agents={agents} start={start} running={active} /> : <Talk item={item} agents={agents} start={start} />}
                </motion.div>
              ))}
            </AnimatePresence>
            {visible.length === 0 && <p className="py-10 text-center text-sm text-slate-600">Нет шагов под этот фильтр.</p>}
          </div>
          {behind && (
            <button onClick={jump} className="absolute bottom-4 right-5 rounded-full border border-accent/40 bg-ink-900/90 px-3 py-1.5 text-xs text-accent-soft shadow-lg backdrop-blur transition hover:bg-accent/20">
              ↓ к новым шагам
            </button>
          )}
        </div>
        <p className="mt-2 text-[11px] text-slate-600">Команды и их вывод цитируют проверяемую цель и показываются как текст. Длинный вывод сокращён.</p>
      </section>
    </div>
  );
}
