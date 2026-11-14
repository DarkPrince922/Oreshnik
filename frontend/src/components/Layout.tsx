import clsx from "clsx";
import { motion } from "framer-motion";
import type { ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import type { Role } from "../lib/api";
import { SECTION_PATH, type Section, type Tone } from "../lib/attention";
import { useAuth } from "../lib/auth";
import { useAttention } from "../lib/useAttention";
import Logo from "./Logo";
import { Badge } from "./ui";

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
    to: "/agent",
    label: "Агент",
    roles: ["admin", "operator"],
    icon: icon(
      <>
        <rect x="4" y="5" width="16" height="12" rx="2.5" />
        <path d="M8 21h8M12 17v4M9 11h.01M15 11h.01" />
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

const ROLE_TONE = { admin: "accent", operator: "ok", viewer: "muted" } as const;
const LIGHT_TONE: Record<Tone, string> = { ok: "bg-signal-ok", warn: "bg-signal-warn", bad: "bg-signal-bad", accent: "bg-accent-soft" };
const SECTION_BY_PATH = Object.fromEntries(Object.entries(SECTION_PATH).map(([section, path]) => [path, section as Section]));

export default function Layout() {
  const { user, logout, hasRole } = useAuth();
  const location = useLocation();
  const items = NAV.filter((n) => !n.roles || hasRole(...n.roles));
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

        <nav className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-1 md:flex-col md:overflow-visible md:pb-0">
          {items.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className="relative shrink-0">
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
                      className="absolute inset-0 rounded-xl border border-accent/30 bg-accent/10"
                      transition={{ type: "spring", stiffness: 420, damping: 34 }}
                    />
                  )}
                  <span className="relative">{item.icon}</span>
                  <span className="relative whitespace-nowrap">{item.label}</span>
                  {(() => {
                    const light = lit[SECTION_BY_PATH[item.to]];
                    if (!light) return null;
                    return (
                      <span className="relative ml-auto flex h-2 w-2" title={light.title} aria-label={light.title} role="status">
                        {light.pulse && <span className={clsx("absolute inline-flex h-full w-full animate-ping rounded-full opacity-70", LIGHT_TONE[light.tone])} />}
                        <span className={clsx("relative inline-flex h-2 w-2 rounded-full", LIGHT_TONE[light.tone])} />
                      </span>
                    );
                  })()}
                </div>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="hidden border-t border-white/5 p-4 md:block">
          <div className="truncate text-sm text-slate-200">{user?.email}</div>
          <div className="mt-1.5 flex items-center justify-between">
            {user && <Badge tone={ROLE_TONE[user.role]}>{user.role}</Badge>}
            <button onClick={logout} className="text-xs text-slate-500 transition hover:text-white">
              Выйти
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
