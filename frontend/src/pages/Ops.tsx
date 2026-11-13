import { motion } from "framer-motion";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner, rise, stagger } from "../components/ui";
import {
  cancelOpsRun,
  createOpsRun,
  errorMessage,
  listOpsRuns,
  listOpsTargetRunners,
  listProfiles,
  type LLMProfile,
  type OpsRun,
} from "../lib/api";
import { canCancel, canSubmit, fleetSummary, hostStatusLabel, hostStatusTone, runStatusLabel, runStatusTone } from "../lib/ops";

interface Form {
  name: string;
  goal: string;
  llmProfileId: number | null;
  runnerIds: number[];
}
const emptyForm: Form = { name: "", goal: "", llmProfileId: null, runnerIds: [] };

function HostRow({ host }: { host: OpsRun["hosts"][number] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-white/5 bg-ink-900/40 p-3">
      <button type="button" className="flex w-full items-center justify-between gap-3 text-left" onClick={() => setOpen((o) => !o)}>
        <span className="truncate font-mono text-xs text-slate-300">{host.runner_name}</span>
        <span className="flex shrink-0 items-center gap-2">
          {host.exit_code !== null && <span className="font-mono text-[11px] text-slate-500">код {host.exit_code}</span>}
          <Badge tone={hostStatusTone(host.status)}>{hostStatusLabel(host.status)}</Badge>
        </span>
      </button>
      {open && host.output && (
        <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
          {host.output}
        </pre>
      )}
      {open && !host.output && <p className="mt-2 text-[11px] text-slate-600">Пока нет вывода.</p>}
    </div>
  );
}

function RunCard({ run, onCancel, cancelling }: { run: OpsRun; onCancel: () => void; cancelling: boolean }) {
  const [scriptOpen, setScriptOpen] = useState(false);
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-white">{run.name}</h3>
          <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">{run.goal}</p>
        </div>
        <Badge tone={runStatusTone(run.status)}>{runStatusLabel(run.status)}</Badge>
      </div>

      <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-slate-500">
        <div>профиль: <span className="text-slate-300">{run.llm_profile_name ?? "—"}</span></div>
        <div>кто запустил: <span className="text-slate-300">{run.owner_email}</span></div>
        <div>хосты: <span className="text-slate-300">{fleetSummary(run.hosts)}</span></div>
      </dl>

      {run.script_error && <p className="mt-2 text-xs text-signal-bad">Скрипт не создан: {run.script_error}</p>}
      {run.blocked_reason && (
        <p className="mt-2 text-xs text-signal-bad">Выполнение остановлено защитой: {run.blocked_reason}</p>
      )}

      {run.script && (
        <div className="mt-3">
          <button type="button" className="text-[11px] text-slate-500 underline decoration-dotted hover:text-slate-300" onClick={() => setScriptOpen((o) => !o)}>
            {scriptOpen ? "Скрыть скрипт" : "Показать скрипт"}
          </button>
          {scriptOpen && (
            <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
              {run.script}
            </pre>
          )}
        </div>
      )}

      {run.hosts.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {run.hosts.map((h) => (
            <HostRow key={h.id} host={h} />
          ))}
        </div>
      )}

      {canCancel(run) && (
        <div className="mt-4">
          <Button variant="danger" loading={cancelling} onClick={onCancel}>
            Отменить оставшиеся хосты
          </Button>
        </div>
      )}
    </Card>
  );
}

