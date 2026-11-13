import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner, StatusDot, relativeTime, rise, stagger } from "../components/ui";
import {
  checkRunner,
  createRunner,
  deleteRunner,
  errorMessage,
  forgetHostKey,
  getProvisionLog,
  listRunners,
  provisionRunner,
  trustHostKey,
  updateRunner,
  type Runner,
  type RunnerKind,
  type RunnerStatus,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { authLabel, groupFingerprint, hasLogin, shortFingerprint, sshActions } from "../lib/hostKey";

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
  ssh_password: string;
  clear_key: boolean;
  clear_password: boolean;
}
const emptyForm: Form = { name: "", kind: "mock", ssh_host: "", ssh_port: "22", ssh_user: "", ssh_private_key: "", ssh_password: "", clear_key: false, clear_password: false };

/** What the panel knows about the host's key, and what to do about it. */
function HostKeyBlock({ runner, busy, onConfirm, onForget }: { runner: Runner; busy: boolean; onConfirm: () => void; onForget: () => void }) {
  const a = sshActions(runner);
  if (a.state === "unknown") return null;
  const pinned = runner.ssh_host_key_fingerprint;
  const pending = runner.ssh_host_key_pending_fingerprint;
  if (a.state === "pending" && pending) {
    return (
      <div data-testid="hostkey-pending" className="mt-3 rounded-lg border border-signal-warn/25 bg-signal-warn/10 px-3.5 py-3 text-xs">
        <div className="font-medium text-signal-warn">Ключ хоста не подтверждён</div>
        <div className="mt-1 break-all font-mono text-[11px] text-slate-300" title={pending}>{shortFingerprint(pending, 14)}</div>
        <p className="mt-1.5 leading-relaxed text-slate-500">Пока вы не сверите отпечаток, панель не отправит хосту ни ключ, ни пароль.</p>
        <Button className="mt-2.5 !px-3 !py-1.5 text-xs" onClick={onConfirm} disabled={!a.canConfirm || busy}>Проверить отпечаток…</Button>
      </div>
    );
  }
  if (a.state === "changed" && pinned && pending) {
    return (
      <div data-testid="hostkey-changed" role="alert" className="mt-3 rounded-lg border border-signal-bad/30 bg-signal-bad/10 px-3.5 py-3 text-xs">
        <div className="font-medium text-signal-bad">Хост показывает другой ключ</div>
        <p className="mt-1 leading-relaxed text-slate-400">
          Это бывает после переустановки сервера — и при подмене хоста. Панель не подключается и не отправляет ничего, пока вы не решите.
        </p>
        <dl className="mt-2 space-y-1 font-mono text-[11px]">
          <div className="flex gap-2"><dt className="w-16 shrink-0 text-slate-500">был</dt><dd className="break-all text-slate-300">{shortFingerprint(pinned, 14)}</dd></div>
          <div className="flex gap-2"><dt className="w-16 shrink-0 text-slate-500">сейчас</dt><dd className="break-all text-signal-bad">{shortFingerprint(pending, 14)}</dd></div>
        </dl>
        <Button variant="danger" className="mt-2.5 !px-3 !py-1.5 text-xs" onClick={onForget} disabled={!a.canForget || busy}>Сбросить старый ключ…</Button>
      </div>
    );
  }
  return (
    <div data-testid="hostkey-trusted" className="mt-3 flex items-center justify-between gap-3 text-[11px] text-slate-500">
      <span className="min-w-0 truncate" title={pinned ?? ""}>
        Ключ хоста подтверждён · <span className="font-mono text-slate-400">{runner.ssh_host_key_algorithm ?? "ssh"}</span> ·{" "}
        <span className="font-mono text-slate-400">{pinned ? shortFingerprint(pinned) : ""}</span>
      </span>
      {a.canForget && <button className="shrink-0 transition hover:text-white" onClick={onForget} disabled={busy}>Сбросить</button>}
    </div>
  );
}

