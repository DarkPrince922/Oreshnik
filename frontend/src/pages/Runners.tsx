import { motion } from "framer-motion";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner, StatusDot, relativeTime, rise, stagger } from "../components/ui";
import {
  checkRunner,
  createRunner,
  deleteRunner,
  errorMessage,
  listRunners,
  updateRunner,
  type Runner,
  type RunnerKind,
  type RunnerStatus,
} from "../lib/api";
import { useAuth } from "../lib/auth";

const KIND_LABEL: Record<RunnerKind, string> = { mock: "Mock", local: "Локальный", ssh: "SSH" };
const STATUS: Record<RunnerStatus, { label: string; tone: "ok" | "warn" | "bad" | "muted" }> = {
  online: { label: "online", tone: "ok" },
  offline: { label: "offline", tone: "muted" },
  provisioning: { label: "установка", tone: "warn" },
  error: { label: "ошибка", tone: "bad" },
};

interface Form {
  name: string;
  kind: RunnerKind;
  ssh_host: string;
  ssh_port: string;
  ssh_user: string;
  ssh_private_key: string;
}
const emptyForm: Form = { name: "", kind: "mock", ssh_host: "", ssh_port: "22", ssh_user: "", ssh_private_key: "" };

export default function Runners() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [runners, setRunners] = useState<Runner[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Runner | null>(null);
  const [form, setForm] = useState<Form>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setRunners(await listRunners());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setFormError(null);
    setOpen(true);
  };
  const openEdit = (r: Runner) => {
    setEditing(r);
    setForm({ name: r.name, kind: r.kind, ssh_host: r.ssh_host ?? "", ssh_port: String(r.ssh_port ?? 22), ssh_user: r.ssh_user ?? "", ssh_private_key: "" });
    setFormError(null);
    setOpen(true);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setBusy(true);
    try {
      const ssh = form.kind === "ssh";
      const port = Number(form.ssh_port) || 22;
      if (editing) {
        await updateRunner(editing.id, {
          name: form.name,
          ...(ssh
            ? { ssh_host: form.ssh_host, ssh_port: port, ssh_user: form.ssh_user, ...(form.ssh_private_key.trim() ? { ssh_private_key: form.ssh_private_key } : {}) }
            : {}),
        });
      } else {
        await createRunner({
          name: form.name,
          kind: form.kind,
          ...(ssh ? { ssh_host: form.ssh_host, ssh_port: port, ssh_user: form.ssh_user, ssh_private_key: form.ssh_private_key } : {}),
        });
      }
      setOpen(false);
      await reload();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const check = async (r: Runner) => {
    setChecking(r.id);
    try {
      const updated = await checkRunner(r.id);
      setRunners((list) => list?.map((x) => (x.id === updated.id ? updated : x)) ?? null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setChecking(null);
    }
  };

  const remove = async (r: Runner) => {
    if (!window.confirm(`Удалить раннер «${r.name}»?`)) return;
    setDeleting(r.id);
    try {
      await deleteRunner(r.id);
      await reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Раннеры"
        subtitle="Где выполняются сканы Strix: симуляция, этот сервер или удалённые хосты по SSH."
        action={isAdmin ? <Button variant="primary" onClick={openCreate}>+ Новый раннер</Button> : undefined}
      />
      <ErrorNote message={error} />

      {runners === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {runners?.length === 0 && (
        <EmptyState title="Раннеров нет" text="Добавьте раннер, чтобы можно было запускать сканы." action={isAdmin ? <Button variant="primary" onClick={openCreate}>Добавить раннер</Button> : undefined} />
      )}

      {runners && runners.length > 0 && (
        <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 md:grid-cols-2">
          {runners.map((r) => {
            const st = STATUS[r.status];
            return (
              <motion.div key={r.id} variants={rise}>
                <Card className="flex h-full flex-col p-5 transition-colors hover:border-accent/30">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-white">{r.name}</h3>
                      {r.kind === "ssh" && r.ssh_host && (
                        <p className="mt-0.5 truncate font-mono text-xs text-slate-500">{r.ssh_user}@{r.ssh_host}:{r.ssh_port}</p>
                      )}
                    </div>
                    <Badge tone="accent">{KIND_LABEL[r.kind]}</Badge>
                  </div>

                  <div className="mt-4 flex items-center gap-2 text-sm text-slate-300">
                    <StatusDot tone={st.tone} />
                    {st.label}
                    <span className="text-xs text-slate-600">
                      · {r.last_seen_at ? `в сети ${relativeTime(r.last_seen_at)}` : "ещё не был в сети"}
                    </span>
                  </div>
                  {r.status_detail && <p className="mt-2 text-xs leading-relaxed text-slate-500">{r.status_detail}</p>}

                  {isAdmin && (
                    <div className="mt-5 flex gap-2 pt-1">
                      <Button loading={checking === r.id} onClick={() => check(r)} className="flex-1">Проверить</Button>
                      <Button onClick={() => openEdit(r)}>Изменить</Button>
                      <Button variant="danger" loading={deleting === r.id} onClick={() => remove(r)}>✕</Button>
                    </div>
                  )}
                </Card>
              </motion.div>
            );
          })}
        </motion.div>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title={editing ? "Изменить раннер" : "Новый раннер"}>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Название">
            <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required maxLength={100} placeholder="build-server-1" />
          </Field>

          {!editing && (
            <Field label="Тип" hint={{ mock: "Симуляция: токены не тратятся — для отладки панели.", local: "Strix + Docker на сервере панели.", ssh: "Удалённый хост. Установка и проверка — в фазе 5." }[form.kind]}>
              <select className="field" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as RunnerKind })}>
                <option value="mock">Mock (симуляция)</option>
                <option value="local">Локальный (Strix + Docker)</option>
                <option value="ssh">SSH-хост</option>
              </select>
            </Field>
          )}

          {form.kind === "ssh" && (
            <>
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label="Хост">
                    <input className="field font-mono" value={form.ssh_host} onChange={(e) => setForm({ ...form, ssh_host: e.target.value })} required placeholder="10.0.0.5" />
                  </Field>
                </div>
                <Field label="Порт">
                  <input className="field font-mono" type="number" min={1} max={65535} value={form.ssh_port} onChange={(e) => setForm({ ...form, ssh_port: e.target.value })} />
                </Field>
              </div>
              <Field label="Пользователь">
                <input className="field font-mono" value={form.ssh_user} onChange={(e) => setForm({ ...form, ssh_user: e.target.value })} required placeholder="root" />
              </Field>
              <Field label="Приватный ключ" hint={editing?.has_ssh_key ? "Ключ сохранён. Оставьте пустым, чтобы не менять." : "PEM / OpenSSH. Хранится в зашифрованном виде."}>
                <textarea className="field h-28 resize-none font-mono text-xs" value={form.ssh_private_key} onChange={(e) => setForm({ ...form, ssh_private_key: e.target.value })} required={!editing?.has_ssh_key} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" spellCheck={false} />
              </Field>
            </>
          )}

          <ErrorNote message={formError} />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setOpen(false)}>Отмена</Button>
            <Button type="submit" variant="primary" loading={busy}>{editing ? "Сохранить" : "Создать"}</Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
