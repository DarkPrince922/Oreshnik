import clsx from "clsx";
import { motion } from "framer-motion";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { SeverityCounts, StatusBadge, isActive } from "../components/scan";
import { Card, relativeTime, rise, stagger } from "../components/ui";
import { getCosts, getHealth, listProfiles, listRunners, listScans, type CostSummary, type Health, type LLMProfile, type Runner, type ScanList } from "../lib/api";
import { useAuth } from "../lib/auth";
import { WARN_AT, budgetState } from "../lib/cost";
import { formatCost, targetsLabel } from "../lib/format";

function Stat({ label, value, sub, to, live = false }: { label: string; value: string; sub?: string; to?: string; live?: boolean }) {
  const body = (
    <div
      className={clsx(
        "h-full rounded-2xl border px-5 py-4 transition-all duration-200 hover:-translate-y-0.5",
        live ? "border-accent/40 bg-gradient-to-br from-accent/[0.18] to-accent/[0.04] shadow-[0_18px_50px_-28px_rgba(124,92,255,0.9)]" : "border-white/[0.08] bg-gradient-to-b from-white/[0.05] to-transparent hover:border-white/15"
      )}
    >
      <div className={clsx("flex items-center gap-2 text-xs", live ? "text-accent-soft" : "text-slate-400")}>
        {live && (
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-pulse-ring rounded-full bg-accent-soft" />
            <span className="relative h-2 w-2 rounded-full bg-accent-soft" />
          </span>
        )}
        {label}
      </div>
      <div className="mt-2 text-[32px] font-semibold leading-tight tracking-tight text-white">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-400">{sub}</div>}
    </div>
  );
  return <motion.div variants={rise}>{to ? <Link to={to}>{body}</Link> : body}</motion.div>;
}

