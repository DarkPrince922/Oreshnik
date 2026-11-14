import clsx from "clsx";
import { motion } from "framer-motion";
import { useState } from "react";

import { contextView } from "../../lib/agentModel";
import { agoRu, filterSessions, sessionCounts, turnsRu, type SessionFilter } from "../../lib/agentSessions";
import type { AgentSession } from "../../lib/api";
import { CloseIcon, SearchIcon } from "./icons";

const FILTERS: { id: SessionFilter; label: string }[] = [
  { id: "all", label: "Все" },
  { id: "running", label: "Работают" },
  { id: "error", label: "С ошибкой" },
];

function SessionCard({ session: s, active, onSelect, onRemove }: { session: AgentSession; active: boolean; onSelect: () => void; onRemove: () => void }) {
  const running = s.status === "running";
  const failed = Boolean(s.last_error);
  const context = contextView(s.context_used, s.context_window);
  return (
    <div
      className={clsx(
        "group relative rounded-2xl border transition-colors",
        active ? "border-accent/50 bg-accent/[0.12]" : failed ? "border-signal-bad/30 bg-ink-900/60 hover:bg-white/[0.04]" : "border-white/[0.08] bg-ink-900/60 hover:bg-white/[0.04]"
      )}
    >
      <button type="button" className="flex w-full flex-col gap-2 px-3.5 py-3 text-left" onClick={onSelect} aria-current={active ? "true" : undefined}>
        <span className="flex items-center gap-2">
          <span
            className={clsx("h-2 w-2 shrink-0", failed && !running ? "rounded-sm bg-signal-bad" : "rounded-full", running ? "bg-signal-ok shadow-[0_0_0_4px_rgba(61,220,151,0.18)]" : !failed && "bg-slate-500")}
          />
          <span className={clsx("min-w-0 flex-1 truncate text-sm font-semibold", active ? "text-white" : "text-slate-100")}>{s.title}</span>
          {/* the same place holds the delete button while the card is hovered */}
          <span className={clsx("shrink-0 text-[11px]", running ? "text-signal-ok" : failed ? "text-signal-bad group-hover:invisible" : "text-slate-400 group-hover:invisible")}>
            {running ? "работает" : failed ? "ошибка" : agoRu(s.updated_at, Date.now())}
          </span>
        </span>
        <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
          {s.llm_model && <span className="max-w-full truncate rounded-md border border-white/10 px-1.5 py-px font-mono text-slate-300">{s.llm_model}</span>}
          <span>{turnsRu(s.turn_count)}</span>
        </span>
        {failed ? (
          <span className="line-clamp-2 text-[11px] text-rose-300 [overflow-wrap:anywhere]">{s.last_error}</span>
        ) : (
          context.percent !== null && (
            <span className="flex items-center gap-2 text-[11px] text-slate-400" title="Сколько места в контексте модели занято">
              <span className="h-1 flex-1 overflow-hidden rounded-full bg-white/10">
                <span
                  className={clsx("block h-full rounded-full", context.tone === "bad" ? "bg-signal-bad" : context.tone === "warn" ? "bg-signal-warn" : "bg-accent-soft")}
                  style={{ width: `${Math.max(2, context.percent)}%` }}
                />
              </span>
              {context.percent}%
            </span>
          )
        )}
      </button>
      <button
        type="button"
        title="Удалить сессию"
        aria-label={`Удалить сессию «${s.title}»`}
        disabled={running}
        className="absolute right-2 top-2 hidden rounded-lg p-1.5 text-slate-400 hover:bg-signal-bad/10 hover:text-signal-bad focus-visible:block disabled:hidden group-hover:block group-hover:disabled:hidden"
        onClick={onRemove}
      >
        <CloseIcon className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

/** The left column of «Агент»: search, state filters and one card per session (state, model, turns, how full the context is). */
export default function SessionList({
  sessions,
  selected,
  onSelect,
  onRemove,
}: {
  sessions: AgentSession[];
  selected: number | null;
  onSelect: (id: number) => void;
  onRemove: (s: AgentSession) => void;
}) {
  const [filter, setFilter] = useState<SessionFilter>("all");
  const [query, setQuery] = useState("");
  const counts = sessionCounts(sessions);
  const shown = filterSessions(sessions, filter, query);

  return (
    <nav aria-label="Сессии агента" className="flex flex-col gap-2.5">
      <label className="flex h-10 items-center gap-2 rounded-xl border border-white/10 bg-ink-900/60 px-3 text-slate-400 focus-within:border-accent/50">
        <SearchIcon />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Найти сессию"
          aria-label="Найти сессию"
          className="min-w-0 flex-1 bg-transparent text-sm text-slate-100 outline-none placeholder:text-slate-500"
        />
      </label>

      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            aria-pressed={filter === f.id}
            onClick={() => setFilter(f.id)}
            className={clsx(
              "rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors",
              filter === f.id ? "border-accent/45 bg-accent/15 text-white" : "border-white/10 text-slate-400 hover:bg-white/5 hover:text-slate-200"
            )}
          >
            {f.label} · {counts[f.id]}
          </button>
        ))}
      </div>

      {shown.map((s) => (
        <motion.div key={s.id} layout>
          <SessionCard session={s} active={s.id === selected} onSelect={() => onSelect(s.id)} onRemove={() => onRemove(s)} />
        </motion.div>
      ))}
      {shown.length === 0 && <p className="px-1 py-4 text-center text-xs text-slate-400">Под этот фильтр сессий нет.</p>}
    </nav>
  );
}
