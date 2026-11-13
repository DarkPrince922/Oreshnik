import clsx from "clsx";
import { motion } from "framer-motion";

import type { ScanMode, ScanStatus, Severity } from "../lib/api";
import { StatusDot } from "./ui";

// Tailwind only sees complete class names, so every variant is spelled out here.
export const SEVERITIES: Severity[] = ["critical", "high", "medium", "low", "info", "unknown"];

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
  info: "Info",
  unknown: "Unknown",
};

export const SEVERITY_STYLE: Record<Severity, { chip: string; bar: string; text: string }> = {
  critical: { chip: "bg-sev-critical/15 text-sev-critical border-sev-critical/30", bar: "bg-sev-critical", text: "text-sev-critical" },
  high: { chip: "bg-sev-high/15 text-sev-high border-sev-high/30", bar: "bg-sev-high", text: "text-sev-high" },
  medium: { chip: "bg-sev-medium/15 text-sev-medium border-sev-medium/30", bar: "bg-sev-medium", text: "text-sev-medium" },
  low: { chip: "bg-sev-low/15 text-sev-low border-sev-low/30", bar: "bg-sev-low", text: "text-sev-low" },
  info: { chip: "bg-sev-info/15 text-sev-info border-sev-info/30", bar: "bg-sev-info", text: "text-sev-info" },
  unknown: { chip: "bg-sev-unknown/15 text-sev-unknown border-sev-unknown/30", bar: "bg-sev-unknown", text: "text-sev-unknown" },
};

export function SeverityChip({ severity, children }: { severity: Severity; children?: React.ReactNode }) {
  const s = SEVERITY_STYLE[severity] ?? SEVERITY_STYLE.unknown;
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide", s.chip)}>
      {children ?? SEVERITY_LABEL[severity] ?? severity}
    </span>
  );
}

/** Segmented bar of findings per severity; segments grow in on mount. */
export function SeverityBar({ counts, className }: { counts: Partial<Record<Severity, number>>; className?: string }) {
  const total = SEVERITIES.reduce((n, s) => n + (counts[s] ?? 0), 0);
  if (total === 0) return <div className={clsx("h-1.5 rounded-full bg-white/5", className)} />;
  return (
    <div className={clsx("flex h-1.5 gap-0.5 overflow-hidden rounded-full", className)} role="img" aria-label={`Находок: ${total}`}>
      {SEVERITIES.filter((s) => counts[s]).map((s) => (
        <motion.div
          key={s}
          className={SEVERITY_STYLE[s].bar}
          initial={{ flexGrow: 0 }}
          animate={{ flexGrow: counts[s] }}
          transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
          style={{ flexBasis: 0 }}
        />
      ))}
    </div>
  );
}

export function SeverityCounts({ counts }: { counts: Partial<Record<Severity, number>> }) {
  const present = SEVERITIES.filter((s) => counts[s]);
  if (present.length === 0) return <span className="text-xs text-slate-600">—</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {present.map((s) => (
        <span key={s} className={clsx("inline-flex items-center gap-1 text-xs font-semibold", SEVERITY_STYLE[s].text)} title={SEVERITY_LABEL[s]}>
          <span className={clsx("h-1.5 w-1.5 rounded-full", SEVERITY_STYLE[s].bar)} />
          {counts[s]}
        </span>
      ))}
    </span>
  );
}

// ── Scan status ──────────────────────────────────────────────────────────

type Tone = "ok" | "warn" | "bad" | "muted" | "accent";
export const STATUS_META: Record<ScanStatus, { label: string; tone: Tone; active: boolean }> = {
  queued: { label: "В очереди", tone: "muted", active: true },
  running: { label: "Выполняется", tone: "accent", active: true },
  cancelling: { label: "Отмена…", tone: "warn", active: true },
  // "succeeded" means the run finished and nothing was found — not that the target is secure.
  succeeded: { label: "Завершён, находок нет", tone: "ok", active: false },
  vulns_found: { label: "Найдены уязвимости", tone: "bad", active: false },
  failed: { label: "Ошибка", tone: "bad", active: false },
  cancelled: { label: "Отменён", tone: "muted", active: false },
};

export const isActive = (status: ScanStatus) => STATUS_META[status].active;

const TONE_STYLE: Record<Tone, string> = {
  ok: "bg-signal-ok/10 text-signal-ok border-signal-ok/25",
  warn: "bg-signal-warn/10 text-signal-warn border-signal-warn/25",
  bad: "bg-signal-bad/10 text-signal-bad border-signal-bad/25",
  muted: "bg-white/5 text-slate-400 border-white/10",
  accent: "bg-accent/15 text-accent-soft border-accent/30",
};

export function StatusBadge({ status }: { status: ScanStatus }) {
  const meta = STATUS_META[status];
  return (
    <span className={clsx("inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium whitespace-nowrap", TONE_STYLE[meta.tone])}>
      {status === "running" ? (
        <span className="relative inline-flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full rounded-full bg-accent-soft animate-pulse-ring" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-accent-soft" />
        </span>
      ) : status === "queued" || status === "cancelling" ? (
        <motion.span
          className="h-2 w-2 rounded-full bg-current"
          animate={{ opacity: [1, 0.3, 1] }}
          transition={{ duration: 1.4, repeat: Infinity }}
        />
      ) : (
        <StatusDot tone={meta.tone === "accent" ? "ok" : meta.tone === "bad" ? "bad" : meta.tone === "warn" ? "warn" : meta.tone === "ok" ? "ok" : "muted"} />
      )}
      {meta.label}
    </span>
  );
}

export const MODE_META: Record<ScanMode, { label: string; hint: string }> = {
  quick: { label: "Быстрый", hint: "Минуты, самый дешёвый. Для проверки конфигурации и CI." },
  standard: { label: "Стандартный", hint: "Регулярное тестирование: баланс глубины и стоимости." },
  deep: { label: "Глубокий", hint: "Тщательный обзор. Долго и дорого — задайте лимит бюджета." },
};
