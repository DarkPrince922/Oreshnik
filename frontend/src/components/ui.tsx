import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { useEffect, type ButtonHTMLAttributes, type ReactNode } from "react";

// ── Motion presets ───────────────────────────────────────────────────────

export const stagger = {
  hidden: {},
  show: { transition: { staggerChildren: 0.06 } },
};
export const rise = {
  hidden: { opacity: 0, y: 14 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: [0.22, 1, 0.36, 1] as const } },
};

// ── Primitives ───────────────────────────────────────────────────────────

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={clsx(
        "inline-block h-4 w-4 animate-spin rounded-full border-2 border-white/20 border-t-white",
        className
      )}
    />
  );
}

type Variant = "primary" | "ghost" | "danger";
interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
}
export function Button({ variant = "ghost", loading, className, children, disabled, ...rest }: ButtonProps) {
  const styles: Record<Variant, string> = {
    primary:
      "text-white bg-gradient-to-br from-accent to-accent-glow shadow-[0_8px_24px_-10px_rgba(124,92,255,0.7)] hover:-translate-y-px",
    ghost: "text-slate-300 bg-white/5 border border-white/10 hover:bg-white/10 hover:text-white",
    danger: "text-signal-bad bg-signal-bad/10 border border-signal-bad/20 hover:bg-signal-bad/20",
  };
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition-all duration-200",
        "disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0",
        styles[variant],
        className
      )}
    >
      {loading && <Spinner className="h-3.5 w-3.5" />}
      {children}
    </button>
  );
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx("glass rounded-2xl bg-gradient-to-b from-white/[0.035] to-transparent", className)}>{children}</div>;
}

type Tone = "ok" | "warn" | "bad" | "accent" | "muted";
export function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  const tones: Record<Tone, string> = {
    ok: "bg-signal-ok/10 text-signal-ok border-signal-ok/20",
    warn: "bg-signal-warn/10 text-signal-warn border-signal-warn/20",
    bad: "bg-signal-bad/10 text-signal-bad border-signal-bad/20",
    accent: "bg-accent/10 text-accent-soft border-accent/20",
    muted: "bg-white/5 text-slate-400 border-white/10",
  };
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium", tones[tone])}>
      {children}
    </span>
  );
}

export function StatusDot({ tone }: { tone: "ok" | "warn" | "bad" | "muted" }) {
  const color = { ok: "bg-signal-ok", warn: "bg-signal-warn", bad: "bg-signal-bad", muted: "bg-slate-500" }[tone];
  return (
    <span className="relative inline-flex h-2 w-2">
      {tone === "ok" && <span className="absolute inline-flex h-full w-full rounded-full bg-signal-ok animate-pulse-ring" />}
      <span className={clsx("relative inline-flex h-2 w-2 rounded-full", color)} />
    </span>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium text-slate-400">{label}</span>
      {children}
      {hint && <span className="mt-1.5 block text-[11px] leading-relaxed text-slate-500">{hint}</span>}
    </label>
  );
}

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={clsx(
        "relative h-6 w-11 shrink-0 rounded-full border transition-colors duration-200 disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "border-accent/50 bg-accent/40" : "border-white/10 bg-white/10"
      )}
    >
      <motion.span
        className="absolute left-0.5 top-0.5 h-[18px] w-[18px] rounded-full bg-white shadow"
        animate={{ x: checked ? 20 : 0 }}
        transition={{ type: "spring", stiffness: 500, damping: 32 }}
      />
    </button>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  return (
    <AnimatePresence>
      {message && (
        <motion.p
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
          className="overflow-hidden rounded-lg border border-signal-bad/20 bg-signal-bad/10 px-3 py-2 text-xs text-signal-bad"
        >
          {message}
        </motion.p>
      )}
    </AnimatePresence>
  );
}