/** The fingerprint is compared by a person with what the server itself says; only then is the key pinned. */
function HostKeyModal({ runner, onClose, onDone }: { runner: Runner | null; onClose: () => void; onDone: (r: Runner) => void }) {
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setSure(false);
    setError(null);
    setBusy(false);
  }, [runner?.id, runner?.ssh_host_key_pending_fingerprint]);
  const fp = runner?.ssh_host_key_pending_fingerprint ?? null;

  const confirm = async () => {
    if (!runner || !fp) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await trustHostKey(runner.id, fp));
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  };

  return (
    <Modal open={runner !== null && fp !== null} onClose={onClose} title="Подтвердите ключ хоста">
      {runner && fp && (
        <div className="space-y-4" data-testid="trust-dialog">
          <p className="text-xs leading-relaxed text-slate-400">
            Панель впервые подключается к <span className="font-mono text-slate-200">{runner.ssh_user}@{runner.ssh_host}:{runner.ssh_port}</span>.
            Чтобы убедиться, что это тот сервер, сверьте отпечаток его ключа.
          </p>
          <div className="rounded-xl border border-white/10 bg-black/40 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-600">{runner.ssh_host_key_algorithm ?? "ключ"}</div>
            <div className="mt-1.5 break-words font-mono text-sm leading-relaxed text-white" data-testid="trust-fingerprint">{groupFingerprint(fp)}</div>
          </div>
          <div className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-3 text-xs leading-relaxed text-slate-400">
            Выполните <span className="font-semibold text-slate-300">на самом сервере</span> (через консоль провайдера или уже доверенный вход):
            <pre className="mt-2 overflow-x-auto rounded-md bg-black/50 px-3 py-2 font-mono text-[11px] text-slate-300">ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</pre>
            Для ключа другого типа замените файл на <span className="font-mono">ssh_host_rsa_key.pub</span> или <span className="font-mono">ssh_host_ecdsa_key.pub</span>.
            Если отпечатки <span className="text-slate-200">не совпадают</span> — не подтверждайте: возможно, вас подключают к другому узлу.
          </div>
          <label className="flex items-start gap-3 text-xs leading-relaxed text-slate-300">
            <input type="checkbox" className="mt-0.5 h-4 w-4 shrink-0 accent-[#7c5cff]" checked={sure} onChange={(e) => setSure(e.target.checked)} />
            <span>Я сверил(а) отпечаток с тем, что показывает сам сервер, и они совпадают.</span>
          </label>
          <ErrorNote message={error} />
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Отмена</Button>
            <Button variant="primary" loading={busy} disabled={!sure} onClick={confirm}>Доверять этому хосту</Button>
          </div>
        </div>
      )}
    </Modal>
  );
}

function ProvisionModal({ runner, onClose, onStarted }: { runner: Runner | null; onClose: () => void; onStarted: (r: Runner) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setBusy(false);
    setError(null);
  }, [runner?.id]);
  const start = async () => {
    if (!runner) return;
    setBusy(true);
    setError(null);
    try {
      onStarted(await provisionRunner(runner.id));
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  };
  return (
    <Modal open={runner !== null} onClose={onClose} title="Установить на хост">
      {runner && (
        <div className="space-y-4" data-testid="provision-dialog">
          <p className="text-xs leading-relaxed text-slate-400">
            Панель зайдёт на <span className="font-mono text-slate-200">{runner.ssh_user}@{runner.ssh_host}</span>, посмотрит, чего не хватает, и установит <span className="text-slate-200">только это</span>:
          </p>
          <ul className="space-y-1.5 text-xs leading-relaxed text-slate-300">
            <li>· <span className="text-white">Docker</span> — официальным скриптом get.docker.com, пользователь добавляется в группу <span className="font-mono">docker</span></li>
            <li>· <span className="text-white">python3, util-linux</span> — из пакетов дистрибутива, если их нет</li>
            <li>· <span className="text-white">uv и Strix</span> — для этого пользователя, версия зафиксирована</li>
            <li>· образ песочницы Strix — загружается заранее (он большой)</li>
          </ul>
          <p className="rounded-lg border border-signal-warn/25 bg-signal-warn/10 px-3.5 py-2.5 text-xs leading-relaxed text-signal-warn">
            Системные пакеты ставятся только на Debian/Ubuntu и требуют root или sudo без пароля. Установщики выполняются с правами этого пользователя;
            каждый шаг и его вывод попадают в журнал.
          </p>
          <ErrorNote message={error} />
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Отмена</Button>
            <Button variant="primary" loading={busy} onClick={start}>Установить</Button>
          </div>
        </div>
      )}
    </Modal>
  );
}

