import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { MODE_META } from "../components/scan";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner, Switch, rise, stagger } from "../components/ui";
import {
  createSchedule,
  deleteSchedule,
  errorMessage,
  listProfiles,
  listRunners,
  listSchedules,
  previewSchedule,
  runSchedule,
  updateSchedule,
  type LLMProfile,
  type Runner,
  type ScanMode,
  type Schedule,
  type SchedulePreview,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { CRON_PRESETS, browserZone, budgetRequired, describeCron, formatWhen, normalizeCronInput, relativeIn, zoneOptions } from "../lib/schedule";
import { parseTargets } from "../lib/targets";

const MODES: ScanMode[] = ["quick", "standard", "deep"];
const KIND_LABEL = { mock: "симуляция", local: "этот сервер", ssh: "SSH" } as const;
const MIN_REAL_INTERVAL = 15;

const RESULT_META = {
  started: { label: "Запущен", tone: "ok" },
  skipped: { label: "Пропущен", tone: "warn" },
  error: { label: "Ошибка", tone: "bad" },
} as const;

// ── Form ─────────────────────────────────────────────────────────────────

function ScheduleForm({
  schedule,
  runners,
  profiles,
  onSaved,
  onCancel,
}: {
  schedule: Schedule | null;
  runners: Runner[];
  profiles: LLMProfile[];
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [name, setName] = useState(schedule?.name ?? "");
  const [targetsText, setTargetsText] = useState(schedule?.targets.join("\n") ?? "");
  const [mode, setMode] = useState<ScanMode>(schedule?.scan_mode ?? "quick");
  const [cron, setCron] = useState(schedule?.cron ?? "0 3 * * *");
  const [zone, setZone] = useState(schedule?.timezone ?? browserZone());
  const [runnerId, setRunnerId] = useState<number | null>(
    schedule ? schedule.runner_id : (runners.find((r) => r.status === "online" && r.kind !== "ssh") ?? runners.find((r) => r.kind !== "ssh"))?.id ?? null
  );
  const [profileId, setProfileId] = useState<number | null>(schedule ? schedule.llm_profile_id : profiles[0]?.id ?? null);
  const [budget, setBudget] = useState(schedule?.max_budget ? String(schedule.max_budget) : "");
  const [instruction, setInstruction] = useState(schedule?.instruction ?? "");
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<SchedulePreview | null>(null);

  const runner = runners.find((r) => r.id === runnerId) ?? null;
  const needsProfile = runner !== null && runner.kind !== "mock";
  const targets = useMemo(() => parseTargets(targetsText), [targetsText]);
  const budgetValue = budget.trim() === "" ? null : Number(budget);
  const budgetInvalid = budgetValue !== null && !(budgetValue > 0);
  const budgetMissing = budgetRequired(runner?.kind) && budgetValue === null;
  const cronNormalized = normalizeCronInput(cron);

  // Live preview of the next firings, debounced; stale answers are dropped.
  useEffect(() => {
    if (!cronNormalized) {
      setPreview(null);
      return;
    }
    let alive = true;
    const timer = setTimeout(() => {
      previewSchedule(cronNormalized, zone, 5)
        .then((p) => alive && setPreview(p))
        .catch((e) => alive && setPreview({ valid: false, error: errorMessage(e), next_runs: [], min_interval_minutes: null }));
    }, 300);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [cronNormalized, zone]);

  const tooFrequent =
    budgetRequired(runner?.kind) && preview?.valid === true && preview.min_interval_minutes !== null && preview.min_interval_minutes < MIN_REAL_INTERVAL;

  const canSubmit =
    name.trim() !== "" &&
    targets.length > 0 &&
    runner !== null &&
    (!needsProfile || profileId !== null) &&
    !budgetInvalid &&
    !budgetMissing &&
    preview?.valid === true &&
    !tooFrequent &&
    !busy;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit || !runner) return;
    setError(null);
    setBusy(true);
    const body = {
      name: name.trim(),
      cron: cronNormalized,
      timezone: zone,
      enabled,
      targets,
      scan_mode: mode,
      instruction: instruction.trim() || null,
      max_budget: budgetValue,
      runner_id: runner.id,
      llm_profile_id: needsProfile || profileId !== null ? profileId : null,
    };
    try {
      if (schedule) await updateSchedule(schedule.id, body);
      else await createSchedule(body);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-5">
      {schedule && schedule.runner_id === null && (
        <p className="rounded-lg border border-signal-warn/25 bg-signal-warn/10 px-3 py-2 text-xs text-signal-warn">
          Раннер этого расписания удалён — выберите другой, иначе расписание не запустится.
        </p>
      )}

      <Field label="Название">
        <input className="field" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} placeholder="Ночной скан стенда" required autoFocus />
      </Field>

      <Field
        label={`Цели${targets.length ? ` (${targets.length})` : ""}`}
        hint={isAdmin ? "URL, домен, IP, git-репозиторий или локальный путь (только администратор). По одной на строку." : "URL, домен, IP или git-репозиторий. По одной на строку."}
      >
        <textarea className="field h-24 resize-y font-mono text-xs" value={targetsText} onChange={(e) => setTargetsText(e.target.value)} placeholder={"https://app.example.com\napi.example.com"} spellCheck={false} required />
      </Field>

      <div>
        <span className="mb-1.5 block text-xs font-medium text-slate-400">Когда запускать</span>
        <div className="mb-2 flex flex-wrap gap-2">
          {CRON_PRESETS.map((p) => (
            <button
              type="button"
              key={p.cron}
              onClick={() => setCron(p.cron)}
              className={clsx(
                "rounded-full border px-3 py-1 text-xs transition-colors",
                cronNormalized === p.cron ? "border-accent/40 bg-accent/15 text-white" : "border-white/10 bg-white/5 text-slate-400 hover:text-slate-200"
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_14rem]">
          <Field label="Cron (минута час день месяц день_недели)">
            <input className="field font-mono" value={cron} onChange={(e) => setCron(e.target.value)} spellCheck={false} aria-label="Cron-выражение" aria-invalid={preview?.valid === false} required />
          </Field>
          <Field label="Часовой пояс">
            <select className="field" value={zone} onChange={(e) => setZone(e.target.value)} aria-label="Часовой пояс">
              {zoneOptions(zone).map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </Field>
        </div>

        <div className="mt-3 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3" data-testid="cron-preview">
          {preview === null ? (
            <p className="text-xs text-slate-600">Проверяю…</p>
          ) : !preview.valid ? (
            <p className="text-xs text-signal-bad">{preview.error ?? "Некорректное выражение"}</p>
          ) : (
            <>
              <p className="mb-1.5 text-[11px] text-slate-600"><span className="uppercase tracking-wider">Ближайшие запуски</span> · {zone}</p>
              <ul className="space-y-0.5 font-mono text-xs text-slate-300">
                {preview.next_runs.map((t) => <li key={t}>{formatWhen(t, zone)}</li>)}
              </ul>
            </>
          )}
        </div>
        {tooFrequent && (
          <p className="mt-2 text-xs text-signal-bad">Для настоящего раннера интервал между запусками — не меньше {MIN_REAL_INTERVAL} минут.</p>
        )}
      </div>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Раннер">
          <select className="field" value={runnerId ?? ""} onChange={(e) => setRunnerId(e.target.value ? Number(e.target.value) : null)} required aria-label="Раннер">
            <option value="" disabled>— выберите —</option>
            {runners.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name} · {KIND_LABEL[r.kind]}{r.status !== "online" ? ` · ${r.status}` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label={needsProfile ? "LLM-профиль" : "LLM-профиль (для mock не нужен)"}>
          <select className="field" value={profileId ?? ""} onChange={(e) => setProfileId(e.target.value ? Number(e.target.value) : null)} required={needsProfile} aria-label="LLM-профиль">
            {!needsProfile && <option value="">— не использовать —</option>}
            {profiles.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.strix_model}</option>)}
          </select>
        </Field>
      </div>

      <div>
        <span className="mb-1.5 block text-xs font-medium text-slate-400">Режим</span>
        <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="Режим скана">
          {MODES.map((m) => (
            <button
              type="button"
              role="radio"
              aria-checked={mode === m}
              key={m}
              onClick={() => setMode(m)}
              className={clsx("rounded-xl border px-3 py-2 text-left transition-all", mode === m ? "border-accent/50 bg-accent/15" : "border-white/10 bg-white/5 hover:border-white/20")}
            >
              <span className={clsx("block text-sm font-medium", mode === m ? "text-white" : "text-slate-300")}>{MODE_META[m].label}</span>
              <span className="mt-0.5 block text-[11px] text-slate-500">{m}</span>
            </button>
          ))}
        </div>
      </div>

      <Field
        label={budgetRequired(runner?.kind) ? "Лимит бюджета на один запуск, $ (обязателен)" : "Лимит бюджета на один запуск, $ (необязательно)"}
        hint="Расписание тратит деньги без вашего участия, поэтому для настоящего раннера лимит обязателен: Strix остановит каждый запуск при его достижении."
      >
        <input className="field font-mono" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="5" aria-label="Лимит бюджета" aria-invalid={budgetInvalid || budgetMissing} />
      </Field>
      {budgetInvalid && <p className="-mt-3 text-xs text-signal-bad">Укажите положительное число.</p>}

      <Field label="Инструкция (необязательно)">
        <textarea className="field h-20 resize-y text-sm" value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={10000} />
      </Field>

      <div className="flex items-center justify-between rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
        <div>
          <div className="text-sm text-slate-200">Расписание включено</div>
          <div className="text-[11px] text-slate-500">Выключенное расписание не запускается, но остаётся в списке.</div>
        </div>
        <Switch checked={enabled} onChange={setEnabled} label="Расписание включено" />
      </div>

      <ErrorNote message={error} />
      <div className="flex justify-end gap-3">
        <Button type="button" onClick={onCancel}>Отмена</Button>
        <Button type="submit" variant="primary" loading={busy} disabled={!canSubmit}>{schedule ? "Сохранить" : "Создать расписание"}</Button>
      </div>
    </form>
  );
}

// ── Card ─────────────────────────────────────────────────────────────────

function ScheduleCard({
  schedule,
  showOwner,
  busy,
  onToggle,
  onRun,
  onEdit,
  onDelete,
}: {
  schedule: Schedule;
  showOwner: boolean;
  busy: boolean;
  onToggle: (next: boolean) => void;
  onRun: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const label = describeCron(schedule.cron);
  const result = schedule.last_result ? RESULT_META[schedule.last_result] : null;
  return (
    <motion.li variants={rise} layout data-testid={`schedule-${schedule.id}`}>
      <Card className={clsx("p-5 transition-opacity", !schedule.enabled && "opacity-80")}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="truncate text-base font-semibold text-white">{schedule.name}</h2>
              {schedule.enabled ? <Badge tone="ok">включено</Badge> : <Badge tone="muted">выключено</Badge>}
              <Badge tone="accent">{MODE_META[schedule.scan_mode].label}</Badge>
            </div>
            <p className="mt-1.5 text-xs text-slate-500">
              <span className="font-mono text-slate-300">{schedule.cron}</span>
              {label && <span> · {label}</span>}
              <span> · {schedule.timezone}</span>
              {showOwner && <span> · {schedule.owner_email}</span>}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Switch checked={schedule.enabled} onChange={onToggle} disabled={busy} label={`Включить расписание ${schedule.name}`} />
          </div>
        </div>

        <ul className="mt-4 flex flex-wrap gap-2">
          {schedule.targets.map((t) => (
            <li key={t} className="max-w-full truncate rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 font-mono text-xs text-slate-300">{t}</li>
          ))}
        </ul>

        {schedule.disabled_reason && (
          <p className="mt-4 rounded-lg border border-signal-warn/25 bg-signal-warn/10 px-3.5 py-2.5 text-xs leading-relaxed text-signal-warn [overflow-wrap:anywhere]">{schedule.disabled_reason}</p>
        )}

        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-4">
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-600">Следующий запуск</dt>
            <dd className="mt-1 text-slate-200" data-testid="next-run">
              {schedule.enabled && schedule.next_run_at ? (
                <>
                  {formatWhen(schedule.next_run_at, schedule.timezone)}
                  <span className="ml-2 text-slate-500">{relativeIn(schedule.next_run_at)}</span>
                </>
              ) : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-600">Последний запуск</dt>
            <dd className="mt-1 text-slate-200">
              {schedule.last_run_at ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  {formatWhen(schedule.last_run_at, schedule.timezone)}
                  {result && <Badge tone={result.tone}>{result.label}</Badge>}
                </span>
              ) : "ещё не запускалось"}
            </dd>
            {schedule.last_scan_id !== null && (
              <dd className="mt-0.5"><Link to={`/scans/${schedule.last_scan_id}`} className="text-accent-soft hover:underline">скан #{schedule.last_scan_id}</Link></dd>
            )}
            {schedule.last_detail && schedule.last_result !== "started" && (
              <dd className="mt-0.5 text-slate-500 [overflow-wrap:anywhere]">{schedule.last_detail}</dd>
            )}
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-600">Раннер</dt>
            <dd className="mt-1 text-slate-200">{schedule.runner_name ?? <span className="text-signal-warn">удалён</span>}</dd>
            <dd className="mt-0.5 text-slate-500">{schedule.llm_profile_name ?? "без LLM-профиля"}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-600">Бюджет запуска</dt>
            <dd className="mt-1 text-slate-200">{schedule.max_budget ? `$${schedule.max_budget}` : "не задан"}</dd>
          </div>
        </dl>

        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-white/5 pt-4">
          <Button onClick={onRun} disabled={busy || schedule.runner_id === null} className="!px-3 !py-1.5 text-xs" title="Запустить сейчас, не дожидаясь расписания">Запустить сейчас</Button>
          <Link to={`/scans?schedule=${schedule.id}`}><Button className="!px-3 !py-1.5 text-xs">Сканы расписания</Button></Link>
          <Button onClick={onEdit} disabled={busy} className="!px-3 !py-1.5 text-xs">Изменить</Button>
          <Button variant="danger" onClick={onDelete} disabled={busy} className="!px-3 !py-1.5 text-xs">Удалить</Button>
        </div>
      </Card>
    </motion.li>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────

export default function Schedules() {
  const navigate = useNavigate();
  const { hasRole } = useAuth();
  const showOwner = hasRole("admin");

  const [items, setItems] = useState<Schedule[] | null>(null);
  const [runners, setRunners] = useState<Runner[]>([]);
  const [profiles, setProfiles] = useState<LLMProfile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await listSchedules());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
    listRunners().then(setRunners).catch(() => undefined);
    listProfiles().then(setProfiles).catch(() => undefined);
  }, [load]);

  // "In 5 minutes" and the next-run time drift; refresh now and then.
  useEffect(() => {
    const id = setInterval(() => !document.hidden && void load(), 15000);
    return () => clearInterval(id);
  }, [load]);

  const act = async (id: number, fn: () => Promise<unknown>) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  };

  const onToggle = (s: Schedule, next: boolean) => act(s.id, async () => { await updateSchedule(s.id, { enabled: next }); await load(); });
  const onRun = (s: Schedule) => {
    const paid = s.runner_kind !== null && s.runner_kind !== "mock";
    const text = paid ? `Запустить «${s.name}» сейчас? Скан использует LLM и потратит токены.` : `Запустить «${s.name}» сейчас?`;
    if (!window.confirm(text)) return;
    void act(s.id, async () => {
      const { scan_id } = await runSchedule(s.id);
      navigate(`/scans/${scan_id}`);
    });
  };
  const onDelete = (s: Schedule) => {
    if (!window.confirm(`Удалить расписание «${s.name}»? Уже запущенные им сканы останутся.`)) return;
    void act(s.id, async () => { await deleteSchedule(s.id); await load(); });
  };

  return (
    <>
      <PageHeader
        title="Расписания"
        subtitle="Повторяющиеся сканы по cron. Каждый запуск — обычный скан со своим логом и стоимостью."
        action={<Button variant="primary" onClick={() => setEditing("new")}>+ Новое расписание</Button>}
      />

      <ErrorNote message={error} />
      {items === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {items && items.length === 0 && (
        <EmptyState
          title="Расписаний пока нет"
          text="Например, ночной быстрый скан стенда. Для отладки возьмите раннер mock — он ничего не тратит."
          action={<Button variant="primary" onClick={() => setEditing("new")}>Создать расписание</Button>}
        />
      )}

      {items && items.length > 0 && (
        <motion.ul variants={stagger} initial="hidden" animate="show" className="space-y-4">
          <AnimatePresence initial={false}>
            {items.map((s) => (
              <ScheduleCard
                key={s.id}
                schedule={s}
                showOwner={showOwner}
                busy={busyId === s.id}
                onToggle={(next) => onToggle(s, next)}
                onRun={() => onRun(s)}
                onEdit={() => setEditing(s)}
                onDelete={() => onDelete(s)}
              />
            ))}
          </AnimatePresence>
        </motion.ul>
      )}

      <Modal open={editing !== null} onClose={() => setEditing(null)} title={editing === "new" ? "Новое расписание" : "Изменить расписание"} wide>
        {editing !== null && (
          <ScheduleForm
            key={editing === "new" ? "new" : editing.id}
            schedule={editing === "new" ? null : editing}
            runners={runners}
            profiles={profiles}
            onCancel={() => setEditing(null)}
            onSaved={() => { setEditing(null); void load(); }}
          />
        )}
      </Modal>
    </>
  );
}