export function EmptyState({ title, text, action }: { title: string; text: string; action?: ReactNode }) {
  return (
    <Card className="flex flex-col items-center px-6 py-14 text-center">
      <motion.div
        animate={{ y: [0, -6, 0] }}
        transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
        className="mb-5 flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-accent/40 to-accent-glow/20 text-white shadow-[0_0_30px_-4px_rgba(124,92,255,0.7)] ring-1 ring-inset ring-white/10"
      >
        <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="8" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      </motion.div>
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      <p className="mt-1 max-w-sm text-xs leading-relaxed text-slate-400">{text}</p>
      {action && <div className="mt-5">{action}</div>}
    </Card>
  );
}

// The mark beside a page's title, by the title itself: every page gets the look without having to ask for it.
const PAGE_MARK: Record<string, ReactNode> = {
  "Расписания": <><circle cx="12" cy="12" r="8" /><path d="M12 8v4l3 2" /></>,
  "Расходы": <path d="M5 20V10M12 20V4M19 20v-7" />,
  "LLM-профили": <><circle cx="8" cy="12" r="3" /><path d="M11 12h9M17 12v3" /></>,
  "Раннеры": <><rect x="4" y="5" width="16" height="6" rx="1.5" /><rect x="4" y="13" width="16" height="6" rx="1.5" /></>,
  "Операции": <path d="M4 17l5-5-5-5M12 19h8" />,
  "Настройки": <><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></>,
  "Пользователи": <><circle cx="9" cy="9" r="3" /><path d="M3.5 19c.6-3 2.8-4.7 5.5-4.7s4.9 1.7 5.5 4.7M16 6a3 3 0 0 1 0 6" /></>,
  "Агент": <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M7 20h10M12 16v4M7 8l3 2-3 2M12 12h4" /></>,
  "Навыки": <><path d="M5 4h9l5 5v11H5z" /><path d="M14 4v5h5M8.5 13h7M8.5 16.5h4.5" /></>,
  "Личности": <><circle cx="12" cy="8" r="3.5" /><path d="M5 20c.8-3.6 3.7-5.5 7-5.5s6.2 1.9 7 5.5" /></>,
  "Аккаунт": <><circle cx="12" cy="8.5" r="3.5" /><path d="M5 20c.8-3.6 3.7-5.5 7-5.5s6.2 1.9 7 5.5" /></>,
  "Новый скан": <><circle cx="12" cy="12" r="8" /><path d="M12 9v6M9 12h6" /></>,
};

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  const mark = PAGE_MARK[title];
  return (
    <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <div className="flex items-center gap-3">
          {mark && (
            <span aria-hidden="true" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-glow text-white shadow-[0_8px_24px_-8px_rgba(124,92,255,0.8)]">
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                {mark}
              </svg>
            </span>
          )}
          <h1 className="bg-gradient-to-r from-white to-accent-soft bg-clip-text text-2xl font-semibold tracking-tight text-transparent">{title}</h1>
        </div>
        {subtitle && <p className="mt-1.5 text-sm text-slate-400">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => e.target === e.currentTarget && onClose()}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={title}
            initial={{ opacity: 0, y: 20, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 12, scale: 0.98 }}
            transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
            className={clsx("glass relative max-h-[90vh] w-full overflow-y-auto rounded-3xl p-7 shadow-[0_30px_90px_-30px_rgba(124,92,255,0.55)]", wide ? "max-w-3xl" : "max-w-lg")}
          >
            <span aria-hidden="true" className="pointer-events-none absolute inset-x-12 top-0 h-px bg-gradient-to-r from-transparent via-accent-soft/70 to-transparent" />
            <div className="mb-5 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-white">{title}</h2>
              <button
                onClick={onClose}
                aria-label="Закрыть"
                className="rounded-lg px-2 py-1 text-slate-500 transition hover:bg-white/5 hover:text-white"
              >
                ✕
              </button>
            </div>
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "никогда";
  const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return "только что";
  if (secs < 3600) return `${Math.floor(secs / 60)} мин назад`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} ч назад`;
  return `${Math.floor(secs / 86400)} дн назад`;
}
