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
  return <div className={clsx("glass rounded-2xl", className)}>{children}</div>;
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
        className="mb-5 h-10 w-10 rounded-full border border-accent/40"
        style={{ boxShadow: "0 0 30px rgba(124,92,255,0.35) inset" }}
      />
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      <p className="mt-1 max-w-sm text-xs leading-relaxed text-slate-500">{text}</p>
      {action && <div className="mt-5">{action}</div>}
    </Card>
  );
}

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-white">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
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
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
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
            className="glass max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-3xl p-7"
          >
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
