import clsx from "clsx";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner, Switch } from "../components/ui";
import {
  checkMcp, createMcp, deleteMcp, errorMessage, listMcp, updateMcp, type McpInput, type McpSecret, type McpServer, type PersonaScope,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { MCP_PRESETS, type McpPreset } from "../lib/mcpCatalog";
import { SCOPE_LABEL } from "../lib/personas";
import { mcpHealth } from "../lib/workshop";

const EMPTY: McpInput = {
  name: "", description: "", transport: "command", command: "", url: "", env: [], headers: [], bearer_token: null,
  scope: "both", enabled: true, shared: false, startup_timeout_sec: 30, tool_timeout_sec: 120,
};
const HEALTH_DOT = { ok: "bg-signal-ok", bad: "bg-signal-bad", unknown: "bg-slate-500" } as const;

function PlugIcon({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 3v4M15 3v4M7 7h10v4a5 5 0 0 1-10 0z" />
      <path d="M12 16v5" />
    </svg>
  );
}

function healthText(s: McpServer): string {
  const c = s.last_check;
  if (!c) return "не проверен";
  return c.ok ? `работает · инструментов: ${c.tools.length}` : "не запустился";
}

/** Rows of secrets: a stored one shows its name and an empty value field ("сохранено"); typing a value replaces it. */
function Secrets({ label, hint, items, onChange, namePlaceholder, disabled }: {
  label: string; hint: string; items: McpSecret[]; onChange: (items: McpSecret[]) => void; namePlaceholder: string; disabled: boolean;
}) {
  const set = (i: number, patch: Partial<McpSecret>) => onChange(items.map((it, j) => (j === i ? { ...it, ...patch } : it)));
  return (
    <Field label={label} hint={hint}>
      <div className="space-y-2">
        {items.map((it, i) => (
          <div key={i} className="flex gap-2">
            <input className="field w-2/5 font-mono text-xs" value={it.name} disabled={disabled} placeholder={namePlaceholder} aria-label="Имя" onChange={(e) => set(i, { name: e.target.value.trim() })} />
            <input
              className="field min-w-0 flex-1 font-mono text-xs"
              type="password"
              autoComplete="new-password"
              value={it.value ?? ""}
              disabled={disabled}
              aria-label="Значение"
              placeholder={it.value === null ? "сохранено — впишите, чтобы заменить" : "значение"}
              onChange={(e) => set(i, { value: e.target.value })}
            />
            {!disabled && <Button type="button" onClick={() => onChange(items.filter((_, j) => j !== i))} aria-label="Убрать">✕</Button>}
          </div>
        ))}
        {!disabled && <Button type="button" onClick={() => onChange([...items, { name: "", value: "" }])}>+ Добавить</Button>}
      </div>
    </Field>
  );
}

function ToolsList({ server, checking }: { server: McpServer; checking: boolean }) {
  const check = server.last_check;
  if (checking) {
    return <p className="flex items-center gap-2 px-5 py-4 text-xs text-slate-400"><Spinner className="h-3.5 w-3.5" /> Запускаю сервер в контейнере агента… первый запуск через npx скачивает пакет и может занять минуту.</p>;
  }
  if (!check) return <p className="px-5 py-4 text-xs text-slate-400">Ещё не проверен: «Проверить» запустит сервер так же, как в сессии, и покажет его инструменты.</p>;
  if (!check.ok) return <p className="px-5 py-4 text-xs leading-relaxed text-signal-bad [overflow-wrap:anywhere]">Не запустился: {check.error}</p>;
  return (
    <ul className="max-h-[26rem] divide-y divide-white/5 overflow-y-auto">
      {check.tools.map((t) => (
        <li key={t.name} className="px-5 py-2.5">
          <p className="font-mono text-xs text-accent-soft">{t.name}</p>
          {t.description && <p className="mt-0.5 text-xs leading-relaxed text-slate-400">{t.description}</p>}
        </li>
      ))}
      {check.tools.length === 0 && <li className="px-5 py-4 text-xs text-slate-400">Сервер запустился, но инструментов не предлагает.</li>}
    </ul>
  );
}

/** The fields of a server: how to start or reach it and its secrets. Shared by the wizard and the detail view. */
function ServerFields({ form, setForm, readOnly, hasToken, fixedCommand, hideSecrets }: {
  form: McpInput; setForm: (f: McpInput) => void; readOnly: boolean; hasToken: boolean; fixedCommand?: boolean; hideSecrets?: boolean;
}) {
  const [editCommand, setEditCommand] = useState(!fixedCommand);
  return (
    <>
      {form.transport === "command" ? (
        editCommand ? (
          <Field label="Команда запуска" hint="Выполняется в контейнере агента, там есть node/npx, python3 и pip. Аргументы с пробелами берите в кавычки.">
            <input className="field font-mono text-xs" value={form.command ?? ""} required disabled={readOnly} onChange={(e) => setForm({ ...form, command: e.target.value })} placeholder="npx -y <пакет>" />
          </Field>
        ) : (
          <p className="text-xs text-slate-400">
            Запуск: <span className="font-mono text-slate-200">{form.command}</span>{" "}
            <button type="button" className="text-accent-soft underline decoration-dotted" onClick={() => setEditCommand(true)}>изменить</button>
          </p>
        )
      ) : (
        <>
          <Field label="Адрес (streamable HTTP)" hint="Должен быть доступен из контейнера агента: адрес компьютера в сети, а не localhost.">
            <input className="field font-mono text-xs" value={form.url ?? ""} required disabled={readOnly} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="http://192.168.1.10:9876/mcp" />
          </Field>
          <Field label="Токен (Authorization: Bearer)" hint={hasToken ? "Токен сохранён. Впишите новый, чтобы заменить." : "Если сервер его просит. Хранится зашифрованным."}>
            <input className="field font-mono text-xs" type="password" autoComplete="new-password" disabled={readOnly} value={form.bearer_token ?? ""} onChange={(e) => setForm({ ...form, bearer_token: e.target.value || null })} placeholder={hasToken ? "сохранён" : ""} />
          </Field>
        </>
      )}
      {hideSecrets ? null : form.transport === "command" ? (
        <Secrets label="Переменные окружения" hint="Ключи API и прочее, что серверу нужно. Хранятся зашифрованными и обратно не показываются." items={form.env} onChange={(env) => setForm({ ...form, env })} namePlaceholder="API_KEY" disabled={readOnly} />
      ) : (
        <Secrets label="Заголовки" hint="Например X-Api-Key. Значения хранятся зашифрованными." items={form.headers} onChange={(headers) => setForm({ ...form, headers })} namePlaceholder="X-Api-Key" disabled={readOnly} />
      )}
    </>
  );
}

function body(form: McpInput): McpInput {
  return {
    ...form,
    name: form.name.trim(),
    command: form.transport === "command" ? (form.command ?? "").trim() : null,
    url: form.transport === "url" ? (form.url ?? "").trim() : null,
    env: form.transport === "command" ? form.env.filter((s) => s.name) : [],
    headers: form.transport === "url" ? form.headers.filter((s) => s.name) : [],
  };
}

const STEPS = ["Что подключаем", "Данные", "Проверка", "Готово"];

/** Adding a server from the catalog: pick → fill in → check (in the agent container) → done. A failed check goes back to the data. */
function Wizard({ open, onClose, onSaved, isAdmin, taken }: { open: boolean; onClose: () => void; onSaved: (s: McpServer) => void; isAdmin: boolean; taken: Set<string> }) {
  const [step, setStep] = useState(0);
  const [preset, setPreset] = useState<McpPreset | null>(null);
  const [form, setForm] = useState<McpInput>(EMPTY);
  const [saved, setSaved] = useState<McpServer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setStep(0);
      setPreset(null);
      setSaved(null);
      setError(null);
    }
  }, [open]);

  const choose = (p: McpPreset) => {
    let name = p.name;
    for (let i = 2; name && taken.has(name); i++) name = `${p.name}${i}`;
    setPreset(p);
    setForm({
      ...EMPTY, name, description: p.description, transport: p.transport, command: p.command ?? "", url: "",
      env: (p.env ?? []).map((e) => ({ name: e.name, value: "" })),
    });
    setError(null);
    setStep(1);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const b = body(form);
      const missing = b.env.find((s) => !s.value && s.value !== null);
      if (missing) throw new Error(`Впишите значение ${missing.name}`);
      const server = saved ? await updateMcp(saved.id, { ...b, shared: undefined }) : await createMcp(b);
      setSaved(server);
      setStep(2);
      const checked = await checkMcp(server.id);
      setSaved(checked);
      onSaved(checked);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const check = saved?.last_check;
  return (
    <Modal open={open} onClose={onClose} title="Подключить MCP-сервер" wide="xl">
      <ol className="mb-6 flex flex-wrap items-center gap-3">
        {STEPS.map((t, i) => (
          <li key={t} className="flex items-center gap-2">
            <span className={clsx("flex h-6 w-6 items-center justify-center rounded-full text-[12px] font-semibold", i < step || (i === 3 && step === 3) ? "bg-signal-ok/20 text-signal-ok" : i === step ? "bg-accent text-white" : "bg-white/10 text-slate-400")}>
              {i < step ? "✓" : i + 1}
            </span>
            <span className={clsx("text-[13px]", i <= step ? "text-white" : "text-slate-500")}>{t}</span>
            {i < STEPS.length - 1 && <span aria-hidden="true" className="ml-1 h-px w-8 bg-white/10" />}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {MCP_PRESETS.map((p) => (
            <button key={p.id} type="button" onClick={() => choose(p)} className="rounded-2xl border border-white/[0.08] bg-ink-800/60 p-4 text-left transition-all hover:-translate-y-px hover:border-accent/40 hover:bg-accent/[0.08]">
              <span className="flex items-center gap-2.5">
                <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-accent/40 to-accent-glow/20 text-white ring-1 ring-white/10"><PlugIcon className="h-4 w-4" /></span>
                <span className="text-[14px] font-semibold text-white">{p.title}</span>
              </span>
              <span className="mt-2 block text-[12px] leading-snug text-slate-400">{p.hint}</span>
              {p.env?.length ? <span className="mt-2 inline-block rounded-md bg-white/5 px-1.5 py-0.5 text-[10.5px] text-slate-400">нужен ключ</span> : null}
            </button>
          ))}
        </div>
      )}

      {step === 1 && preset && (
        <form onSubmit={submit} className="space-y-4">
          <div className="rounded-xl border border-white/10 bg-white/[0.03] p-4 text-[12.5px] leading-relaxed text-slate-300"><b className="text-white">{preset.title}.</b> {preset.guide}</div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Имя для агента" hint="Латиница, цифры, _ и -. Модель видит инструменты как mcp__имя__…">
              <input className="field font-mono" value={form.name} required maxLength={32} pattern="[a-z][a-z0-9_\-]*" onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} placeholder="burp" />
            </Field>
            <Field label="Коротко, для списка">
              <input className="field" value={form.description} maxLength={200} onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </Field>
          </div>
          {preset.env?.length ? (
            preset.env.map((e, i) => (
              <Field key={e.name} label={e.label} hint={`Передаётся серверу как ${e.name}. Хранится зашифрованным.`}>
                <input className="field font-mono text-xs" type="password" autoComplete="new-password" required value={form.env[i]?.value ?? ""} placeholder={e.placeholder}
                  onChange={(ev) => setForm({ ...form, env: form.env.map((s, j) => (j === i ? { ...s, value: ev.target.value } : s)) })} />
              </Field>
            ))
          ) : null}
          {preset.id === "custom" || preset.id === "url" ? (
            <ServerFields form={form} setForm={setForm} readOnly={false} hasToken={false} />
          ) : (
            <ServerFields form={form} setForm={setForm} readOnly={false} hasToken={false} fixedCommand hideSecrets key={preset.id} />
          )}
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Каким агентам">
              <select className="field" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value as PersonaScope })}>
                {(Object.keys(SCOPE_LABEL) as PersonaScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
              </select>
            </Field>
            {isAdmin && !saved && (
              <label className="flex items-center gap-3 self-end pb-3 text-sm text-slate-300">
                <input type="checkbox" className="h-4 w-4 accent-accent" checked={Boolean(form.shared)} onChange={(e) => setForm({ ...form, shared: e.target.checked })} />
                Общий: для всех (их агенты получат и ключи)
              </label>
            )}
          </div>
          <ErrorNote message={error} />
          <div className="flex justify-between pt-1">
            <Button type="button" onClick={() => setStep(0)} disabled={busy || saved !== null}>← Назад</Button>
            <Button type="submit" variant="primary" loading={busy}>Проверить подключение →</Button>
          </div>
        </form>
      )}

      {step === 2 && (
        <div className="space-y-4">
          {busy || !check ? (
            <p className="flex items-center gap-2 py-6 text-sm text-slate-300"><Spinner className="h-4 w-4" /> Запускаю сервер в контейнере агента — так же, как в сессии… Первый запуск через npx скачивает пакет и может занять минуту.</p>
          ) : check.ok ? (
            <>
              <p className="text-sm text-signal-ok">Работает: агенту доступно инструментов — {check.tools.length}.</p>
              {saved && <Card className="overflow-hidden"><ToolsList server={saved} checking={false} /></Card>}
              <div className="flex justify-end"><Button variant="primary" onClick={() => setStep(3)}>Готово →</Button></div>
            </>
          ) : (
            <>
              <p className="rounded-xl border border-signal-bad/25 bg-signal-bad/10 p-4 text-[12.5px] leading-relaxed text-signal-bad [overflow-wrap:anywhere]">Не запустился: {check.error}</p>
              <p className="text-xs text-slate-400">Сервер сохранён, но выключать его не нужно: исправьте данные и проверьте снова. Частые причины — неверный ключ или адрес, недоступный из контейнера агента.</p>
              <div className="flex justify-between">
                <Button onClick={() => setStep(1)}>← Исправить</Button>
                <Button onClick={onClose}>Закрыть</Button>
              </div>
            </>
          )}
          <ErrorNote message={error} />
        </div>
      )}

      {step === 3 && saved && (
        <div className="space-y-4 py-2">
          <p className="text-base text-white">Сервер <span className="font-mono text-accent-soft">{saved.name}</span> подключён.</p>
          <p className="text-sm leading-relaxed text-slate-300">Агент «Кода» получит его инструменты со следующего сообщения — и в новых сессиях, и в уже открытых. Выключить можно переключателем на карточке.</p>
          <div className="flex justify-end"><Button variant="primary" onClick={onClose}>Закрыть</Button></div>
        </div>
      )}
    </Modal>
  );
}

