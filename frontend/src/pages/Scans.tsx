import clsx from "clsx";
import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { SeverityBar, SeverityCounts, StatusBadge, STATUS_META, MODE_META, isActive } from "../components/scan";
import { Button, Card, EmptyState, ErrorNote, PageHeader, Spinner, relativeTime, rise, stagger } from "../components/ui";
import { errorMessage, listScans, type Scan, type ScanList, type ScanStatus } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatCost, targetsLabel } from "../lib/format";

const PAGE = 15;
const FILTERS: { value: ScanStatus | "all"; label: string }[] = [
  { value: "all", label: "Все" },
  { value: "running", label: "Выполняются" },
  { value: "queued", label: "В очереди" },
  { value: "vulns_found", label: "С уязвимостями" },
  { value: "succeeded", label: "Без находок" },
  { value: "failed", label: "Ошибки" },
  { value: "cancelled", label: "Отменённые" },
];

function Row({ scan, showOwner }: { scan: Scan; showOwner: boolean }) {
  const { first, more } = targetsLabel(scan.targets);
  return (
    <motion.li variants={rise}>
      <Link
        to={`/scans/${scan.id}`}
        className="grid items-center gap-x-4 gap-y-2 px-5 py-4 transition-colors hover:bg-white/[0.04] md:grid-cols-[3.5rem_minmax(0,1fr)_12.5rem_7rem_5rem_6.5rem]"
      >
        <span className="font-mono text-xs text-slate-600">#{scan.id}</span>
        <span className="min-w-0">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-white">{first}</span>
            {more > 0 && <span className="shrink-0 rounded-md bg-white/10 px-1.5 py-0.5 text-[11px] text-slate-400">+{more}</span>}
          </span>
          <span className="mt-0.5 block truncate text-xs text-slate-500">
            {MODE_META[scan.scan_mode].label} · {scan.runner_name ?? "раннер удалён"}
            {scan.parent_scan_id !== null && ` · ↻ из #${scan.parent_scan_id}`}
            {showOwner && ` · ${scan.owner_email}`}
          </span>
        </span>
        <span><StatusBadge status={scan.status} /></span>
        <span className="flex flex-col gap-1.5">
          <SeverityCounts counts={scan.severity_counts} />
          <SeverityBar counts={scan.severity_counts} />
        </span>
        <span className="font-mono text-xs text-slate-400">{formatCost(scan.cost)}</span>
        <span className="text-xs text-slate-500">{relativeTime(scan.started_at ?? scan.created_at)}</span>
      </Link>
    </motion.li>
  );
}

export default function Scans() {
  const { hasRole } = useAuth();
  const [filter, setFilter] = useState<ScanStatus | "all">("all");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<ScanList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const mine = ++requestId.current;
    try {
      const result = await listScans({ status: filter === "all" ? undefined : filter, limit: PAGE, offset: page * PAGE });
      if (mine === requestId.current) {
        setData(result);
        setError(null);
      }
    } catch (e) {
      if (mine === requestId.current) setError(errorMessage(e));
    }
  }, [filter, page]);

  useEffect(() => {
    setData(null);
    void load();
  }, [load]);

  // Keep the list fresh while anything on the page is still moving.
  const anyActive = data?.items.some((s) => isActive(s.status)) ?? false;
  useEffect(() => {
    if (!anyActive) return;
    const id = setInterval(() => !document.hidden && void load(), 3000);
    return () => clearInterval(id);
  }, [anyActive, load]);

  const canCreate = hasRole("admin", "operator");
  const showOwner = !hasRole("operator");
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE)) : 1;

  return (
    <>
      <PageHeader
        title="Сканы"
        subtitle={data ? `Всего: ${data.total}` : "История и текущие запуски"}
        action={canCreate ? <Link to="/scans/new"><Button variant="primary">+ Новый скан</Button></Link> : undefined}
      />

      <div className="mb-5 flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => { setFilter(f.value); setPage(0); }}
            className={clsx(
              "rounded-full border px-3.5 py-1.5 text-xs font-medium transition-colors",
              filter === f.value ? "border-accent/40 bg-accent/15 text-white" : "border-white/10 bg-white/5 text-slate-400 hover:text-slate-200"
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      <ErrorNote message={error} />
      {data === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {data && data.items.length === 0 && (
        <EmptyState
          title={filter === "all" ? "Сканов пока нет" : `Нет сканов со статусом «${STATUS_META[filter as ScanStatus].label}»`}
          text={filter === "all" ? "Запустите первый скан — для отладки панели выберите раннер mock: он ничего не тратит." : "Смените фильтр, чтобы увидеть остальные."}
          action={filter === "all" && canCreate ? <Link to="/scans/new"><Button variant="primary">Новый скан</Button></Link> : undefined}
        />
      )}

      {data && data.items.length > 0 && (
        <Card className="overflow-hidden">
          <div className="hidden border-b border-white/5 px-5 py-2.5 text-[11px] uppercase tracking-wider text-slate-600 md:grid md:grid-cols-[3.5rem_minmax(0,1fr)_12.5rem_7rem_5rem_6.5rem] md:gap-x-4">
            <span>id</span><span>Цели</span><span>Статус</span><span>Находки</span><span>Стоимость</span><span>Запуск</span>
          </div>
          <motion.ul key={`${filter}-${page}`} variants={stagger} initial="hidden" animate="show" className="divide-y divide-white/5">
            {data.items.map((s) => <Row key={s.id} scan={s} showOwner={showOwner} />)}
          </motion.ul>
        </Card>
      )}

      {data && pages > 1 && (
        <div className="mt-5 flex items-center justify-center gap-3 text-sm text-slate-500">
          <Button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>←</Button>
          <span>{page + 1} / {pages}</span>
          <Button disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>→</Button>
        </div>
      )}
    </>
  );
}
