import clsx from "clsx";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge, Button, Card, ErrorNote, PageHeader, Spinner } from "../components/ui";
import { errorMessage, getAppSettings, updateAppSettings, type AppSettingItem, type AppSettingsGroup } from "../lib/api";
import { describeDefault, planSave, restartNote, toDraft, type Draft } from "../lib/settingsForm";

const inputClass =
  "w-full rounded-xl border bg-ink-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/50 disabled:opacity-50";

function Row({
  item,
  draft,
  error,
  reset,
  onChange,
  onReset,
}: {
  item: AppSettingItem;
  draft: string | boolean | undefined;
  error: string | undefined;
  reset: boolean;
  onChange: (v: string | boolean) => void;
  onReset: () => void;
}) {
  const note = restartNote(item);
  const shown = reset ? (item.kind === "bool" ? Boolean(item.default) : String(item.default)) : draft;
  const canReset = (item.changed && !reset) || (!reset && String(draft) !== String(item.default) && item.value !== item.default);
  return (
    <div className="grid gap-3 border-b border-white/5 px-5 py-4 last:border-0 md:grid-cols-[1fr_16rem]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-slate-100">{item.label}</span>
          {item.changed && !reset && <Badge tone="accent">изменено</Badge>}
          {reset && <Badge tone="warn">будет сброшено</Badge>}
          {item.restart && <Badge tone="muted">нужен перезапуск</Badge>}
        </div>
        {item.hint && <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{item.hint}</p>}
        <p className="mt-1 font-mono text-[10px] text-slate-600">
          {item.env} · по умолчанию: {describeDefault(item)}
        </p>
        {note && <p className="mt-1 text-[11px] text-signal-warn">{note}</p>}
      </div>
      <div>
        <div className="flex items-center gap-2">
          {item.kind === "bool" ? (
            <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={shown === true} disabled={reset} onChange={(e) => onChange(e.target.checked)} className="h-4 w-4 accent-accent" />
              {shown === true ? "включено" : "выключено"}
            </label>
          ) : item.kind === "choice" ? (
            <select value={String(shown)} disabled={reset} onChange={(e) => onChange(e.target.value)} className={clsx(inputClass, error ? "border-signal-bad/60" : "border-white/10")}>
              {item.choices.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          ) : (
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <input
                value={String(shown ?? "")}
                disabled={reset}
                inputMode={item.kind === "text" ? "text" : "decimal"}
                onChange={(e) => onChange(e.target.value)}
                className={clsx(inputClass, "font-mono", error ? "border-signal-bad/60" : "border-white/10")}
              />
              {item.unit && <span className="shrink-0 text-[11px] text-slate-500">{item.unit}</span>}
            </div>
          )}
          {(item.changed || canReset) && (
            <button type="button" title="Вернуть значение по умолчанию (из .env)" className="shrink-0 text-[11px] text-slate-500 underline decoration-dotted hover:text-slate-300" onClick={onReset}>
              {reset ? "отмена" : "сбросить"}
            </button>
          )}
        </div>
        {error && <p className="mt-1 text-[11px] text-signal-bad">{error}</p>}
      </div>
    </div>
  );
}

export default function Settings() {
  const [groups, setGroups] = useState<AppSettingsGroup[] | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [resets, setResets] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const g = await getAppSettings();
      setGroups(g);
      setDraft(toDraft(g));
      setResets(new Set());
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const plan = useMemo(() => (groups ? planSave(groups, draft, resets) : { changes: {}, errors: {} }), [groups, draft, resets]);
  const pending = Object.keys(plan.changes).length;
  const invalid = Object.keys(plan.errors).length;

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const g = await updateAppSettings(plan.changes);
      setGroups(g);
      setDraft(toDraft(g));
      setResets(new Set());
      const needsRestart = g.flatMap((x) => x.items).filter((i) => i.restart && i.key in plan.changes);
      setSaved(
        needsRestart.length
          ? `Сохранено. Применено без перезапуска, кроме: ${needsRestart.map((i) => i.label).join(", ")}.`
          : "Сохранено и применено — перезапуск не нужен (воркеры подхватят за несколько секунд)."
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const toggleReset = (key: string) =>
    setResets((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <div className="pb-28">
      <PageHeader
        title="Настройки"
        subtitle="Рабочие параметры панели. Значение из .env — это значение по умолчанию; всё, что изменено здесь, главнее и действует без перезапуска"
      />
      <ErrorNote message={error} />
      {saved && <p className="mb-4 text-sm text-signal-ok">{saved}</p>}
      {groups === null ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {groups.map((group) => (
            <Card key={group.name}>
              <h2 className="border-b border-white/5 px-5 py-3 text-xs font-semibold uppercase tracking-wider text-slate-400">{group.name}</h2>
              {group.items.map((item) => (
                <Row
                  key={item.key}
                  item={item}
                  draft={draft[item.key]}
                  error={plan.errors[item.key]}
                  reset={resets.has(item.key)}
                  onChange={(v) => setDraft((d) => ({ ...d, [item.key]: v }))}
                  onReset={() => toggleReset(item.key)}
                />
              ))}
            </Card>
          ))}
          <p className="text-[11px] leading-relaxed text-slate-600">
            В .env остаются секреты и то, без чего панель не стартует: JWT_SECRET, SECRET_ENCRYPTION_KEY, доступ к Postgres и Redis, ADMIN_*, CORS,
            AGENT_TOKEN и AGENT_URL, а также число процессов воркеров (SCAN_WORKER_SLOTS — потолок для «Сканов одновременно», AGENT_CONCURRENCY).
          </p>
        </div>
      )}

      {groups && (pending > 0 || invalid > 0) && (
        <div className="fixed bottom-0 left-0 right-0 z-20 border-t border-white/10 bg-ink-950/90 px-6 py-3 backdrop-blur lg:left-64">
          <div className="mx-auto flex max-w-6xl items-center justify-between gap-4">
            <span className="text-sm text-slate-300">
              {invalid > 0 ? <span className="text-signal-bad">Исправьте поля с ошибками: {invalid}</span> : `Изменений: ${pending}`}
            </span>
            <div className="flex gap-2">
              <Button onClick={() => void load()} disabled={saving}>
                Отменить
              </Button>
              <Button variant="primary" loading={saving} disabled={invalid > 0 || pending === 0} onClick={() => void save()}>
                Сохранить
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