function ServerCard({ server, onOpen, onToggle }: { server: McpServer; onOpen: () => void; onToggle: (v: boolean) => void }) {
  const health = mcpHealth(server);
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen()}
      className={clsx(
        "cursor-pointer rounded-2xl border border-white/[0.08] bg-ink-900/70 p-5 transition-all hover:-translate-y-px hover:border-accent/35",
        (!server.enabled || server.overridden) && "opacity-60"
      )}
    >
      <div className="flex items-start gap-3">
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent/40 to-accent-glow/20 text-white ring-1 ring-white/10"><PlugIcon /></span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-[15px] font-semibold text-white">{server.name}</p>
          <p className="line-clamp-2 text-[12.5px] text-slate-400">{server.description || (server.transport === "url" ? server.url : server.command)}</p>
        </div>
        {server.editable && <Switch checked={server.enabled} onChange={onToggle} label={server.enabled ? "Выключить" : "Включить"} />}
      </div>
      <div className="mt-4 flex items-center gap-2 text-[12px]">
        <span className={clsx("h-2 w-2 shrink-0 rounded-full", HEALTH_DOT[health])} aria-hidden="true" />
        <span className="text-slate-300">{healthText(server)}</span>
        <span className="ml-auto flex gap-1.5">
          {server.shared && <span className="rounded-md bg-signal-warn/10 px-2 py-0.5 text-[11px] text-signal-warn">общий</span>}
          {server.overridden && <span className="rounded-md bg-signal-warn/10 px-2 py-0.5 text-[11px] text-signal-warn">заменён вашим</span>}
          <span className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] text-slate-400">{server.transport === "url" ? "адрес" : "команда"}</span>
        </span>
      </div>
    </div>
  );
}

