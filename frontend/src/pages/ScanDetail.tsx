import clsx from "clsx";
import { motion } from "framer-motion";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import AgentsPanel from "../components/AgentsPanel";
import FindingsPanel from "../components/FindingsPanel";
import LogViewer from "../components/LogViewer";
import { MODE_META, SEVERITIES, SEVERITY_LABEL, SEVERITY_STYLE, SeverityBar, StatusBadge, isActive } from "../components/scan";
import { Button, Card, EmptyState, ErrorNote, Spinner } from "../components/ui";
import {
  cancelScan,
  deleteScan,
  downloadExport,
  errorMessage,
  getReport,
  getScan,
  listFindings,
  type ExportFormat,
  type Finding,
  type Scan,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatCost, formatDateTime, formatDuration, formatTokens, scanDuration } from "../lib/format";
import { useScanActivity } from "../lib/useScanActivity";
import { useScanStream } from "../lib/useScanStream";

// The markdown stack is only needed for the report tab, so it is loaded on demand.
const SafeMarkdown = lazy(() => import("../components/Markdown"));

type Tab = "agents" | "log" | "findings" | "report" | "params";

function useNow(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [enabled]);
  return enabled ? now : Date.now();
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wider text-slate-600">{label}</dt>
      <dd className="mt-1 truncate text-sm text-slate-200">{value}</dd>
    </div>
  );
}

/** Validates the URL and remounts per scan, so no state (tab, findings, log) leaks between scans. */
export default function ScanDetail() {
  const raw = useParams().id ?? "";
  const id = /^\d+$/.test(raw) ? Number(raw) : null;
  if (id === null) {
    return <EmptyState title="Скан не найден" text="Некорректный адрес." action={<Link to="/scans"><Button>К списку сканов</Button></Link>} />;
  }
  return <ScanDetailView key={id} scanId={id} />;
}

