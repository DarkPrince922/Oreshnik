import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { AlertIcon, CheckIcon, ChevronLeftIcon, ChevronRightIcon, ClockIcon, CloseIcon, MinusIcon, PlusIcon, SearchIcon } from "../components/agent/icons";
import { SEVERITIES, SEVERITY_LABEL, SEVERITY_STYLE, STATUS_META, MODE_META, isActive } from "../components/scan";
import { Button, Card, EmptyState, ErrorNote, Spinner, relativeTime } from "../components/ui";
import { cancelScan, errorMessage, listScans, type Scan, type ScanList, type Severity } from "../lib/api";
import { useAuth } from "../lib/auth";
import { continueKind } from "../lib/continueScan";
import { formatCost, formatDuration, scanDuration, targetsLabel } from "../lib/format";
import { pageRange, scansSummary, sections, SEVERITY_LETTER, tileCount, TILES, type Counts, type ScanFilter } from "../lib/scansList";

const PAGE = 15;
const LIVE_LIMIT = 30;

function useDebounced<T>(value: T, ms: number): T {
  const [late, setLate] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setLate(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return late;
}

function Targets({ scan, className }: { scan: Scan; className?: string }) {
  const { first, more } = targetsLabel(scan.targets);
  return (
    <span className={clsx("flex min-w-0 items-center gap-2", className)}>
      <span className="truncate">{first}</span>
      {more > 0 && <span className="shrink-0 rounded-md bg-white/10 px-1.5 py-0.5 text-[11px] font-normal text-slate-300">+{more}</span>}
    </span>
  );
}

/** Findings by level as lettered chips: "C 1  H 2  M 4". The letter carries the level, not only the colour. */
function SeverityChips({ counts, empty }: { counts: Partial<Record<Severity, number>>; empty: string }) {
  const present = SEVERITIES.filter((s) => counts[s]);
  if (present.length === 0) return <span className="text-[13px] text-slate-400">{empty}</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {present.map((s) => (
        <span key={s} title={SEVERITY_LABEL[s]} className={clsx("rounded-md border px-1.5 py-0.5 font-mono text-[11px] font-semibold", SEVERITY_STYLE[s].chip)}>
          {SEVERITY_LETTER[s] ?? "?"} {counts[s]}
        </span>
      ))}
    </span>
  );
}

function Meta({ scan, showOwner }: { scan: Scan; showOwner: boolean }) {
  return (
    <>
      {MODE_META[scan.scan_mode].label} · {scan.runner_name ?? "раннер удалён"}
      {scan.parent_scan_id !== null && ` · из #${scan.parent_scan_id}`}
      {scan.resume_count > 0 && ` · продолжен ×${scan.resume_count}`}
      {showOwner && ` · ${scan.owner_email}`}
    </>
  );
}

const ScheduleChip = ({ scan }: { scan: Scan }) =>
  scan.schedule_id === null ? null : (
    <span className="inline-flex max-w-[12rem] items-center gap-1 rounded-md border border-white/[0.12] px-1.5 text-slate-300">
      <ClockIcon className="h-3 w-3 shrink-0" />
      <span className="truncate">{scan.schedule_name ?? `расписание #${scan.schedule_id}`}</span>
    </span>
  );

