import clsx from "clsx";
import { motion } from "framer-motion";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

import { pluralRu } from "../lib/agentSessions";
import { listScans, type Role } from "../lib/api";
import { SECTION_PATH, type Section, type Tone } from "../lib/attention";
import { useAuth } from "../lib/auth";
import { useAttention } from "../lib/useAttention";
import Logo from "./Logo";

const ROLE_LABEL: Record<Role, string> = { admin: "администратор", operator: "оператор", viewer: "наблюдатель" };

// The menu in three groups. «Аккаунт» lives in the user card at the bottom; on a phone, where that card is hidden, it stays a menu item.
type Group = "work" | "resources" | "manage";
const GROUPS: { id: Group; label: string }[] = [
  { id: "work", label: "Работа" },
  { id: "resources", label: "Ресурсы" },
  { id: "manage", label: "Управление" },
];
const GROUP_OF: Record<string, Group> = {
  "/": "work", "/scans": "work", "/schedules": "work", "/code": "work", "/personas": "work", "/ops": "work",
  "/runners": "resources", "/llm": "resources", "/costs": "resources",
  "/settings": "manage", "/users": "manage", "/account": "manage",
};
const ORDER = ["/", "/scans", "/schedules", "/code", "/personas", "/ops", "/runners", "/llm", "/costs", "/settings", "/users", "/account"];

/** How many scans are in progress right now, for the label on «Сканы». Quietly absent when it cannot be asked. */
function useRunningScans(enabled: boolean): number {
  const [running, setRunning] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const ask = () =>
      !document.hidden &&
      listScans({ limit: 1 })
        .then((list) => alive && setRunning((list.counts?.running ?? 0) + (list.counts?.cancelling ?? 0)))
        .catch(() => undefined);
    void ask();
    const id = setInterval(ask, 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [enabled]);
  return running;
}

const icon = (children: ReactNode) => (
  <svg viewBox="0 0 24 24" className="h-[18px] w-[18px]" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    {children}
  </svg>
);

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  roles?: Role[];
  end?: boolean;
}

const NAV: NavItem[] = [
  {
    to: "/",
    label: "Обзор",
    end: true,
    icon: icon(
      <>
        <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
        <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
        <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
        <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
      </>
    ),
  },
  {
    to: "/scans",
    label: "Сканы",
    icon: icon(
      <>
        <circle cx="12" cy="12" r="8" />
        <circle cx="12" cy="12" r="3.2" />
        <path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3" />
      </>
    ),
  },
  {
    to: "/schedules",
    label: "Расписания",
    roles: ["admin", "operator"],
    icon: icon(
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 7v5l3.2 2" />
      </>
    ),
  },
  {
    to: "/costs",
    label: "Расходы",
    roles: ["admin", "operator"],
    icon: icon(
      <>
        <path d="M4 20V10M10 20V4M16 20v-7M21 20H3" />
      </>
    ),
  },
  {
    to: "/llm",
    label: "LLM-профили",
    roles: ["admin", "operator"],
    icon: icon(
      <>
        <circle cx="8" cy="12" r="3.5" />
        <path d="M11.5 12H21M17 12v3M20 12v2" />
      </>
    ),
  },
  {
    to: "/runners",
    label: "Раннеры",
    icon: icon(
      <>
        <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
        <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
        <path d="M7 7.25h.01M7 16.75h.01" />
      </>
    ),
  },
  {
    to: "/code",
    label: "Код",
    roles: ["admin", "operator"],
    icon: icon(
      <>
        <rect x="4" y="5" width="16" height="12" rx="2.5" />
        <path d="M8 21h8M12 17v4M9 11h.01M15 11h.01" />
      </>
    ),
  },
  {
    to: "/personas",
    label: "Личности",
    roles: ["admin", "operator"],
    icon: icon(
      <>
        <circle cx="12" cy="8" r="3.5" />
        <path d="M5 20c.8-3.6 3.7-5.5 7-5.5s6.2 1.9 7 5.5" />
        <path d="M17.5 4.5l1 1 2-2" />
      </>
    ),
  },
  {
    to: "/ops",
    label: "Операции",
    roles: ["admin"],
    icon: icon(
      <>
        <path d="M4 17l5-5-5-5M12 19h8" />
      </>
    ),
  },
  {
    to: "/settings",
    label: "Настройки",
    roles: ["admin"],
    icon: icon(
      <>
        <path d="M4 7h10M18 7h2M4 17h2M10 17h10" />
        <circle cx="16" cy="7" r="2" />
        <circle cx="8" cy="17" r="2" />
      </>
    ),
  },
  {
    to: "/users",
    label: "Пользователи",
    roles: ["admin"],
    icon: icon(
      <>
        <circle cx="9" cy="8.5" r="3.2" />
        <path d="M3.5 19c.6-3 2.8-4.7 5.5-4.7s4.9 1.7 5.5 4.7" />
        <path d="M16 5.6a3 3 0 0 1 0 5.8M17.5 14.6c1.8.5 3 2 3.4 4.4" />
      </>
    ),
  },
  {
    to: "/account",
    label: "Аккаунт",
    icon: icon(
      <>
        <circle cx="12" cy="8.5" r="3.5" />
        <path d="M5 20c.8-3.6 3.7-5.5 7-5.5s6.2 1.9 7 5.5" />
      </>
    ),
  },
];

