import clsx from "clsx";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { PageHeader } from "../components/ui";
import { listMcp, listPersonas, listSkills, type McpServer, type Persona, type Skill } from "../lib/api";
import { defaultPersona } from "../lib/personas";
import { effectiveMcp, effectiveSkills, mcpHealth } from "../lib/workshop";
import Mcp from "./Mcp";
import Personas from "./Personas";
import Skills from "./Skills";

export type WorkshopTab = "personas" | "skills" | "mcp";
const TABS: WorkshopTab[] = ["personas", "skills", "mcp"];

const icon = (d: ReactNode) => (
  <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
);
const TAB_META: Record<WorkshopTab, { label: string; icon: ReactNode }> = {
  personas: { label: "Личности", icon: icon(<><circle cx="12" cy="8" r="3.5" /><path d="M5 20c.8-3.6 3.7-5.5 7-5.5s6.2 1.9 7 5.5" /></>) },
  skills: { label: "Навыки", icon: icon(<><path d="M5 4h9l5 5v11H5z" /><path d="M14 4v5h5M8.5 13h7M8.5 16.5h4.5" /></>) },
  mcp: { label: "MCP-серверы", icon: icon(<><path d="M9 3v4M15 3v4M7 7h10v4a5 5 0 0 1-10 0z" /><path d="M12 16v5" /></>) },
};

function Chip({ children, tone = "accent", onClick }: { children: ReactNode; tone?: "accent" | "ok" | "warn" | "muted"; onClick: () => void }) {
  const tones = {
    accent: "border-accent/35 bg-accent/10 text-accent-soft",
    ok: "border-signal-ok/30 bg-signal-ok/10 text-signal-ok",
    warn: "border-signal-warn/35 bg-signal-warn/10 text-signal-warn",
    muted: "border-white/10 bg-white/5 text-slate-400",
  };
  return (
    <button type="button" onClick={onClick} className={clsx("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] font-medium transition-transform hover:-translate-y-px", tones[tone])}>
      {children}
    </button>
  );
}

/** «Мастерская»: everything an agent gets — who it is (personas), what it knows how to do (skills), what it can reach (MCP) — in one
 * place, with a line on top saying what a new session of «Код» gets right now. */
export default function Workshop() {
  const [params, setParams] = useSearchParams();
  const tab: WorkshopTab = TABS.includes(params.get("tab") as WorkshopTab) ? (params.get("tab") as WorkshopTab) : "skills";
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);

  const refresh = useCallback(() => {
    listPersonas().then(setPersonas).catch(() => undefined);
    listSkills().then(setSkills).catch(() => undefined);
    listMcp().then(setServers).catch(() => undefined);
  }, []);
  useEffect(refresh, [refresh]);

  const go = (t: WorkshopTab) => setParams({ tab: t }, { replace: true });
  const persona = defaultPersona(personas, "code");
  const gets = effectiveSkills(skills, "code");
  const mcp = effectiveMcp(servers, "code");
  const broken = mcp.filter((s) => mcpHealth(s) === "bad");
  const counts: Record<WorkshopTab, number> = { personas: personas.length, skills: skills.filter((s) => s.enabled).length, mcp: servers.filter((s) => s.enabled).length };

  return (
    <>
      <PageHeader title="Мастерская" subtitle="Всё, что получает агент: кем быть, какие приёмы знать и какими внешними инструментами пользоваться." />

      <div className="mb-5 flex flex-wrap items-center gap-2 rounded-2xl border border-accent/25 bg-gradient-to-r from-accent/[0.14] to-transparent px-5 py-3.5">
        <span className="mr-1 text-sm text-slate-300">Новая сессия «Кода» получит:</span>
        <Chip tone={persona ? "accent" : "muted"} onClick={() => go("personas")}>{persona ? `личность: ${persona.name}` : "без личности по умолчанию"}</Chip>
        <Chip tone={gets.length ? "accent" : "muted"} onClick={() => go("skills")}>{gets.length ? `навыков: ${gets.length}` : "без навыков"}</Chip>
        {mcp.length === 0 && <Chip tone="muted" onClick={() => go("mcp")}>без MCP-серверов</Chip>}
        {mcp.map((s) => {
          const h = mcpHealth(s);
          return (
            <Chip key={s.id} tone={h === "bad" ? "warn" : h === "ok" ? "ok" : "muted"} onClick={() => go("mcp")}>
              <span className={clsx("h-1.5 w-1.5 rounded-full", h === "ok" ? "bg-signal-ok" : h === "bad" ? "bg-signal-bad" : "bg-slate-500")} aria-hidden="true" />
              {s.name}{h === "bad" ? " — не запускается" : h === "unknown" ? " — не проверен" : ""}
            </Chip>
          );
        })}
        {broken.length > 0 && <span className="ml-auto text-xs text-signal-warn">Неработающий сервер агент просто пропустит — откройте его, чтобы увидеть причину.</span>}
      </div>

      <div className="mb-5 flex w-fit max-w-full flex-wrap gap-1 rounded-2xl border border-white/[0.08] bg-ink-900/60 p-1" role="tablist" aria-label="Разделы мастерской">
        {TABS.map((t) => {
          const on = t === tab;
          return (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => go(t)}
              className={clsx("flex items-center gap-2 rounded-xl px-4 py-2 text-sm transition-colors", on ? "border border-accent/40 bg-gradient-to-r from-accent/30 to-accent/10 text-white" : "border border-transparent text-slate-400 hover:text-white")}
            >
              {TAB_META[t].icon}
              {TAB_META[t].label}
              <span className={clsx("rounded-full px-1.5 text-[11px]", on ? "bg-white/15 text-white" : "bg-white/5 text-slate-400")}>{counts[t]}</span>
              {t === "mcp" && broken.length > 0 && <span className="h-2 w-2 rounded-full bg-signal-bad" aria-label="есть неработающий сервер" />}
            </button>
          );
        })}
      </div>

      {tab === "personas" && <Personas embedded onChanged={refresh} />}
      {tab === "skills" && <Skills embedded onChanged={refresh} />}
      {tab === "mcp" && <Mcp embedded onChanged={refresh} />}
    </>
  );
}