/** «MCP-серверы»: external tools for the agents. Cards with a switch; a card opens the server; «Из каталога» adds one step by step. */
export default function Mcp({ embedded = false, onChanged }: { embedded?: boolean; onChanged?: () => void }) {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [list, setList] = useState<McpServer[] | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [form, setForm] = useState<McpInput>(EMPTY);
  const [wizard, setWizard] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const rows = await listMcp();
      setList(rows);
      return rows;
    } catch (e) {
      setError(errorMessage(e));
      return null;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const changed = async () => {
    await load();
    onChanged?.();
  };

  const current = openId !== null ? list?.find((s) => s.id === openId) ?? null : null;
  const readOnly = current !== null && !current.editable;

  const open = (s: McpServer) => {
    setOpenId(s.id);
    setForm({
      name: s.name, description: s.description, transport: s.transport, command: s.command ?? "", url: s.url ?? "",
      env: s.env_names.map((name) => ({ name, value: null })), headers: s.header_names.map((name) => ({ name, value: null })),
      bearer_token: null, scope: s.scope, enabled: s.enabled, shared: s.shared, startup_timeout_sec: s.startup_timeout_sec, tool_timeout_sec: s.tool_timeout_sec,
    });
    setError(null);
  };

  const run = async (name: string, fn: () => Promise<void>) => {
    setBusy(name);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const toggle = (s: McpServer, enabled: boolean) => void run(`t${s.id}`, async () => {
    await updateMcp(s.id, { enabled });
    await changed();
  });

  const save = (e: FormEvent) => {
    e.preventDefault();
    if (!current) return;
    void run("save", async () => {
      const saved = await updateMcp(current.id, { ...body(form), shared: undefined });
      setList((l) => l?.map((s) => (s.id === saved.id ? saved : s)) ?? l);
      setBusy("check");
      await checkMcp(saved.id);
      await changed();
    });
  };

  const remove = () => {
    if (!current || !window.confirm(`Удалить MCP-сервер ${current.name}?`)) return;
    void run("delete", async () => {
      await deleteMcp(current.id);
      setOpenId(null);
      await changed();
    });
  };

  const own = list?.filter((s) => !s.shared) ?? [];
  const shared = list?.filter((s) => s.shared) ?? [];
  const addButton = <Button variant="primary" onClick={() => setWizard(true)}>+ Из каталога</Button>;

  return (
    <>
      {embedded ? (
        !current && (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <p className="max-w-3xl text-sm text-slate-400">Внешние инструменты агента: поиск, Shodan, GitHub, Burp и другие. Переключатель на карточке включает и выключает, клик открывает подробности.</p>
            {addButton}
          </div>
        )
      ) : (
        <PageHeader title="MCP-серверы" subtitle="Внешние инструменты для агента." action={addButton} />
      )}

      {list === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}
      {!current && <ErrorNote message={error} />}

      {list !== null && list.length === 0 && (
        <EmptyState
          title="MCP-серверов пока нет"
          text="Подключите готовый из каталога — поиск в интернете, Shodan, VirusTotal, GitHub — или свой: команду или адрес работающего сервера (Burp, Caido)."
          action={<Button variant="primary" onClick={() => setWizard(true)}>Открыть каталог</Button>}
        />
      )}

      {list !== null && list.length > 0 && !current && (
        <div className="space-y-6">
          {[["Мои", own], ["Общие — от администратора", shared]].map(([title, rows]) =>
            (rows as McpServer[]).length > 0 ? (
              <section key={title as string}>
                <p className="mb-2.5 px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">{title as string}</p>
                <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
                  {(rows as McpServer[]).map((s) => <ServerCard key={s.id} server={s} onOpen={() => open(s)} onToggle={(v) => toggle(s, v)} />)}
                  {title === "Мои" && (
                    <button type="button" onClick={() => setWizard(true)} className="flex min-h-[8.5rem] flex-col items-center justify-center gap-1.5 rounded-2xl border border-dashed border-accent/40 bg-accent/[0.05] text-accent-soft transition-colors hover:bg-accent/10">
                      <span className="text-3xl leading-none">+</span>
                      <span className="text-sm font-medium">Добавить из каталога</span>
                      <span className="text-[11px] text-slate-500">или свой сервер</span>
                    </button>
                  )}
                </div>
              </section>
            ) : null
          )}
        </div>
      )}

      {current && (
        <div className="space-y-5">
          <button type="button" onClick={() => setOpenId(null)} className="text-sm text-accent-soft hover:underline">← Все серверы</button>
          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
            <Card className="p-6">
              <form onSubmit={save} className="space-y-4">
                <div className="flex flex-wrap items-center gap-3">
                  <span className={clsx("h-2.5 w-2.5 rounded-full", HEALTH_DOT[mcpHealth(current)])} aria-hidden="true" />
                  <h2 className="min-w-0 flex-1 truncate font-mono text-lg font-semibold text-white">{current.name}</h2>
                  {current.shared && <Badge tone="warn">общий</Badge>}
                  <Button type="button" loading={busy === "check"} onClick={() => void run("check", async () => { await checkMcp(current.id); await changed(); })}>Проверить</Button>
                </div>
                {readOnly && <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-2.5 text-xs text-slate-300">Общий сервер от администратора: агенты его получают, изменить его может только администратор.</p>}
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Имя">
                    <input className="field font-mono" value={form.name} required maxLength={32} pattern="[a-z][a-z0-9_\-]*" disabled={readOnly} onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} />
                  </Field>
                  <Field label="Коротко, для списка">
                    <input className="field" value={form.description} maxLength={200} disabled={readOnly} onChange={(e) => setForm({ ...form, description: e.target.value })} />
                  </Field>
                </div>
                <ServerFields form={form} setForm={setForm} readOnly={readOnly} hasToken={current.has_token} key={current.id} />
                {current.has_token && !readOnly && form.transport === "url" && (
                  <label className="flex items-center gap-2 text-xs text-slate-400">
                    <input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={form.bearer_token === ""} onChange={(e) => setForm({ ...form, bearer_token: e.target.checked ? "" : null })} />
                    убрать сохранённый токен
                  </label>
                )}
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="Каким агентам">
                    <select className="field" value={form.scope} disabled={readOnly} onChange={(e) => setForm({ ...form, scope: e.target.value as PersonaScope })}>
                      {(Object.keys(SCOPE_LABEL) as PersonaScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
                    </select>
                  </Field>
                  <Field label="Ждать запуска, с">
                    <input className="field" type="number" min={1} max={600} disabled={readOnly} value={form.startup_timeout_sec} onChange={(e) => setForm({ ...form, startup_timeout_sec: Number(e.target.value) || 30 })} />
                  </Field>
                  <Field label="Ждать инструмент, с">
                    <input className="field" type="number" min={1} max={3600} disabled={readOnly} value={form.tool_timeout_sec} onChange={(e) => setForm({ ...form, tool_timeout_sec: Number(e.target.value) || 120 })} />
                  </Field>
                </div>
                <ErrorNote message={error} />
                {!readOnly && (
                  <div className="flex flex-wrap justify-end gap-2 pt-1">
                    <Button type="button" variant="danger" onClick={remove} disabled={busy !== null}>Удалить</Button>
                    <Button type="submit" variant="primary" loading={busy === "save"}>Сохранить и проверить</Button>
                  </div>
                )}
              </form>
            </Card>
            <Card className="min-w-0 overflow-hidden">
              <div className="flex items-center justify-between border-b border-white/5 px-5 py-3">
                <h3 className="text-sm font-semibold text-white">Инструменты</h3>
                {current.last_check?.at && <span className="text-[11px] text-slate-500">проверено {new Date(current.last_check.at).toLocaleString("ru-RU")}</span>}
              </div>
              <ToolsList server={current} checking={busy === "check" || busy === "save"} />
            </Card>
          </div>
        </div>
      )}

      <Wizard open={wizard} onClose={() => setWizard(false)} onSaved={() => void changed()} isAdmin={isAdmin} taken={new Set(own.map((s) => s.name))} />
    </>
  );
}