const LIGHT_TONE: Record<Tone, string> = { ok: "bg-signal-ok", warn: "bg-signal-warn", bad: "bg-signal-bad", accent: "bg-accent-soft" };
const SECTION_BY_PATH = Object.fromEntries(Object.entries(SECTION_PATH).map(([section, path]) => [path, section as Section]));

export default function Layout() {
  const { user, logout, hasRole } = useAuth();
  const location = useLocation();
  const items = NAV.filter((n) => !n.roles || hasRole(...n.roles)).sort((a, b) => ORDER.indexOf(a.to) - ORDER.indexOf(b.to));
  const running = useRunningScans(user !== null);
  const canCreate = hasRole("admin", "operator");
  const lit = useAttention(user?.id ?? null, location.pathname);

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="glass flex shrink-0 flex-col border-0 border-white/5 md:sticky md:top-0 md:h-screen md:w-64 md:border-r">
        <div className="flex items-center gap-3 px-5 py-5">
          <Logo size={34} />
          <div>
            <div className="text-sm font-semibold tracking-tight text-white">Oreshnik</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">console</div>
          </div>
        </div>

        {canCreate && (
          <Link
            to="/scans/new"
            className="mx-3 mb-4 hidden h-10 items-center justify-center gap-2 rounded-xl bg-gradient-to-br from-accent to-accent-glow text-sm font-semibold text-white shadow-[0_8px_24px_-10px_rgba(124,92,255,0.7)] transition-transform hover:-translate-y-px md:flex"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
            Новый скан
          </Link>
        )}

        <nav className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-1 md:flex-col md:gap-0.5 md:overflow-y-auto md:pb-3">
          {GROUPS.map((group) => {
            const own = items.filter((item) => GROUP_OF[item.to] === group.id);
            if (own.length === 0) return null;
            return [
              <p key={group.id} className={clsx("hidden px-3 pb-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500 md:block", group.id !== "work" && "md:pt-4", group.id === "manage" && own.every((i) => i.to === "/account") && "md:hidden")}>
                {group.label}
              </p>,
              ...own.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={clsx("relative shrink-0", item.to === "/account" && "md:hidden")}>
              {({ isActive }) => (
                <div
                  className={clsx(
                    "relative flex items-center gap-3 rounded-xl px-3.5 py-2.5 text-sm transition-colors",
                    isActive ? "text-white" : "text-slate-400 hover:text-slate-200"
                  )}
                >
                  {isActive && (
                    <motion.span
                      layoutId="nav-active"
                      className="absolute inset-0 rounded-xl border border-accent/45 bg-gradient-to-r from-accent/25 to-accent/[0.08] shadow-[0_10px_28px_-16px_rgba(124,92,255,0.9)]"
                      transition={{ type: "spring", stiffness: 420, damping: 34 }}
                    />
                  )}
                  <span className="relative">{item.icon}</span>
                  <span className="relative whitespace-nowrap">{item.label}</span>
                  {item.to === "/scans" && running > 0 && (
                    <span className="relative ml-auto rounded-full bg-accent/25 px-2 py-px text-[11px] font-semibold text-accent-soft" title="Сканов выполняется сейчас">
                      {running} {pluralRu(running, ["идёт", "идут", "идут"])}
                    </span>
                  )}
                  {(() => {
                    const light = lit[SECTION_BY_PATH[item.to]];
                    if (!light) return null;
                    return (
                      <span className={clsx("relative flex h-2 w-2", !(item.to === "/scans" && running > 0) && "ml-auto")} title={light.title} aria-label={light.title} role="status">
                        {light.pulse && <span className={clsx("absolute inline-flex h-full w-full animate-ping rounded-full opacity-70", LIGHT_TONE[light.tone])} />}
                        <span className={clsx("relative inline-flex h-2 w-2 rounded-full", LIGHT_TONE[light.tone])} />
                      </span>
                    );
                  })()}
                </div>
              )}
            </NavLink>
          )),
            ];
          })}
        </nav>

        <div className="hidden p-3 md:block">
          <div className="flex items-center gap-2.5 rounded-2xl border border-white/[0.08] bg-ink-800/70 py-2 pl-2.5 pr-1.5">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent/50 to-accent-glow/30 text-sm font-semibold uppercase text-white ring-1 ring-inset ring-white/10">
              {user?.email.slice(0, 1) ?? "?"}
            </span>
            <Link to="/account" className="min-w-0 flex-1" title="Аккаунт">
              <span className="block truncate text-[13px] text-white">{user?.email}</span>
              {user && <span className="block text-[11px] text-accent-soft">{ROLE_LABEL[user.role]}</span>}
            </Link>
            <button onClick={logout} aria-label="Выйти" title="Выйти" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-slate-400 transition hover:bg-white/5 hover:text-white">
              <svg viewBox="0 0 24 24" className="h-[18px] w-[18px]" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M10 5H6a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h4M15 8l4 4-4 4M19 12H10" /></svg>
            </button>
          </div>
        </div>
      </aside>

      <main className="min-w-0 flex-1 px-5 py-8 md:px-8 md:py-10 xl:px-10 2xl:px-12">
        <motion.div
          key={location.pathname}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
          className="w-full"
        >
          <Outlet />
        </motion.div>
        <button onClick={logout} className="mt-10 text-xs text-slate-600 hover:text-white md:hidden">
          Выйти ({user?.email})
        </button>
      </main>
    </div>
  );
}
