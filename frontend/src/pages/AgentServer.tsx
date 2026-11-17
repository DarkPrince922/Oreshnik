import clsx from "clsx";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, PageHeader, Spinner } from "../components/ui";
import {
  checkAgentServer, deleteAgentServer, errorMessage, forgetAgentServerKey, getAgentServer, getAgentServerLog, installAgentServer, saveAgentServer,
  trustAgentServerKey, type AgentServer, type AgentServerInput,
} from "../lib/api";
import { useAuth } from "../lib/auth";

const TONE = { online: "ok", installing: "accent", new: "warn", offline: "warn", error: "bad" } as const;
const LABEL = { online: "работает", installing: "идёт установка", new: "нужно подтвердить ключ", offline: "не готов", error: "ошибка" } as const;

type Form = { ssh_host: string; ssh_port: string; ssh_user: string; ssh_private_key: string; ssh_password: string };
const EMPTY: Form = { ssh_host: "", ssh_port: "22", ssh_user: "root", ssh_private_key: "", ssh_password: "" };

/** «Агент»: the user's own server with OpenHands, run as root. This page adds it, confirms its key and installs OpenHands there.
 * The OpenHands interface itself opens here in the next step. */
export default function AgentServerPage() {
  const { user, hasRole } = useAuth();
  const allowed = hasRole("admin") || user?.agent_access === true;
  const [server, setServer] = useState<AgentServer | null | undefined>(undefined);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<Form>(EMPTY);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState("");
  const logBox = useRef<HTMLPreElement>(null);

  const load = useCallback(async () => {
    try {
      const s = await getAgentServer();
      setServer(s);
      return s;
    } catch (e) {
      setError(errorMessage(e));
      setServer(null);
      return null;
    }
  }, []);

  useEffect(() => {
    if (allowed) void load();
  }, [allowed, load]);

  // While OpenHands is being installed, follow its log.
  useEffect(() => {
    if (!server?.installing) return;
    let alive = true;
    const tick = async () => {
      try {
        const l = await getAgentServerLog();
        if (!alive) return;
        setLog(l.log);
        if (!l.running) void load();
      } catch {
        /* the next tick tries again */
      }
    };
    void tick();
    const id = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [server?.installing, load]);

  useEffect(() => {
    if (logBox.current) logBox.current.scrollTop = logBox.current.scrollHeight;
  }, [log]);

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setError(null);
    try {
      const out = await fn();
      if (out && typeof out === "object" && "status" in out) setServer(out as AgentServer);
      else await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const startEdit = () => {
    setForm(server ? { ...EMPTY, ssh_host: server.ssh_host, ssh_port: String(server.ssh_port), ssh_user: server.ssh_user } : EMPTY);
    setEditing(true);
    setError(null);
  };

  const save = (e: FormEvent) => {
    e.preventDefault();
    const body: AgentServerInput = {
      ssh_host: form.ssh_host.trim(),
      ssh_port: Number(form.ssh_port) || 22,
      ssh_user: form.ssh_user.trim() || "root",
      ssh_private_key: form.ssh_private_key.trim() || null,
      ssh_password: form.ssh_password || null,
    };
    void act("save", async () => {
      const saved = await saveAgentServer(body);
      setEditing(false);
      return saved;
    });
  };

  if (!allowed) {
    return (
      <>
        <PageHeader title="Агент" subtitle="OpenHands на вашем собственном сервере, с правами root на нём." />
        <EmptyState title="Раздел вам не включён" text="Доступ к «Агенту» даёт администратор: «Пользователи» → кнопка «Агент» у вашего аккаунта." />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Агент"
        subtitle="OpenHands на вашем собственном сервере. Агент работает там от root: может ставить программы, поднимать сервисы и стенды. Сервер нужен отдельный — не храните на нём ничего ценного."
        action={server && !editing ? <Button onClick={startEdit}>Изменить сервер</Button> : undefined}
      />
      <ErrorNote message={error} />

      {server === undefined && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {server === null && !editing && (
        <EmptyState
          title="Сервер ещё не добавлен"
          text="Возьмите отдельный VDS (Ubuntu 22.04/24.04, от 2 ядер и 4 ГБ памяти), укажите его адрес и вход по SSH под root — панель сама поставит туда OpenHands."
          action={<Button variant="primary" onClick={startEdit}>Добавить сервер</Button>}
        />
      )}

      {editing && (
        <Card className="max-w-3xl p-6">
          <form onSubmit={save} className="space-y-4">
            <h2 className="text-lg font-semibold text-white">{server ? "Сервер" : "Новый сервер"}</h2>
            <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_7rem_10rem]">
              <Field label="Адрес (IP или домен)">
                <input className="field font-mono" value={form.ssh_host} required onChange={(e) => setForm({ ...form, ssh_host: e.target.value })} placeholder="203.0.113.5" />
              </Field>
              <Field label="Порт SSH">
                <input className="field font-mono" inputMode="numeric" value={form.ssh_port} onChange={(e) => setForm({ ...form, ssh_port: e.target.value })} />
              </Field>
              <Field label="Пользователь">
                <input className="field font-mono" value={form.ssh_user} onChange={(e) => setForm({ ...form, ssh_user: e.target.value })} />
              </Field>
            </div>
            <Field label="Закрытый SSH-ключ" hint={server?.has_key ? "Ключ уже сохранён. Вставьте новый, чтобы заменить; оставьте пустым, чтобы не менять." : "Вставьте ключ целиком, вместе со строками BEGIN/END. Хранится зашифрованным и больше не показывается."}>
              <textarea className="field min-h-[7rem] font-mono text-xs" value={form.ssh_private_key} onChange={(e) => setForm({ ...form, ssh_private_key: e.target.value })} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" autoComplete="off" />
            </Field>
            <Field label="Или пароль" hint={server?.has_password ? "Пароль уже сохранён; оставьте пустым, чтобы не менять." : "Можно вместо ключа. Хранится зашифрованным."}>
              <input className="field" type="password" value={form.ssh_password} onChange={(e) => setForm({ ...form, ssh_password: e.target.value })} autoComplete="new-password" />
            </Field>
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" onClick={() => setEditing(false)}>Отмена</Button>
              <Button type="submit" variant="primary" loading={busy === "save"}>Сохранить</Button>
            </div>
          </form>
        </Card>
      )}

      {server && !editing && (
        <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <Card className="space-y-4 p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate font-mono text-lg text-white">{server.ssh_user}@{server.ssh_host}{server.ssh_port !== 22 ? `:${server.ssh_port}` : ""}</p>
                <p className="text-xs text-slate-400">вход: {[server.has_key && "ключ", server.has_password && "пароль"].filter(Boolean).join(" + ")}{server.openhands_version ? ` · OpenHands ${server.openhands_version}` : ""}</p>
              </div>
              <Badge tone={TONE[server.status]}>{LABEL[server.status]}</Badge>
            </div>
            {server.status_detail && (
              <p className={clsx("rounded-lg border px-3.5 py-2.5 text-xs leading-relaxed [overflow-wrap:anywhere]", server.status === "error" ? "border-signal-bad/25 bg-signal-bad/10 text-signal-bad" : "border-white/10 bg-white/5 text-slate-300")}>{server.status_detail}</p>
            )}

            {server.pending_fingerprint && (
              <div className="space-y-3 rounded-xl border border-signal-warn/30 bg-signal-warn/[0.07] p-4">
                <p className="text-sm text-slate-200">Сервер показал ключ. Сравните его с выводом на самом сервере, прежде чем подтверждать:</p>
                <p className="break-all rounded-lg bg-black/40 px-3 py-2 font-mono text-xs text-white">{server.pending_fingerprint}</p>
                <p className="text-[11px] text-slate-400">На сервере: <span className="font-mono">ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</span></p>
                <Button variant="primary" loading={busy === "trust"} onClick={() => act("trust", () => trustAgentServerKey(server.pending_fingerprint as string))}>
                  Совпадает — подтвердить
                </Button>
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <Button loading={busy === "check"} disabled={server.installing} onClick={() => act("check", checkAgentServer)}>Проверить</Button>
              <Button
                variant="primary"
                loading={busy === "install"}
                disabled={!server.host_key_fingerprint || server.installing}
                title={!server.host_key_fingerprint ? "Сначала проверьте сервер и подтвердите его ключ" : "Поставить, починить или обновить OpenHands"}
                onClick={() => act("install", installAgentServer)}
              >
                {server.openhands_version ? "Переустановить" : "Установить OpenHands"}
              </Button>
              {server.host_key_fingerprint && (
                <Button disabled={server.installing} onClick={() => window.confirm("Сбросить подтверждённый ключ сервера? Понадобится подтвердить его заново.") && act("forget", forgetAgentServerKey)}>
                  Сбросить ключ
                </Button>
              )}
              <Button variant="danger" disabled={server.installing} onClick={() => window.confirm("Убрать сервер из панели? На самом сервере ничего не удаляется.") && act("delete", async () => { await deleteAgentServer(); setServer(null); })}>
                Убрать
              </Button>
            </div>
            {server.status === "online" && (
              <p className="text-xs leading-relaxed text-slate-400">
                OpenHands работает и слушает только внутри сервера (127.0.0.1:8000). Открыть его интерфейс прямо здесь можно будет в следующем обновлении панели.
              </p>
            )}
          </Card>

          {(server.installing || log) && (
            <Card className="min-w-0 overflow-hidden">
              <div className="flex items-center justify-between border-b border-white/5 px-5 py-3">
                <h2 className="text-sm font-semibold text-white">Журнал установки</h2>
                {server.installing && <Spinner className="h-3.5 w-3.5" />}
              </div>
              <pre ref={logBox} className="max-h-[32rem] overflow-y-auto whitespace-pre-wrap px-5 py-4 font-mono text-[11px] leading-relaxed text-slate-300 [overflow-wrap:anywhere]">{log || "Ожидание…"}</pre>
            </Card>
          )}
        </div>
      )}
    </>
  );
}