function ScanDetailView({ scanId }: { scanId: number }) {
  const navigate = useNavigate();
  const { user, hasRole } = useAuth();

  const [scan, setScan] = useState<Scan | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("log");
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [report, setReport] = useState<string | null | undefined>(undefined);
  const [busy, setBusy] = useState<"cancel" | "delete" | ExportFormat | null>(null);
  const tabChosen = useRef(false);
  const scanRef = useRef<Scan | null>(null);
  scanRef.current = scan;

  const reload = useCallback(async () => {
    try {
      setScan(await getScan(scanId));
    } catch (e) {
      if ((e as { response?: { status?: number } }).response?.status === 404) setMissing(true);
      else setError(errorMessage(e));
    }
  }, [scanId]);

  const { buffer, state } = useScanStream(scanId, {
    onStatus: (status) => {
      if (scanRef.current?.status !== status) void reload();
    },
  });

  useEffect(() => {
    void reload();
  }, [reload]);

  const active = scan ? isActive(scan.status) : false;

  // Fallback to polling in case the socket is down.
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => !document.hidden && void reload(), 5000);
    return () => clearInterval(id);
  }, [active, reload]);

  // Results exist once the scan is over.
  const finished = scan !== null && !active;
  const findingsKey = scan ? `${scan.status}:${scan.findings_total}` : "";
  useEffect(() => {
    if (!finished) return;
    let alive = true;
    listFindings(scanId).then((f) => alive && setFindings(f)).catch((e) => alive && setError(errorMessage(e)));
    getReport(scanId).then((r) => alive && setReport(r)).catch(() => alive && setReport(null));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished, scanId, findingsKey]);

  // Raw agent transcripts are not shown to viewers (the API refuses them too).
  const canSeeAgents = !hasRole("viewer");

  // Open on the most useful tab once, when the scan first loads: what the agents are doing while it runs,
  // the findings once it is over.
  useEffect(() => {
    if (scan && !tabChosen.current) {
      tabChosen.current = true;
      if (isActive(scan.status) && canSeeAgents) setTab("agents");
      else if (!isActive(scan.status) && scan.findings_total > 0) setTab("findings");
    }
  }, [scan, canSeeAgents]);

  const activity = useScanActivity(scanId, { enabled: canSeeAgents && (tab === "agents" || active), active });

  const now = useNow(active);
  const duration = useMemo(() => (scan ? scanDuration(scan, now) : null), [scan, now]);

  const canModify = scan !== null && (hasRole("admin") || (hasRole("operator") && scan.user_id === user?.id));

  const run = async (kind: "cancel" | "delete" | ExportFormat, fn: () => Promise<unknown>) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const onCancel = () =>
    window.confirm("Остановить скан? Уже найденное сохранится.") &&
    run("cancel", async () => setScan(await cancelScan(scanId)));
  const onDelete = () =>
    window.confirm("Удалить скан вместе с находками и логом? Это необратимо.") &&
    run("delete", async () => {
      await deleteScan(scanId);
      navigate("/scans", { replace: true });
    });

  if (missing) {
    return (
      <EmptyState title="Скан не найден" text="Его нет или он принадлежит другому пользователю." action={<Link to="/scans"><Button>К списку сканов</Button></Link>} />
    );
  }
  if (!scan) {
    return error ? <ErrorNote message={error} /> : <div className="flex justify-center py-24"><Spinner className="h-6 w-6" /></div>;
  }

  const tabs: { id: Tab; label: string; count?: number }[] = [
    ...(canSeeAgents ? [{ id: "agents" as Tab, label: "Агенты", count: activity.agents.length }] : []),
    { id: "log", label: "Лог" },
    { id: "findings", label: "Находки", count: scan.findings_total },
    { id: "report", label: "Отчёт" },
    ...(scan.instruction || scan.max_budget ? [{ id: "params" as Tab, label: "Параметры" }] : []),
  ];

  const detailTone =
    scan.status === "failed" ? "border-signal-bad/25 bg-signal-bad/10 text-signal-bad"
    : scan.status_detail && (scan.status === "succeeded" || scan.status === "vulns_found") ? "border-signal-warn/25 bg-signal-warn/10 text-signal-warn"
    : "border-white/10 bg-white/5 text-slate-400";

  return (
    <>
      <Link to="/scans" className="mb-5 inline-block text-sm text-slate-500 transition hover:text-white">← Сканы</Link>

      <Card className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-xl font-semibold tracking-tight text-white">Скан #{scan.id}</h1>
              <StatusBadge status={scan.status} />
            </div>
            <ul className="mt-3 flex flex-wrap gap-2">
              {scan.targets.map((t) => (
                <li key={t} className="max-w-full truncate rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 font-mono text-xs text-slate-300">{t}</li>
              ))}
            </ul>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {finished && (["json", "sarif", "md"] as ExportFormat[]).map((fmt) => (
              <Button key={fmt} loading={busy === fmt} onClick={() => run(fmt, () => downloadExport(scanId, fmt))} className="!px-3 !py-1.5 text-xs" title={`Скачать ${fmt.toUpperCase()}`}>
                ↓ {fmt.toUpperCase()}
              </Button>
            ))}
            {canModify && active && scan.status !== "cancelling" && (
              <Button variant="danger" loading={busy === "cancel"} onClick={onCancel}>Остановить</Button>
            )}
            {canModify && finished && (
              <Button variant="danger" loading={busy === "delete"} onClick={onDelete}>Удалить</Button>
            )}
          </div>
        </div>

        {scan.status_detail && (
          <p className={clsx("mt-5 rounded-lg border px-3.5 py-2.5 text-xs leading-relaxed [overflow-wrap:anywhere]", detailTone)}>{scan.status_detail}</p>
        )}

        <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Режим" value={MODE_META[scan.scan_mode].label} />
          <Stat label="Раннер" value={scan.runner_name ?? "удалён"} />
          <Stat label="Длительность" value={duration === null ? "—" : formatDuration(duration)} />
          <Stat label="Стоимость" value={formatCost(scan.cost)} />
          <Stat label="Токены" value={formatTokens(scan.total_tokens)} />
          <Stat label="Владелец" value={scan.owner_email} />
        </dl>

        {scan.findings_total > 0 && (
          <div className="mt-6">
            <SeverityBar counts={scan.severity_counts} className="!h-2" />
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
              {SEVERITIES.filter((s) => scan.severity_counts[s]).map((s) => (
                <span key={s} className="inline-flex items-center gap-2 text-xs text-slate-400">
                  <span className={clsx("h-2 w-2 rounded-full", SEVERITY_STYLE[s].bar)} />
                  {SEVERITY_LABEL[s]} <span className="font-semibold text-slate-200">{scan.severity_counts[s]}</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </Card>

      <ErrorNote message={error} />

      <div className="mb-5 mt-8 flex gap-1 border-b border-white/10">
        {tabs.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)} className={clsx("relative px-4 py-2.5 text-sm transition-colors", tab === t.id ? "text-white" : "text-slate-500 hover:text-slate-300")}>
            {t.label}
            {t.count !== undefined && t.count > 0 && <span className="ml-2 rounded-full bg-white/10 px-1.5 py-0.5 text-[11px] text-slate-300">{t.count}</span>}
            {tab === t.id && <motion.span layoutId="scan-tab" className="absolute inset-x-0 -bottom-px h-0.5 bg-accent" transition={{ type: "spring", stiffness: 420, damping: 34 }} />}
          </button>
        ))}
      </div>

      {tab === "agents" && canSeeAgents && (
        <AgentsPanel events={activity.events} agents={activity.agents} loaded={activity.loaded} error={activity.error} active={active} />
      )}

      {tab === "log" && <LogViewer buffer={buffer} state={state} active={active} />}

      {tab === "findings" && (
        active ? (
          <EmptyState title="Находки появятся после завершения" text="Strix печатает их в лог по ходу работы; в таблицу они сохраняются, когда скан закончен." />
        ) : findings === null ? (
          <div className="flex justify-center py-16"><Spinner className="h-6 w-6" /></div>
        ) : findings.length === 0 ? (
          <EmptyState title="Находок нет" text="Скан завершился без находок. Это не доказательство, что цель безопасна: проверено только то, что успел охватить скан." />
        ) : (
          <FindingsPanel scanId={scanId} findings={findings} />
        )
      )}

      {tab === "report" && (
        <Card className="p-6 sm:p-8">
          {active ? (
            <p className="text-sm text-slate-500">Отчёт формируется в конце скана.</p>
          ) : report === undefined ? (
            <div className="flex justify-center py-10"><Spinner /></div>
          ) : report === null ? (
            <p className="text-sm text-slate-500">Отчёта нет — Strix не успел его записать.</p>
          ) : (
            <Suspense fallback={<div className="flex justify-center py-10"><Spinner /></div>}>
              <SafeMarkdown>{report}</SafeMarkdown>
            </Suspense>
          )}
        </Card>
      )}

      {tab === "params" && (
        <Card className="space-y-5 p-6">
          <dl className="grid gap-x-6 gap-y-5 sm:grid-cols-3">
            <Stat label="LLM-профиль" value={scan.llm_profile_name ?? "—"} />
            <Stat label="Лимит бюджета" value={scan.max_budget ? `$${scan.max_budget}` : "не задан"} />
            <Stat label="Создан" value={formatDateTime(scan.created_at)} />
          </dl>
          {scan.instruction && (
            <div>
              <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Инструкция</h3>
              <p className="whitespace-pre-wrap text-sm text-slate-300 [overflow-wrap:anywhere]">{scan.instruction}</p>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