/** A scan that is running (or being stopped): how long, what it has found and spent so far. */
function LiveCard({ scan, showOwner, canStop, onStop }: { scan: Scan; showOwner: boolean; canStop: boolean; onStop: () => void }) {
  const stopping = scan.status === "cancelling";
  const elapsed = scanDuration(scan, Date.now());
  const budget = scan.max_budget;
  const spent = scan.cost ?? 0;
  const share = budget ? Math.min(100, Math.round((spent / budget) * 100)) : null;
  return (
    <article className="flex min-w-0 flex-1 basis-[26rem] flex-col gap-3 rounded-2xl border border-accent/40 bg-ink-900/60 px-5 py-4">
      <div className="flex items-center justify-between gap-3 text-xs">
        <span className={clsx("inline-flex items-center gap-2 font-medium", stopping ? "text-signal-warn" : "text-accent-soft")}>
          <span className={clsx("h-2 w-2 rounded-full", stopping ? "bg-signal-warn" : "bg-accent-soft shadow-[0_0_0_4px_rgba(157,134,255,0.22)]")} />
          {STATUS_META[scan.status].label}
          {elapsed !== null && ` · ${formatDuration(elapsed)}`}
        </span>
        <span className="font-mono text-slate-400">#{scan.id}</span>
      </div>
      <div className="min-w-0">
        <h3 className="text-[17px] font-semibold text-white">
          <Targets scan={scan} />
        </h3>
        <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-slate-400">
          <Meta scan={scan} showOwner={showOwner} />
          <ScheduleChip scan={scan} />
        </p>
      </div>
      <div className="flex flex-wrap gap-x-7 gap-y-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px] text-slate-400">Находки на этот момент</span>
          <SeverityChips counts={scan.severity_counts} empty="пока нет" />
        </div>
        <div className="flex min-w-0 flex-1 basis-44 flex-col gap-1.5">
          <span className="text-[11px] text-slate-400">
            Стоимость · {budget ? `${formatCost(spent)} из ${formatCost(budget)}` : "без лимита"}
          </span>
          {share !== null ? (
            <span className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-white/10" title={`${share}% лимита`}>
              <span className={clsx("block h-full rounded-full", share >= 90 ? "bg-signal-bad" : share >= 70 ? "bg-signal-warn" : "bg-accent-soft")} style={{ width: `${Math.max(2, share)}%` }} />
            </span>
          ) : (
            <span className="font-mono text-[13px] font-medium text-white">{formatCost(scan.cost)}</span>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Link to={`/scans/${scan.id}`} className="inline-flex h-10 items-center rounded-xl border border-white/[0.12] bg-white/5 px-4 text-[13px] font-medium text-white transition-colors hover:bg-white/10">
          Открыть ход скана
        </Link>
        {canStop && !stopping && (
          <Button variant="danger" className="h-10 text-[13px]" onClick={onStop}>
            Остановить
          </Button>
        )}
      </div>
    </article>
  );
}

function QueuedRow({ scan, showOwner, canStop, onStop }: { scan: Scan; showOwner: boolean; canStop: boolean; onStop: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 rounded-2xl border border-dashed border-white/[0.16] py-1.5 pl-4 pr-2 text-[13px]">
      <span className="font-mono text-xs text-slate-400">#{scan.id}</span>
      <Link to={`/scans/${scan.id}`} className="min-w-0 max-w-full font-medium text-white hover:text-accent-soft">
        <Targets scan={scan} />
      </Link>
      <span className="text-slate-400">
        <Meta scan={scan} showOwner={showOwner} />
      </span>
      <span className="min-w-0 flex-1 basis-40 text-slate-300">В очереди: ждёт свободного места</span>
      {canStop ? (
        <Button className="h-9 border-transparent bg-transparent text-[13px]" onClick={onStop}>
          Отменить
        </Button>
      ) : (
        <span className="h-9" />
      )}
    </div>
  );
}

const ICON: Record<string, { box: string; icon: JSX.Element }> = {
  vulns_found: { box: "bg-signal-bad/[0.12] text-signal-bad", icon: <AlertIcon /> },
  failed: { box: "bg-signal-bad/[0.12] text-signal-bad", icon: <CloseIcon /> },
  succeeded: { box: "bg-signal-ok/[0.12] text-signal-ok", icon: <CheckIcon /> },
  cancelled: { box: "bg-white/[0.07] text-slate-400", icon: <MinusIcon /> },
};
// A table from `xl` up; below that the columns do not fit beside the menu, so a row wraps into lines instead.
const COLS = "xl:grid xl:grid-cols-[minmax(0,1fr)_11.5rem_6.5rem_5rem_6.5rem]";

function HistoryRow({ scan, showOwner, canContinue }: { scan: Scan; showOwner: boolean; canContinue: boolean }) {
  const mark = ICON[scan.status] ?? ICON.cancelled;
  const duration = scanDuration(scan, Date.now());
  const failed = scan.status === "failed";
  return (
    // The whole row opens the scan (the title link is stretched over it); «Продолжить» sits above that.
    <li className={clsx("relative flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5 transition-colors hover:bg-white/[0.04]", COLS)}>
      <span className="flex w-full min-w-0 items-center gap-3 xl:w-auto">
        <span className={clsx("flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px]", mark.box)} title={STATUS_META[scan.status].label}>
          {mark.icon}
        </span>
        <span className="min-w-0">
          <Link to={`/scans/${scan.id}`} className="block text-sm font-medium text-white after:absolute after:inset-0">
            <Targets scan={scan} />
          </Link>
          <span className="flex flex-wrap items-center gap-1.5 text-xs text-slate-400">
            <span className="font-mono">#{scan.id}</span> · <Meta scan={scan} showOwner={showOwner} />
            <ScheduleChip scan={scan} />
          </span>
          {failed && scan.status_detail && <span className="mt-0.5 line-clamp-1 text-xs text-rose-300 [overflow-wrap:anywhere]">{scan.status_detail}</span>}
        </span>
      </span>
      <span>
        {canContinue ? (
          <Link
            to={`/scans/${scan.id}`}
            state={{ continue: true }}
            className="relative z-10 inline-flex h-9 items-center rounded-[10px] border border-accent/45 bg-accent/[0.14] px-3.5 text-[13px] font-medium text-white transition-colors hover:bg-accent/25"
          >
            Продолжить
          </Link>
        ) : (
          <SeverityChips counts={scan.severity_counts} empty={scan.status === "succeeded" ? "находок нет" : "—"} />
        )}
      </span>
      <span className="text-[13px] text-slate-300">{duration === null ? "—" : formatDuration(duration)}</span>
      <span className="font-mono text-[13px] text-slate-300">{formatCost(scan.cost)}</span>
      <span className="text-xs text-slate-400">{relativeTime(scan.finished_at ?? scan.started_at ?? scan.created_at)}</span>
    </li>
  );
}

const TILE_TONE = { accent: "text-accent-soft", bad: "text-rose-300", ok: "text-signal-ok", muted: "text-slate-400" } as const;

export default function Scans() {
  const { hasRole, user } = useAuth();
  const [params, setParams] = useSearchParams();
  const rawSchedule = params.get("schedule");
  const scheduleId = rawSchedule !== null && /^\d+$/.test(rawSchedule) ? Number(rawSchedule) : null;
  const [filter, setFilter] = useState<ScanFilter>("all");
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const query = useDebounced(search.trim(), 300);
  const [live, setLive] = useState<Scan[] | null>(null);
  const [history, setHistory] = useState<ScanList | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const show = sections(filter);

  const load = useCallback(async () => {
    const mine = ++requestId.current;
    const common = { schedule_id: scheduleId ?? undefined, q: query || undefined };
    const status = filter === "all" ? undefined : filter;
    try {
      const [liveList, historyList] = await Promise.all([
        show.live ? listScans({ ...common, status, active: status ? undefined : true, limit: LIVE_LIMIT }) : null,
        show.history ? listScans({ ...common, status, active: status ? undefined : false, limit: PAGE, offset: page * PAGE }) : null,
      ]);
      if (mine !== requestId.current) return;
      setLive(liveList?.items ?? []);
      setHistory(historyList);
      setCounts((liveList ?? historyList)?.counts ?? {});
      setError(null);
    } catch (e) {
      if (mine === requestId.current) setError(errorMessage(e));
    }
  }, [filter, page, scheduleId, query, show.live, show.history]);

  useEffect(() => {
    setLive(null);
    setHistory(null);
    void load();
  }, [load]);

  // Keep the page fresh while anything on it is still moving.
  const anyActive = (live?.some((s) => isActive(s.status)) ?? false) || (counts ? (counts.running ?? 0) + (counts.queued ?? 0) + (counts.cancelling ?? 0) > 0 : false);
  useEffect(() => {
    if (!anyActive) return;
    const id = setInterval(() => !document.hidden && void load(), 3000);
    return () => clearInterval(id);
  }, [anyActive, load]);

  const canCreate = hasRole("admin", "operator");
  const showOwner = !hasRole("operator");
  const canModify = (scan: Scan) => hasRole("admin") || (hasRole("operator") && scan.user_id === user?.id);
  const stop = async (scan: Scan) => {
    if (!window.confirm(scan.status === "queued" ? `Отменить скан #${scan.id}?` : `Остановить скан #${scan.id}? Уже найденное сохранится.`)) return;
    try {
      await cancelScan(scan.id);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  };
  const pick = (value: ScanFilter) => {
    setFilter(value);
    setPage(0);
  };

  const loading = live === null && history === null && !error;
  const running = live?.filter((s) => s.status !== "queued") ?? [];
  const queued = live?.filter((s) => s.status === "queued") ?? [];
  const rows = history?.items ?? [];
  const pages = history ? Math.max(1, Math.ceil(history.total / PAGE)) : 1;
  const nothing = !loading && running.length + queued.length + rows.length === 0;
  const everything = counts ? tileCount(counts, "all") : 0;

  return (
    <>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-white">Сканы</h1>
          <p className="mt-1 text-sm text-slate-400">{counts ? scansSummary(counts) : "История и текущие запуски"}</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex h-10 w-72 max-w-full items-center gap-2 rounded-xl border border-white/10 bg-ink-900/60 px-3 text-slate-400 focus-within:border-accent/50">
            <SearchIcon />
            <input
              type="search"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(0);
              }}
              placeholder="Найти по цели или номеру"
              aria-label="Найти скан по цели или номеру"
              className="min-w-0 flex-1 bg-transparent text-sm text-slate-100 outline-none placeholder:text-slate-500"
            />
          </label>
          {canCreate && (
            <Link to="/scans/new">
              <Button variant="primary">
                <PlusIcon />
                Новый скан
              </Button>
            </Link>
          )}
        </div>
      </div>

      {scheduleId !== null && (
        <div className="mb-4 flex items-center gap-3 text-sm text-slate-400" data-testid="schedule-filter">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-xs text-accent-soft">
            <ClockIcon className="h-3 w-3" />
            Сканы расписания #{scheduleId}
          </span>
          <button onClick={() => { setParams({}); setPage(0); }} className="text-xs text-slate-400 transition hover:text-white">
            Показать все
          </button>
        </div>
      )}

      <div className="mb-6 flex flex-wrap gap-2.5">
        {TILES.map((t) => (
          <button
            key={t.value}
            type="button"
            aria-pressed={filter === t.value}
            onClick={() => pick(t.value)}
            className={clsx(
              "flex min-h-[4.5rem] flex-1 basis-32 flex-col justify-between rounded-2xl border px-3.5 py-2.5 text-left transition-colors",
              filter === t.value ? "border-accent/50 bg-accent/[0.14]" : "border-white/[0.08] bg-ink-900/60 hover:bg-white/[0.04]"
            )}
          >
            <span className={clsx("text-xs", filter === t.value ? "text-white" : TILE_TONE[t.tone])}>{t.label}</span>
            <span className="text-[22px] font-semibold leading-tight text-white">{counts ? tileCount(counts, t.value) : "—"}</span>
          </button>
        ))}
      </div>

      <ErrorNote message={error} />
      {loading && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {nothing && !error && (
        <EmptyState
          title={query ? "Ничего не найдено" : everything === 0 ? "Сканов пока нет" : `Нет сканов: «${TILES.find((t) => t.value === filter)?.label}»`}
          text={query ? "Проверьте запрос: ищем по части адреса цели или по номеру скана." : everything === 0 ? "Запустите первый скан — для отладки панели выберите раннер mock: он ничего не тратит." : "Выберите другую плитку, чтобы увидеть остальные."}
          action={everything === 0 && !query && canCreate ? <Link to="/scans/new"><Button variant="primary">Новый скан</Button></Link> : undefined}
        />
      )}

      {running.length + queued.length > 0 && (
        <section className="mb-6 flex flex-col gap-3">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Сейчас в работе</h2>
          {running.length > 0 && (
            <div className="flex flex-wrap gap-4">
              {running.map((s) => <LiveCard key={s.id} scan={s} showOwner={showOwner} canStop={canModify(s)} onStop={() => void stop(s)} />)}
            </div>
          )}
          {queued.map((s) => <QueuedRow key={s.id} scan={s} showOwner={showOwner} canStop={canModify(s)} onStop={() => void stop(s)} />)}
        </section>
      )}

      {rows.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">История</h2>
          <Card className="overflow-hidden">
            <div className={clsx("hidden gap-x-4 border-b border-white/5 px-5 py-2.5 text-[11px] uppercase tracking-wider text-slate-500", COLS)}>
              <span>Скан</span><span>Находки</span><span>Длительность</span><span>Стоимость</span><span>Когда</span>
            </div>
            <ul className="divide-y divide-white/5">
              {rows.map((s) => <HistoryRow key={s.id} scan={s} showOwner={showOwner} canContinue={(s.status === "failed" || s.status === "cancelled") && canModify(s) && continueKind(s) !== null} />)}
            </ul>
          </Card>
          {history && history.total > PAGE && (
            <div className="flex flex-wrap items-center justify-between gap-3 text-[13px] text-slate-400">
              <span>{pageRange(page, PAGE, history.total)}</span>
              <span className="flex gap-2">
                <Button className="h-10 w-10 !p-0" aria-label="Предыдущая страница" disabled={page === 0} onClick={() => setPage((p) => p - 1)}><ChevronLeftIcon /></Button>
                <Button className="h-10 w-10 !p-0" aria-label="Следующая страница" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}><ChevronRightIcon /></Button>
              </span>
            </div>
          )}
        </section>
      )}
    </>
  );
}