export default function Dashboard() {
  const { user, hasRole } = useAuth();
  const canProfiles = hasRole("admin", "operator");

  const [health, setHealth] = useState<Health | null | undefined>(undefined);
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [runners, setRunners] = useState<Runner[] | null>(null);
  const [scans, setScans] = useState<ScanList | null>(null);
  const [costs, setCosts] = useState<CostSummary | null>(null);

  useEffect(() => {
    let alive = true;
    getHealth().then((h) => alive && setHealth(h)).catch(() => alive && setHealth(null));
    listRunners().then((r) => alive && setRunners(r)).catch(() => undefined);
    listScans({ limit: 20 }).then((s) => alive && setScans(s)).catch(() => undefined);
    if (canProfiles) getCosts(30).then((c) => alive && setCosts(c)).catch(() => undefined);
    if (canProfiles) listProfiles().then((p) => alive && setProfiles(p)).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [canProfiles]);

  const online = runners?.filter((r) => r.status === "online").length ?? 0;
  const activeScans = scans?.items.filter((s) => isActive(s.status)).length ?? 0;
  const nearLimit = scans?.items.filter((s) => isActive(s.status) && (budgetState(s.cost, s.max_budget)?.fraction ?? 0) >= WARN_AT) ?? [];
  const recentFindings = scans?.items.reduce((n, s) => n + s.findings_total, 0) ?? 0;
  const services = [
    { label: "API", ok: health === undefined ? null : health !== null },
    { label: "База данных", ok: health ? health.services.database : health === null ? false : null },
    { label: "Redis", ok: health ? health.services.redis : health === null ? false : null },
  ];

  return (
    <>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-1 text-xs text-slate-400">{new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</p>
          <h1 className="bg-gradient-to-r from-white to-accent-soft bg-clip-text text-[28px] font-semibold leading-tight tracking-tight text-transparent">
            Добро пожаловать{user ? `, ${user.email.split("@")[0]}` : ""}
          </h1>
        </div>
        <ul className="flex flex-wrap gap-2 text-xs text-slate-300" aria-label="Состояние служб">
          {services.map((s) => (
            <li key={s.label} className="inline-flex items-center gap-1.5 rounded-full border border-white/10 px-2.5 py-1" title={s.ok === null ? "проверяется" : s.ok ? "работает" : "не отвечает"}>
              <span className={clsx("h-1.5 w-1.5 rounded-full", s.ok === null ? "bg-slate-500" : s.ok ? "bg-signal-ok" : "bg-signal-bad")} />
              {s.label}
            </li>
          ))}
        </ul>
      </div>

      <motion.div variants={stagger} initial="hidden" animate="show" className={clsx("grid gap-4 sm:grid-cols-2 lg:grid-cols-4", canProfiles && "xl:grid-cols-5")}>
        <Stat label="Активные сканы" value={scans ? String(activeScans) : "—"} sub="в очереди и выполняются" to="/scans" live={activeScans > 0} />
        <Stat label="Находки" value={scans ? String(recentFindings) : "—"} sub={scans ? `в последних ${scans.items.length} сканах` : undefined} to="/scans" />
        <Stat label="Раннеры онлайн" value={runners ? `${online} / ${runners.length}` : "—"} sub="готовы принимать сканы" to="/runners" />
        {canProfiles && (
          <Stat
            label="Расходы за месяц"
            value={costs ? formatCost(costs.periods.month) : "—"}
            sub={costs ? (costs.running.count > 0 ? `сейчас в работе: ${formatCost(costs.running.cost)}` : "по сканам этого месяца") : undefined}
            to="/costs"
          />
        )}
        {canProfiles && <Stat label="LLM-профили" value={profiles ? String(profiles.length) : "—"} sub="ваши ключи и модели" to="/llm" />}
      </motion.div>

      {nearLimit.length > 0 && (
        <div role="status" data-testid="budget-warning" className="mt-6 rounded-xl border border-signal-warn/25 bg-signal-warn/10 px-4 py-3 text-sm text-signal-warn">
          Близко к лимиту бюджета:{" "}
          {nearLimit.map((s, i) => (
            <span key={s.id}>
              {i > 0 && ", "}
              <Link to={`/scans/${s.id}`} className="underline">#{s.id}</Link>
            </span>
          ))}
          . При достижении лимита Strix остановит скан.
        </div>
      )}

      <motion.div variants={rise} initial="hidden" animate="show" transition={{ delay: 0.25 }} className={clsx("mt-6 grid items-start gap-4", canProfiles && "lg:grid-cols-[minmax(0,1fr)_19rem]")}>
        <Card className="min-w-0 overflow-hidden">
          <div className="flex items-center justify-between border-b border-white/5 px-5 py-4">
            <h2 className="text-sm font-semibold text-white">Последние сканы</h2>
            <Link to="/scans" className="text-xs text-slate-500 transition hover:text-white">Все →</Link>
          </div>
          {scans === null ? (
            <p className="px-5 py-8 text-center text-sm text-slate-600">Загрузка…</p>
          ) : scans.items.length === 0 ? (
            <p className="px-5 py-8 text-center text-sm text-slate-500">Сканов пока нет.</p>
          ) : (
            <ul className="divide-y divide-white/5">
              {scans.items.slice(0, 5).map((s) => {
                const { first, more } = targetsLabel(s.targets);
                return (
                  <li key={s.id}>
                    <Link to={`/scans/${s.id}`} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5 transition-colors hover:bg-white/[0.04]">
                      <span className="font-mono text-xs text-slate-600">#{s.id}</span>
                      <span className="min-w-0 flex-1 basis-40 truncate text-sm text-slate-200">
                        {first}{more > 0 && <span className="ml-2 text-xs text-slate-500">+{more}</span>}
                      </span>
                      <SeverityCounts counts={s.severity_counts} />
                      <StatusBadge status={s.status} />
                      <span className="w-20 text-right text-xs text-slate-600">{relativeTime(s.started_at ?? s.created_at)}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
        {canProfiles && (
          <Card className="space-y-2.5 p-5">
            <h2 className="pb-1 text-sm font-semibold text-white">Быстрые действия</h2>
            {[
              { to: "/scans/new", label: "Запустить скан" },
              { to: "/agent", label: "Открыть агента" },
              { to: "/schedules", label: "Настроить расписание" },
            ].map((a) => (
              <Link key={a.to} to={a.to} className="group/act flex h-11 items-center justify-between rounded-xl border border-white/10 bg-ink-800/70 px-3.5 text-sm text-white transition-colors hover:border-accent/40 hover:bg-accent/10">
                {a.label}
                <span aria-hidden="true" className="text-slate-500 transition-transform group-hover/act:translate-x-0.5 group-hover/act:text-accent-soft">→</span>
              </Link>
            ))}
          </Card>
        )}
      </motion.div>
    </>
  );
}