/** The installation log: polled while it runs, then left as it ended. */
function ProvisionLogPanel({ runnerId, running }: { runnerId: number; running: boolean }) {
  const [log, setLog] = useState("");
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getProvisionLog(runnerId)
        .then((r) => alive && (setLog(r.log), setError(null)))
        .catch((e) => alive && setError(errorMessage(e)));
    void load();
    if (!running) return () => { alive = false; };
    const id = setInterval(() => !document.hidden && void load(), 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [runnerId, running]);

  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  return (
    <div className="mt-3" data-testid="provision-log">
      <div className="mb-1.5 flex items-center justify-between text-[11px] uppercase tracking-wider text-slate-600">
        <span>{running ? "Журнал установки · идёт" : "Журнал последней установки"}</span>
        {running && <Spinner className="h-3 w-3" />}
      </div>
      <pre ref={box} className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/10 bg-black/50 px-3 py-2.5 font-mono text-[11px] leading-relaxed text-slate-300">
        {log || (error ? "" : "…")}
      </pre>
      <ErrorNote message={error} />
    </div>
  );
}

export default function Runners() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [runners, setRunners] = useState<Runner[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [trusting, setTrusting] = useState<Runner | null>(null);
  const [installing, setInstalling] = useState<Runner | null>(null);
  const [logOpen, setLogOpen] = useState<Record<number, boolean>>({});
  const [acting, setActing] = useState<number | null>(null);

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

  // While a host is being installed, its status changes under us: keep the list fresh.
  const anyProvisioning = runners?.some((r) => r.status === "provisioning") ?? false;
  useEffect(() => {
    if (!anyProvisioning) return;
    const id = setInterval(() => !document.hidden && void reload(), 3000);
    return () => clearInterval(id);
  }, [anyProvisioning, reload]);

  const replace = (updated: Runner) => setRunners((list) => list?.map((x) => (x.id === updated.id ? updated : x)) ?? null);
  const forget = async (r: Runner) => {
    if (!window.confirm(`Сбросить подтверждённый ключ хоста «${r.name}»? Следующая проверка покажет новый отпечаток для подтверждения.`)) return;
    setActing(r.id);
    try {
      replace(await forgetHostKey(r.id));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setActing(null);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setFormError(null);
    setOpen(true);
  };
  const openEdit = (r: Runner) => {
    setEditing(r);
    setForm({ ...emptyForm, name: r.name, kind: r.kind, ssh_host: r.ssh_host ?? "", ssh_port: String(r.ssh_port ?? 22), ssh_user: r.ssh_user ?? "" });
    setFormError(null);
    setOpen(true);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const ssh = form.kind === "ssh";
    if (ssh && !hasLogin({ key: form.ssh_private_key, password: form.ssh_password, hadKey: !!editing?.has_ssh_key, hadPassword: !!editing?.has_ssh_password, clearKey: form.clear_key, clearPassword: form.clear_password })) {
      setFormError("Нужен хотя бы один способ входа: приватный ключ или пароль.");
      return;
    }
    setBusy(true);
    try {
      const port = Number(form.ssh_port) || 22;
      const credentials = {
        ...(form.ssh_private_key.trim() ? { ssh_private_key: form.ssh_private_key } : {}),
        ...(form.ssh_password ? { ssh_password: form.ssh_password } : {}),
      };
      if (editing) {
        await updateRunner(editing.id, {
          name: form.name,
          ...(ssh
            ? { ssh_host: form.ssh_host, ssh_port: port, ssh_user: form.ssh_user, ...credentials, clear_ssh_key: form.clear_key, clear_ssh_password: form.clear_password }
            : {}),
        });
      } else {
        await createRunner({
          name: form.name,
          kind: form.kind,
          ...(ssh ? { ssh_host: form.ssh_host, ssh_port: port, ssh_user: form.ssh_user, ...credentials } : {}),
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
        <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(22rem,1fr))] [&>*]:max-w-[44rem]">
          {runners.map((r) => {
            const st = STATUS[r.status];
            return (
              <motion.div key={r.id} variants={rise} data-testid={`runner-${r.id}`}>
                <Card className="flex h-full flex-col p-5 transition-colors hover:border-accent/30">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-white">{r.name}</h3>
                      {r.kind === "ssh" && r.ssh_host && (
                        <p className="mt-0.5 truncate font-mono text-xs text-slate-500">
                          {r.ssh_user}@{r.ssh_host}:{r.ssh_port}
                          <span className="ml-2 font-sans text-slate-600" data-testid="auth-label">· вход: {authLabel(r)}</span>
                        </p>
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

                  {isAdmin && r.kind === "ssh" && (
                    <HostKeyBlock runner={r} busy={acting === r.id} onConfirm={() => setTrusting(r)} onForget={() => forget(r)} />
                  )}
                  {isAdmin && r.kind === "ssh" && (r.status === "provisioning" || (logOpen[r.id] && r.has_provision_log)) && (
                    <ProvisionLogPanel runnerId={r.id} running={r.status === "provisioning"} />
                  )}

                  {isAdmin && (
                    <div className="mt-5 flex flex-wrap gap-2 pt-1">
                      <Button loading={checking === r.id} disabled={r.status === "provisioning"} onClick={() => check(r)} className="flex-1">Проверить</Button>
                      {r.kind === "ssh" && sshActions(r).canInstall && (
                        <Button variant="primary" onClick={() => setInstalling(r)}>Установить</Button>
                      )}
                      {r.kind === "ssh" && r.has_provision_log && r.status !== "provisioning" && (
                        <Button onClick={() => setLogOpen((o) => ({ ...o, [r.id]: !o[r.id] }))}>{logOpen[r.id] ? "Скрыть журнал" : "Журнал"}</Button>
                      )}
                      <Button disabled={r.status === "provisioning"} onClick={() => openEdit(r)}>Изменить</Button>
                      <Button variant="danger" loading={deleting === r.id} onClick={() => remove(r)}>✕</Button>
                    </div>
                  )}
                </Card>
              </motion.div>
            );
          })}
        </motion.div>
      )}

      <HostKeyModal
        runner={trusting}
        onClose={() => setTrusting(null)}
        onDone={(r) => {
          replace(r);
          setTrusting(null);
        }}
      />
      <ProvisionModal
        runner={installing}
        onClose={() => setInstalling(null)}
        onStarted={(r) => {
          replace(r);
          setInstalling(null);
          void reload();
        }}
      />

      <Modal open={open} onClose={() => setOpen(false)} title={editing ? "Изменить раннер" : "Новый раннер"}>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Название">
            <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required maxLength={100} placeholder="build-server-1" />
          </Field>

          {!editing && (
            <Field label="Тип" hint={{ mock: "Симуляция: токены не тратятся — для отладки панели.", local: "Strix + Docker на сервере панели.", ssh: "Удалённый хост. Панель подключится по SSH (ключом или паролем), попросит сверить отпечаток хоста и сможет установить Docker и Strix." }[form.kind]}>
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
                  <Field label="Хост" hint={editing ? "Если изменить адрес или порт, подтверждённый ключ хоста сбросится." : undefined}>
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
              <Field label="Приватный ключ" hint={editing?.has_ssh_key ? "Ключ сохранён. Оставьте пустым, чтобы не менять." : "PEM / OpenSSH, без пароля; публичная часть — в authorized_keys пользователя. Хранится в зашифрованном виде. Нужен ключ или пароль."}>
                <textarea className="field h-24 resize-none font-mono text-xs" value={form.ssh_private_key} onChange={(e) => setForm({ ...form, ssh_private_key: e.target.value })} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" spellCheck={false} />
              </Field>
              <Field label="Пароль" hint={editing?.has_ssh_password ? "Пароль сохранён. Оставьте пустым, чтобы не менять." : "Вход по паролю менее надёжен, чем по ключу: пароль можно подобрать. Хранится в зашифрованном виде и отправляется только хосту с подтверждённым ключом. Если указаны оба, сначала пробуется ключ."}>
                <input className="field font-mono" type="password" autoComplete="new-password" value={form.ssh_password} onChange={(e) => setForm({ ...form, ssh_password: e.target.value })} placeholder={editing?.has_ssh_password ? "••••••••" : ""} aria-label="Пароль" />
              </Field>
              {editing && (editing.has_ssh_key || editing.has_ssh_password) && (
                <div className="space-y-1.5 text-xs text-slate-400">
                  {editing.has_ssh_password && (
                    <label className="flex items-center gap-2"><input type="checkbox" className="accent-[#7c5cff]" checked={form.clear_password} onChange={(e) => setForm({ ...form, clear_password: e.target.checked })} /> Удалить сохранённый пароль</label>
                  )}
                  {editing.has_ssh_key && (
                    <label className="flex items-center gap-2"><input type="checkbox" className="accent-[#7c5cff]" checked={form.clear_key} onChange={(e) => setForm({ ...form, clear_key: e.target.checked })} /> Удалить сохранённый ключ</label>
                  )}
                  <p className="text-[11px] text-slate-600">Хотя бы один способ входа должен остаться. Для установки системных пакетов нужен root или sudo без пароля — пароль входа для sudo не используется.</p>
                </div>
              )}
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
