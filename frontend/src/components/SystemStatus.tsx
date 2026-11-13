import { useEffect, useState } from "react";

import { getHealth, type Health } from "../lib/api";

type Probe = { label: string; ok: boolean | null };

function Dot({ ok }: { ok: boolean | null }) {
  const color =
    ok === null ? "bg-slate-500" : ok ? "bg-signal-ok" : "bg-signal-bad";
  return (
    <span className="relative inline-flex h-2 w-2">
      {ok && (
        <span className="absolute inline-flex h-full w-full rounded-full bg-signal-ok animate-pulse-ring" />
      )}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${color}`} />
    </span>
  );
}

export default function SystemStatus() {
  const [health, setHealth] = useState<Health | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const h = await getHealth();
        if (!alive) return;
        setHealth(h);
        setReachable(true);
      } catch {
        if (!alive) return;
        setReachable(false);
        setHealth(null);
      }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const probes: Probe[] = [
    { label: "API", ok: reachable },
    { label: "DB", ok: health ? health.services.database : reachable ? false : null },
    { label: "Redis", ok: health ? health.services.redis : reachable ? false : null },
  ];

  return (
    <div className="flex items-center justify-center gap-5 text-[11px] uppercase tracking-wider text-slate-400">
      {probes.map((p) => (
        <span key={p.label} className="inline-flex items-center gap-1.5">
          <Dot ok={p.ok} />
          {p.label}
        </span>
      ))}
    </div>
  );
}
