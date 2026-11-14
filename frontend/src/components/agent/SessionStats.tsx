import clsx from "clsx";
import type { ReactNode } from "react";

import { contextView, formatTokens, goalResumable, goalStatusLabel } from "../../lib/agentModel";
import { agoRu } from "../../lib/agentSessions";
import type { AgentSession } from "../../lib/api";
import { CoinsIcon, GaugeIcon, TargetIcon, TurnsIcon } from "./icons";

/** One tile: a label (with an optional action on the right), the value, and a line under it. */
function Tile({ label, icon, action, accent = false, title, children }: { label: string; icon: ReactNode; action?: ReactNode; accent?: boolean; title?: string; children: ReactNode }) {
  return (
    <div
      title={title}
      className={clsx(
        "flex min-w-0 flex-1 basis-40 flex-col gap-1 rounded-xl border px-3 py-2 transition-colors",
        accent ? "border-accent/40 bg-gradient-to-br from-accent/[0.16] to-accent/[0.04] shadow-[0_0_24px_-10px_rgba(124,92,255,0.7)]" : "border-white/[0.08] bg-gradient-to-b from-white/[0.05] to-transparent hover:border-white/15"
      )}
    >
      <span className="flex items-center justify-between gap-2">
        <span className={clsx("flex min-w-0 items-center gap-1.5 text-[11px]", accent ? "text-accent-soft" : "text-slate-400")}>
          <span className={accent ? "text-accent-soft" : "text-slate-500"}>{icon}</span>
          <span className="truncate">{label}</span>
        </span>
        {action}
      </span>
      {children}
    </div>
  );
}

function Bar({ percent, tone = "bg-accent-soft", children }: { percent: number | null; tone?: string; children: ReactNode }) {
  return (
    <span className="flex items-center gap-2 text-[11px] text-slate-400">
      <span className="h-1 min-w-[2rem] flex-1 overflow-hidden rounded-full bg-white/10">
        <span className={clsx("block h-full rounded-full transition-[width] duration-700", tone)} style={{ width: `${percent === null ? 0 : Math.max(2, percent)}%` }} />
      </span>
      <span className="shrink-0">{children}</span>
    </span>
  );
}

const linkClass = "text-[11px] text-accent-soft underline decoration-dotted underline-offset-2 hover:text-white disabled:cursor-not-allowed disabled:opacity-50";

/** The row of tiles above the chat: how full the context is, what the session has spent, its turns and its goal. */
export default function SessionStats({
  session,
  status,
  waiting,
  onCompact,
  onGoalSet,
  onGoalResume,
  onGoalClear,
}: {
  session: AgentSession;
  status: "idle" | "running";
  waiting: boolean;
  onCompact: () => void;
  onGoalSet: () => void;
  onGoalResume: () => void;
  onGoalClear: () => void;
}) {
  const running = status === "running";
  const context = contextView(session.context_used, session.context_window);
  const goal = session.goal;
  const goalPercent = goal?.token_budget ? Math.min(100, Math.round((goal.tokens_used / goal.token_budget) * 100)) : null;
  const toneText = context.tone === "bad" ? "text-signal-bad" : context.tone === "warn" ? "text-signal-warn" : "text-white";
  const toneBar = context.tone === "bad" ? "bg-signal-bad" : context.tone === "warn" ? "bg-signal-warn" : "bg-gradient-to-r from-accent-glow to-accent-soft";

  return (
    <div className="flex flex-wrap gap-2.5 border-b border-white/5 px-5 py-3">
      <Tile
        label="Контекст"
        icon={<GaugeIcon className="h-3 w-3" />}
        title={session.context_window_known ? "Сколько места в контексте модели занято после последнего хода" : "Codex не знает эту модель: размер окна взят по умолчанию и может не совпадать с настоящим"}
        action={
          context.suggestCompact && !running ? (
            <button type="button" className={linkClass} onClick={onCompact}>
              сжать
            </button>
          ) : undefined
        }
      >
        <span className={clsx("text-lg font-semibold leading-tight", toneText)}>
          {context.percent === null ? "—" : `${session.context_window_known ? "" : "≈"}${context.percent}%`}
        </span>
        {context.percent === null ? (
          <span className="text-[11px] text-slate-400">появится после первого хода</span>
        ) : (
          <Bar percent={context.percent} tone={toneBar}>
            {formatTokens(session.context_used ?? 0)} из {formatTokens(session.context_window ?? 0)}
          </Bar>
        )}
      </Tile>

      <Tile label="Токены за сессию" icon={<CoinsIcon className="h-3 w-3" />}>
        <span className="text-lg font-semibold leading-tight text-white">
          {formatTokens(session.tokens_in)} <span className="text-xs font-normal text-slate-400">на входе</span>
        </span>
        <span className="text-[11px] text-slate-400">{formatTokens(session.tokens_out)} на выходе</span>
      </Tile>

      <Tile label="Ходов" icon={<TurnsIcon className="h-3 w-3" />}>
        <span className="text-lg font-semibold leading-tight text-white">{session.turn_count}</span>
        <span className={clsx("truncate text-[11px]", running ? (waiting ? "text-signal-warn" : "text-accent-soft") : "text-slate-400")}>
          {running ? (waiting ? "ждёт вашего ответа" : "идёт ход…") : `изменена ${agoRu(session.updated_at, Date.now())}`}
        </span>
      </Tile>

      {goal ? (
        <Tile
          label={`Цель · ${goalStatusLabel(goal.status)}`}
          icon={<TargetIcon className="h-3 w-3" />}
          accent
          action={
            <span className="flex shrink-0 gap-2">
              {goalResumable(goal, status) && (
                <button type="button" className={linkClass} onClick={onGoalResume}>
                  продолжить
                </button>
              )}
              {!running && (
                <button type="button" className={linkClass} onClick={onGoalClear}>
                  снять
                </button>
              )}
            </span>
          }
        >
          <span className="truncate text-[13px] font-medium leading-6 text-white" title={goal.objective}>
            {goal.objective}
          </span>
          <Bar percent={goalPercent}>
            <span title="Потрачено токенов на цель и её бюджет">
              {formatTokens(goal.tokens_used)}
              {goal.token_budget ? ` из ${formatTokens(goal.token_budget)}` : " токенов"}
            </span>
          </Bar>
        </Tile>
      ) : (
        <Tile
          label="Цель"
          icon={<TargetIcon className="h-3 w-3" />}
          action={
            <button type="button" className={linkClass} disabled={running} onClick={onGoalSet}>
              поставить
            </button>
          }
        >
          <span className="text-[13px] leading-6 text-slate-300">не задана</span>
          <span className="truncate text-[11px] text-slate-400">с целью агент работает сам</span>
        </Tile>
      )}
    </div>
  );
}
