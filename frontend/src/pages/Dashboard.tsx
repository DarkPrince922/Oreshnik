import { motion } from "framer-motion";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Card, PageHeader, StatusDot, rise, stagger } from "../components/ui";
import { getHealth, listProfiles, listRunners, type Health, type LLMProfile, type Runner } from "../lib/api";
import { useAuth } from "../lib/auth";

function Stat({ label, value, sub, to }: { label: string; value: string; sub?: string; to?: string }) {
  const body = (
    <Card className="h-full p-5 transition-colors hover:border-accent/30">
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-3 text-3xl font-semibold tracking-tight text-white">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </Card>
  );
  return (
    <motion.div variants={rise}>
      {to ? <Link to={to}>{body}</Link> : body}
    </motion.div>
  );
}

export default function Dashboard() {
  const { user, hasRole } = useAuth();
  const canProfiles = hasRole("admin", "operator");

  const [health, setHealth] = useState<Health | null | undefined>(undefined);
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [runners, setRunners] = useState<Runner[] | null>(null);

  useEffect(() => {
    let alive = true;
    getHealth().then((h) => alive && setHealth(h)).catch(() => alive && setHealth(null));
    listRunners().then((r) => alive && setRunners(r)).catch(() => undefined);
    if (canProfiles) listProfiles().then((p) => alive && setProfiles(p)).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [canProfiles]);

  const online = runners?.filter((r) => r.status === "online").length ?? 0;
  const services = [
    { label: "API", ok: health === undefined ? null : health !== null },
    { label: "База данных", ok: health ? health.services.database : health === null ? false : null },
    { label: "Redis", ok: health ? health.services.redis : health === null ? false : null },
  ];

  return (
    <>
      <PageHeader title={`Добро пожаловать${user ? `, ${user.email.split("@")[0]}` : ""}`} subtitle="Сводка по панели Oreshnik" />

      <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Stat label="Раннеры онлайн" value={runners ? `${online} / ${runners.length}` : "—"} sub="готовы принимать сканы" to="/runners" />
        {canProfiles && (
          <Stat label="LLM-профили" value={profiles ? String(profiles.length) : "—"} sub="ваши ключи и модели" to="/llm" />
        )}
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
      </motion.div>

      <motion.div variants={rise} initial="hidden" animate="show" transition={{ delay: 0.3 }}>
        <Card className="mt-6 p-6">
          <div className="flex items-center gap-4">
            <motion.div
              animate={{ rotate: 360 }}
              transition={{ duration: 14, repeat: Infinity, ease: "linear" }}
              className="h-9 w-9 shrink-0 rounded-full border border-dashed border-accent/50"
            />
            <div>
              <h3 className="text-sm font-semibold text-white">Сканы — следующий этап</h3>
              <p className="mt-0.5 text-xs leading-relaxed text-slate-500">
                Запуск Strix, live-лог и отчёты появятся в фазах 2–3. Пока можно настроить профили LLM и раннеры.
              </p>
            </div>
          </div>
        </Card>
      </motion.div>
    </>
  );
}
