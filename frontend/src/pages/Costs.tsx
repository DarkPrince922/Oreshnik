import clsx from "clsx";
import { motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import BudgetBar from "../components/BudgetBar";
import { StatusBadge } from "../components/scan";
import { Card, EmptyState, ErrorNote, PageHeader, Spinner, rise, stagger } from "../components/ui";
import { errorMessage, getCosts, listScans, type CostSummary, type Scan } from "../lib/api";
import { useAuth } from "../lib/auth";
import { WARN_AT, barLayout, budgetState, niceMax, shortDate } from "../lib/cost";
import { formatCost, formatTokens, targetsLabel } from "../lib/format";

const WINDOWS = [7, 30, 90];
const W = 720;
const H = 180;

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <motion.div variants={rise}>
      <Card className="h-full p-5">
        <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
        <div className="mt-3 text-3xl font-semibold tracking-tight text-white">{value}</div>
        {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
      </Card>
    </motion.div>
  );
}

function DayChart({ days }: { days: CostSummary["per_day"] }) {
  const max = useMemo(() => niceMax(Math.max(0, ...days.map((d) => d.cost))), [days]);
  const bars = useMemo(() => barLayout(days.map((d) => d.cost), W, H, max), [days, max]);
  const labelEvery = Math.max(1, Math.ceil(days.length / 10));
  const total = days.reduce((n, d) => n + d.cost, 0);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H + 28}`} className="w-full" role="img" aria-label={`Расходы по дням, всего ${formatCost(total)}`} data-testid="cost-chart">
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1="0" x2={W} y1={H - f * H} y2={H - f * H} stroke="rgba(255,255,255,0.06)" />
            <text x="0" y={H - f * H - 4} fontSize="10" fill="rgba(148,163,184,0.7)">{formatCost(max * f)}</text>
          </g>
        ))}
        {bars.map((b, i) => (
          <g key={days[i].date}>
            <title>{`${days[i].date}: ${formatCost(days[i].cost)} · сканов: ${days[i].scans}`}</title>
            <motion.rect
              x={b.x}
              width={b.width}
              rx="2"
              className="fill-accent"
              initial={{ y: H, height: 0 }}
              animate={{ y: b.y, height: b.height }}
              transition={{ duration: 0.7, delay: Math.min(i * 0.012, 0.5), ease: [0.22, 1, 0.36, 1] }}
              data-testid="cost-bar"
              data-cost={days[i].cost}
            />
            {i % labelEvery === 0 && (
              <text x={b.x + b.width / 2} y={H + 16} fontSize="10" textAnchor="middle" fill="rgba(148,163,184,0.7)">{shortDate(days[i].date)}</text>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}

export default function Costs() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [days, setDays] = useState(30);
  const [data, setData] = useState<CostSummary | null>(null);
  const [running, setRunning] = useState<Scan[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    getCosts(days).then((d) => alive && (setData(d), setError(null))).catch((e) => alive && setError(errorMessage(e)));
    return () => {
      alive = false;
    };
  }, [days]);

  // Live: scans that are running now and their budgets; the money total of running scans refreshes too.
  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (document.hidden) return;
      listScans({ status: "running", limit: 50 }).then((r) => alive && setRunning(r.items)).catch(() => undefined);
      getCosts(days).then((d) => alive && setData(d)).catch(() => undefined);
    };
    listScans({ status: "running", limit: 50 }).then((r) => alive && setRunning(r.items)).catch(() => undefined);
    const id = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [days]);

  const watched = running.filter((s) => s.max_budget !== null);
  const atRisk = watched.filter((s) => (budgetState(s.cost, s.max_budget)?.fraction ?? 0) >= WARN_AT);

  return (
    <>
      <PageHeader
        title="Расходы"
        subtitle={isAdmin ? "Токены LLM по всем пользователям, дням и сканам." : "Ваши расходы на LLM по дням и сканам."}
        action={
          <div className="flex gap-1 rounded-full border border-white/10 bg-white/5 p-1" role="group" aria-label="Период">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setDays(w)}
                aria-pressed={days === w}
                className={clsx("rounded-full px-3.5 py-1 text-xs font-medium transition-colors", days === w ? "bg-accent/25 text-white" : "text-slate-400 hover:text-slate-200")}
              >
                {w} дн
              </button>
            ))}
          </div>
        }
      />

      <ErrorNote message={error} />
      {data === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {data && (
        <>
          <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Сегодня" value={formatCost(data.periods.today)} sub="по UTC" />
            <Stat label="7 дней" value={formatCost(data.periods.week)} sub="включая сегодня" />
            <Stat label="Этот месяц" value={formatCost(data.periods.month)} sub={`токенов за период: ${formatTokens(data.tokens)}`} />
            <Stat
              label="Сейчас в работе"
              value={formatCost(data.running.cost)}
              sub={data.running.count > 0 ? `сканов выполняется: ${data.running.count}` : "ничего не выполняется"}
            />
          </motion.div>

          {watched.length > 0 && (
            <Card className="mt-6 p-5" >
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-white">Бюджеты работающих сканов</h2>
                {atRisk.length > 0 && <span className="text-xs text-signal-warn" role="status">Близко к лимиту: {atRisk.length}</span>}
              </div>
              <ul className="space-y-4" data-testid="running-budgets">
                {watched.map((s) => (
                  <li key={s.id} className="grid items-center gap-x-6 gap-y-2 md:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
                    <Link to={`/scans/${s.id}`} className="min-w-0 truncate text-sm text-slate-200 hover:text-white">
                      <span className="mr-2 font-mono text-xs text-slate-600">#{s.id}</span>{targetsLabel(s.targets).first}
                    </Link>
                    <BudgetBar cost={s.cost} limit={s.max_budget} active compact />
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card className="mt-6 p-5">
            <div className="mb-3 flex items-baseline justify-between">
              <h2 className="text-sm font-semibold text-white">По дням</h2>
              <span className="text-xs text-slate-500">за {data.days} дн · {formatCost(data.periods.window)}</span>
            </div>
            <DayChart days={data.per_day} />
          </Card>

          <div className={clsx("mt-6 grid gap-6", isAdmin ? "lg:grid-cols-2" : "")}>
            {isAdmin && (
              <Card className="overflow-hidden">
                <h2 className="border-b border-white/5 px-5 py-4 text-sm font-semibold text-white">По пользователям</h2>
                {data.per_user.length === 0 ? (
                  <p className="px-5 py-8 text-center text-sm text-slate-500">За этот период расходов нет.</p>
                ) : (
                  <table className="w-full text-sm" data-testid="per-user">
                    <tbody className="divide-y divide-white/5">
                      {data.per_user.map((u) => (
                        <tr key={u.user_id}>
                          <td className="truncate px-5 py-3 text-slate-200">{u.email}</td>
                          <td className="px-3 py-3 text-right text-xs text-slate-500">сканов: {u.scans}</td>
                          <td className="px-5 py-3 text-right font-mono text-slate-200">{formatCost(u.cost)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
            )}

            <Card className="overflow-hidden">
              <h2 className="border-b border-white/5 px-5 py-4 text-sm font-semibold text-white">Самые дорогие сканы</h2>
              {data.top_scans.length === 0 ? (
                <EmptyState title="Пока пусто" text="Здесь появятся сканы, которые что-то стоили." />
              ) : (
                <ul className="divide-y divide-white/5" data-testid="top-scans">
                  {data.top_scans.map((s) => (
                    <li key={s.id}>
                      <Link to={`/scans/${s.id}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 transition-colors hover:bg-white/[0.04]">
                        <span className="font-mono text-xs text-slate-600">#{s.id}</span>
                        <span className="min-w-0 flex-1 basis-40 truncate text-sm text-slate-200">{s.target}</span>
                        {isAdmin && <span className="hidden truncate text-xs text-slate-500 xl:inline">{s.owner_email}</span>}
                        <StatusBadge status={s.status as Scan["status"]} />
                        <span className="w-16 text-right font-mono text-sm text-slate-200">{formatCost(s.cost)}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <p className="mt-6 text-[11px] leading-relaxed text-slate-600">
            Стоимость берётся из данных самого Strix (run.json) и считается по дате запуска скана. У продолжения учитывается только его собственная часть — расходы родителя не удваиваются.
          </p>
        </>
      )}
    </>
  );
}