export default function Ops() {
  const [runs, setRuns] = useState<OpsRun[] | null>(null);
  const [profiles, setProfiles] = useState<LLMProfile[]>([]);
  const [runners, setRunners] = useState<{ id: number; name: string }[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<Form>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState<number | null>(null);

  const reload = useCallback(async () => {
    try {
      const [r, p, rn] = await Promise.all([listOpsRuns(), listProfiles(), listOpsTargetRunners()]);
      setRuns(r);
      setProfiles(p);
      setRunners(rn);
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const openCreate = () => {
    setForm(emptyForm);
    setFormError(null);
    setOpen(true);
  };

  const toggleRunner = (id: number) => {
    setForm((f) => ({
      ...f,
      runnerIds: f.runnerIds.includes(id) ? f.runnerIds.filter((x) => x !== id) : [...f.runnerIds, id],
    }));
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setBusy(true);
    try {
      await createOpsRun({
        name: form.name.trim(),
        goal: form.goal,
        llm_profile_id: form.llmProfileId as number,
        runner_ids: form.runnerIds,
      });
      setOpen(false);
      await reload();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (run: OpsRun) => {
    setCancelling(run.id);
    try {
      await cancelOpsRun(run.id);
      await reload();
    } catch (e) {
      setLoadError(errorMessage(e));
    } finally {
      setCancelling(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Операции"
        subtitle="Опишите задачу словами — модель напишет shell-скрипт и он сразу выполнится как root по SSH на выбранных раннерах. Подтверждения нет: скрипт всегда виден здесь целиком."
        action={<Button variant="primary" onClick={openCreate}>+ Новая задача</Button>}
      />
      <ErrorNote message={loadError} />

      {runs === null && !loadError && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {runs?.length === 0 && (
        <EmptyState
          title="Задач пока нет"
          text="Опишите, что сделать на серверах — например «обнови пакеты и перезапусти nginx»."
          action={<Button variant="primary" onClick={openCreate}>Новая задача</Button>}
        />
      )}

      {runs && runs.length > 0 && (
        <motion.div variants={stagger} initial="hidden" animate="show" className="space-y-4">
          {runs.map((r) => (
            <motion.div key={r.id} variants={rise}>
              <RunCard run={r} onCancel={() => cancel(r)} cancelling={cancelling === r.id} />
            </motion.div>
          ))}
        </motion.div>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title="Новая операционная задача">
        <form onSubmit={submit} className="space-y-4">
          <Field label="Название (необязательно)" hint="Если оставить пустым, возьмётся начало формулировки задачи.">
            <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={200} placeholder="Обновление пакетов" />
          </Field>

          <Field label="Что сделать" hint="Опишите задачу своими словами — модель переведёт это в shell-скрипт и запустит на выбранных хостах как root.">
            <textarea
              className="field h-24 resize-none"
              value={form.goal}
              onChange={(e) => setForm({ ...form, goal: e.target.value })}
              placeholder="обнови пакеты и перезапусти nginx"
              required
              maxLength={10000}
            />
          </Field>

          <Field label="LLM-профиль">
            <select
              className="field"
              value={form.llmProfileId ?? ""}
              onChange={(e) => setForm({ ...form, llmProfileId: e.target.value ? Number(e.target.value) : null })}
              required
            >
              <option value="" disabled>
                Выберите профиль…
              </option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            {profiles.length === 0 && <p className="mt-1.5 text-[11px] text-signal-warn">Нет своих LLM-профилей — добавьте его на странице «LLM-профили».</p>}
          </Field>

          <div>
            <span className="mb-1.5 block text-xs font-medium text-slate-400">Раннеры (SSH)</span>
            {runners.length === 0 ? (
              <p className="text-[11px] text-signal-warn">Нет готовых SSH-раннеров — подтвердите ключ хоста на странице «Раннеры».</p>
            ) : (
              <div className="max-h-40 space-y-1 overflow-auto rounded-xl bg-ink-800/80 p-2">
                {runners.map((r) => (
                  <label key={r.id} className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-slate-300 hover:bg-white/5">
                    <input type="checkbox" className="accent-[#7c5cff]" checked={form.runnerIds.includes(r.id)} onChange={() => toggleRunner(r.id)} />
                    {r.name}
                  </label>
                ))}
              </div>
            )}
          </div>

          <ErrorNote message={formError} />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setOpen(false)}>
              Отмена
            </Button>
            <Button
              type="submit"
              variant="primary"
              loading={busy}
              disabled={!canSubmit({ goal: form.goal, llmProfileId: form.llmProfileId, runnerIds: form.runnerIds })}
            >
              Выполнить сразу
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
