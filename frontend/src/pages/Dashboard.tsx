import clsx from "clsx";
import { motion } from "framer-motion";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { SeverityCounts, StatusBadge, isActive } from "../components/scan";
import { Button, Card, PageHeader, StatusDot, relativeTime, rise, stagger } from "../components/ui";
import { getCosts, getHealth, listProfiles, listRunners, listScans, type CostSummary, type Health, type LLMProfile, type Runner, type ScanList } from "../lib/api";
import { useAuth } from "../lib/auth";
import { WARN_AT, budgetState } from "../lib/cost";
import { formatCost, targetsLabel } from "../lib/format";

function Stat({ label, value, sub, to }: { label: string; value: string; sub?: string; to?: string }) {
  const body = (
    <Card className="h-full p-5 transition-colors hover:border-accent/30">
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-3 text-3xl font-semibold tracking-tight text-white">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </Card>
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
      <PageHeader
        title={`Добро пожаловать${user ? `, ${user.email.split("@")[0]}` : ""}`}
        subtitle="Сводка по панели Oreshnik"
        action={hasRole("admin", "operator") ? <Link to="/scans/new"><Button variant="primary">+ Новый скан</Button></Link> : undefined}
      />

      <motion.div variants={stagger} initial="hidden" animate="show" className={clsx("grid gap-4 sm:grid-cols-2 lg:grid-cols-4", canProfiles && "xl:grid-cols-5")}>
        <Stat label="Активные сканы" value={scans ? String(activeScans) : "—"} sub="в очереди и выполняются" to="/scans" />
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
        {canProfiles ? (
          <Stat label="LLM-профили" value={profiles ? String(profiles.length) : "—"} sub="ваши ключи и модели" to="/llm" />
        ) : (
          <motion.div variants={rise}>
            <Card className="h-full p-5">
              <div className="text-xs uppercase tracking-wider text-slate-500">Сервисы</div>
              <ul className="mt-4 space-y-2.5">
                {services.map((s) => (
                  <li key={s.label} className="flex items-center justify-between text-sm text-slate-300">
                    {s.label}
                    <StatusDot tone={s.ok === null ? "muted" : s.ok ? "ok" : "bad"} />
                  </li>
                ))}
              </ul>
            </Card>
          </motion.div>
        )}
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

      <motion.div variants={rise} initial="hidden" animate="show" transition={{ delay: 0.25 }} className="mt-6">
        <Card className="overflow-hidden">
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
      </motion.div>
    </>
  );
}
