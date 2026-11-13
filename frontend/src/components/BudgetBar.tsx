import clsx from "clsx";
import { motion } from "framer-motion";

import { budgetMessage, budgetState, type BudgetTone } from "../lib/cost";
import { formatCost } from "../lib/format";

const BAR: Record<BudgetTone, string> = { ok: "bg-accent", warn: "bg-signal-warn", bad: "bg-signal-bad" };
const NOTE: Record<BudgetTone, string> = {
  ok: "",
  warn: "border-signal-warn/25 bg-signal-warn/10 text-signal-warn",
  bad: "border-signal-bad/25 bg-signal-bad/10 text-signal-bad",
};

/** How much of the scan's budget is used, live while the scan runs; warns from 80% and at the limit. */
export default function BudgetBar({ cost, limit, active, compact = false }: { cost: number | null; limit: number | null; active: boolean; compact?: boolean }) {
  const state = budgetState(cost, limit);
  if (state === null) return null;
  const message = budgetMessage(state, active);
  return (
    <div data-testid="budget-bar" data-tone={state.tone}>
      <div className="mb-1.5 flex items-baseline justify-between gap-3 text-xs">
        <span className="text-slate-500">Бюджет</span>
        <span className={clsx("font-mono", state.tone === "ok" ? "text-slate-300" : state.tone === "warn" ? "text-signal-warn" : "text-signal-bad")}>
          {formatCost(state.spent)} из ${state.limit} · {state.percent}%
        </span>
      </div>
      <div
        className={clsx("overflow-hidden rounded-full bg-white/10", compact ? "h-1" : "h-2")}
        role="progressbar"
        aria-label="Использование бюджета"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.min(100, state.percent)}
      >
        <motion.div
          className={clsx("h-full rounded-full", BAR[state.tone])}
          initial={false}
          animate={{ width: `${state.barFraction * 100}%` }}
          transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        />
      </div>
      {message && !compact && (
        <p className={clsx("mt-3 rounded-lg border px-3.5 py-2.5 text-xs leading-relaxed", NOTE[state.tone])} role="status">{message}</p>
      )}
    </div>
  );
}
