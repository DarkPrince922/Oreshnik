import clsx from "clsx";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, PageHeader, Spinner } from "../components/ui";
import { checkMcp, createMcp, deleteMcp, errorMessage, listMcp, updateMcp, type McpInput, type McpSecret, type McpServer, type PersonaScope } from "../lib/api";
import { useAuth } from "../lib/auth";
import { SCOPE_LABEL } from "../lib/personas";

const EMPTY: McpInput = {
  name: "", description: "", transport: "command", command: "", url: "", env: [], headers: [], bearer_token: null,
  scope: "both", enabled: true, shared: false, startup_timeout_sec: 30, tool_timeout_sec: 120,
};

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
            <input className="field w-2/5 font-mono text-xs" value={it.name} disabled={disabled} placeholder={namePlaceholder} onChange={(e) => set(i, { name: e.target.value.trim() })} />
            <input
              className="field min-w-0 flex-1 font-mono text-xs"
              type="password"
              autoComplete="new-password"
              value={it.value ?? ""}
              disabled={disabled}
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

function Item({ server, active, onPick }: { server: McpServer; active: boolean; onPick: () => void }) {
  const check = server.last_check;
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={active ? "true" : undefined}
      className={clsx(
        "w-full rounded-2xl border px-4 py-3 text-left transition-all duration-200",
        active ? "border-accent/50 bg-gradient-to-br from-accent/[0.20] to-accent/[0.05] shadow-[0_10px_30px_-14px_rgba(124,92,255,0.8)]" : "border-white/[0.08] bg-ink-900/60 hover:-translate-y-px hover:border-white/15",
        !server.enabled || server.overridden ? "opacity-60" : ""
      )}
    >
      <span className="flex items-center gap-2">
        <span className={clsx("h-2 w-2 shrink-0 rounded-full", !check ? "bg-slate-500" : check.ok ? "bg-signal-ok" : "bg-signal-bad")} aria-hidden="true" />
        <span className="min-w-0 truncate font-mono text-[14px] font-semibold text-white">{server.name}</span>
        {!server.enabled && <span className="shrink-0 text-[10px] uppercase tracking-wider text-slate-500">выкл</span>}
        {server.overridden && <span className="shrink-0 text-[10px] uppercase tracking-wider text-signal-warn">заменён вашим</span>}
      </span>
      <span className="mt-0.5 block truncate font-mono text-[11.5px] text-slate-400">{server.transport === "url" ? server.url : server.command}</span>
      <span className="mt-1 block text-[11px] text-slate-400">
        {[SCOPE_LABEL[server.scope], check ? (check.ok ? `инструментов: ${check.tools.length}` : "ошибка запуска") : "не проверен"].join(" · ")}
      </span>
    </button>
  );
}

/** «MCP-серверы»: external tools for the agents. A command runs inside the agent container; a URL is a server somewhere else. */
export default function Mcp() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [list, setList] = useState<McpServer[] | null>(null);
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [form, setForm] = useState<McpInput>(EMPTY);
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
    void load().then((rows) => rows && rows.length > 0 && pick(rows[0]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  const current = typeof selected === "number" ? list?.find((s) => s.id === selected) ?? null : null;
  const readOnly = current !== null && !current.editable;

  function pick(s: McpServer) {
    setSelected(s.id);
    setForm({
      name: s.name, description: s.description, transport: s.transport, command: s.command ?? "", url: s.url ?? "",
      env: s.env_names.map((name) => ({ name, value: null })), headers: s.header_names.map((name) => ({ name, value: null })),
      bearer_token: null, scope: s.scope, enabled: s.enabled, shared: s.shared, startup_timeout_sec: s.startup_timeout_sec, tool_timeout_sec: s.tool_timeout_sec,
    });
    setError(null);
  }

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

  const refreshTo = async (saved: McpServer) => {
    const rows = await load();
    const fresh = rows?.find((s) => s.id === saved.id);
    if (fresh) pick(fresh);
  };

  const save = (e: FormEvent) => {
    e.preventDefault();
    const body: McpInput = {
      ...form,
      name: form.name.trim(),
      command: form.transport === "command" ? (form.command ?? "").trim() : null,
      url: form.transport === "url" ? (form.url ?? "").trim() : null,
      env: form.transport === "command" ? form.env.filter((s) => s.name) : [],
      headers: form.transport === "url" ? form.headers.filter((s) => s.name) : [],
    };
    void run("save", async () => {
      const saved = selected === "new" ? await createMcp(body) : await updateMcp(selected as number, { ...body, shared: undefined });
      // a new or changed server is checked right away: the user sees its tools, or why it does not start
      await refreshTo(saved.last_check ? saved : await checkMcp(saved.id));
    });
  };

  const remove = () => {
    if (!current || !window.confirm(`Удалить MCP-сервер ${current.name}?`)) return;
    void run("delete", async () => {
      await deleteMcp(current.id);
      const rows = await load();
      if (rows && rows.length > 0) pick(rows[0]);
      else setSelected(null);
    });
  };

  const own = list?.filter((s) => !s.shared) ?? [];
  const shared = list?.filter((s) => s.shared) ?? [];
  const check = current?.last_check ?? null;

  return (
    <>
      <PageHeader
        title="MCP-серверы"
        subtitle="Внешние инструменты для агента: Burp, Caido, браузер, базы, API. Агент видит их инструменты рядом со своими и вызывает сам. Работает и через шлюзы, принимающие только обычные функции."
        action={<Button variant="primary" onClick={() => { setSelected("new"); setForm(EMPTY); setError(null); }}>+ Новый сервер</Button>}
      />

      {list === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {list !== null && list.length === 0 && selected !== "new" && (
        <EmptyState
          title="MCP-серверов пока нет"
          text="Добавьте команду, которую панель запустит рядом с агентом (например, npx -y @playwright/mcp@latest --headless), или адрес работающего MCP-сервера (например, Burp с расширением MCP)."
          action={<Button variant="primary" onClick={() => { setSelected("new"); setForm(EMPTY); }}>Добавить сервер</Button>}
        />
      )}

      {list !== null && (list.length > 0 || selected === "new") && (
        <div className="grid items-start gap-5 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="min-w-0 space-y-2.5">
            {own.length > 0 && <p className="px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Мои</p>}
            {own.map((s) => <Item key={s.id} server={s} active={s.id === selected} onPick={() => pick(s)} />)}
            {shared.length > 0 && <p className="px-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Общие — от администратора</p>}
            {shared.map((s) => <Item key={s.id} server={s} active={s.id === selected} onPick={() => pick(s)} />)}
            <p className="px-1 pt-2 text-[11px] leading-relaxed text-slate-500">Изменения доходят до агента с вашим следующим сообщением. Свой сервер с тем же именем заменяет общий.</p>
          </div>

          {selected !== null && (
            <div className="min-w-0 space-y-5">
              <Card className="p-6">
                <form onSubmit={save} className="space-y-4">
                  <div className="flex flex-wrap items-center gap-3">
                    <h2 className="min-w-0 flex-1 truncate font-mono text-lg font-semibold text-white">{selected === "new" ? "Новый MCP-сервер" : current?.name}</h2>
                    {current?.shared && <Badge tone="warn">общий</Badge>}
                    {current && <Button type="button" loading={busy === "check"} onClick={() => void run("check", async () => refreshTo(await checkMcp(current.id)))}>Проверить</Button>}
                  </div>
                  {readOnly && <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-2.5 text-xs text-slate-300">Это общий сервер от администратора: агенты его получают, изменить его может только администратор.</p>}

                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label="Имя" hint="Латиница, цифры, _ и -. Модель видит инструменты как mcp__имя__инструмент.">
                      <input className="field font-mono" value={form.name} required maxLength={32} pattern="[a-z][a-z0-9_\-]*" disabled={readOnly} onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} placeholder="burp" />
                    </Field>
                    <Field label="Коротко, для списка">
                      <input className="field" value={form.description} maxLength={200} disabled={readOnly} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Прокси Burp Suite" />
                    </Field>
                  </div>

                  <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Как подключить">
                    {([["command", "Команда — запустить рядом с агентом"], ["url", "Адрес — сервер уже работает"]] as const).map(([value, text]) => (
                      <button
                        key={value}
                        type="button"
                        role="radio"
                        aria-checked={form.transport === value}
                        disabled={readOnly}
                        onClick={() => setForm({ ...form, transport: value })}
                        className={clsx("rounded-xl border px-3.5 py-2 text-sm transition-colors", form.transport === value ? "border-accent/50 bg-accent/15 text-white" : "border-white/10 text-slate-300 hover:border-white/20")}
                      >
                        {text}
                      </button>
                    ))}
                  </div>

                  {form.transport === "command" ? (
                    <>
                      <Field label="Команда запуска" hint="Выполняется в контейнере агента, там есть node/npx, python3 и pip. Аргументы с пробелами берите в кавычки. Пример: npx -y @playwright/mcp@latest --headless">
                        <input className="field font-mono text-xs" value={form.command ?? ""} required disabled={readOnly} onChange={(e) => setForm({ ...form, command: e.target.value })} placeholder="npx -y @modelcontextprotocol/server-fetch" />
                      </Field>
                      <Secrets label="Переменные окружения" hint="Ключи API и прочее, что серверу нужно. Хранятся зашифрованными и обратно не показываются." items={form.env} onChange={(env) => setForm({ ...form, env })} namePlaceholder="SHODAN_API_KEY" disabled={readOnly} />
                    </>
                  ) : (
                    <>
                      <Field label="Адрес (streamable HTTP)" hint="Должен быть доступен из контейнера агента. Сервер на этом же компьютере — не localhost, а адрес компьютера в сети.">
                        <input className="field font-mono text-xs" value={form.url ?? ""} required disabled={readOnly} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="http://192.168.1.10:9876/mcp" />
                      </Field>
                      <Field label="Токен (Authorization: Bearer)" hint={current?.has_token ? "Токен сохранён. Впишите новый, чтобы заменить." : "Если сервер его просит. Хранится зашифрованным."}>
                        <input className="field font-mono text-xs" type="password" autoComplete="new-password" disabled={readOnly} value={form.bearer_token ?? ""} onChange={(e) => setForm({ ...form, bearer_token: e.target.value || null })} placeholder={current?.has_token ? "сохранён" : ""} />
                      </Field>
                      {current?.has_token && !readOnly && (
                        <label className="flex items-center gap-2 text-xs text-slate-400">
                          <input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={form.bearer_token === ""} onChange={(e) => setForm({ ...form, bearer_token: e.target.checked ? "" : null })} />
                          убрать сохранённый токен
                        </label>
                      )}
                      <Secrets label="Заголовки" hint="Например X-Api-Key. Значения хранятся зашифрованными." items={form.headers} onChange={(headers) => setForm({ ...form, headers })} namePlaceholder="X-Api-Key" disabled={readOnly} />
                    </>
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

                  <div className="flex flex-col gap-2">
                    <label className="flex items-center gap-3 text-sm text-slate-300">
                      <input type="checkbox" className="h-4 w-4 accent-accent" checked={form.enabled} disabled={readOnly} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                      Включён — агенты его получают
                    </label>
                    {selected === "new" && isAdmin && (
                      <label className="flex items-center gap-3 text-sm text-slate-300">
                        <input type="checkbox" className="h-4 w-4 accent-accent" checked={Boolean(form.shared)} onChange={(e) => setForm({ ...form, shared: e.target.checked })} />
                        Общий: для всех пользователей (их агенты получат и ваши ключи)
                      </label>
                    )}
                  </div>

                  <ErrorNote message={error} />
                  {!readOnly && (
                    <div className="flex flex-wrap justify-end gap-2 pt-1">
                      {current && <Button type="button" variant="danger" onClick={remove} disabled={busy !== null}>Удалить</Button>}
                      <Button type="submit" variant="primary" loading={busy === "save"}>{selected === "new" ? "Добавить и проверить" : "Сохранить и проверить"}</Button>
                    </div>
                  )}
                </form>
              </Card>

              {current && (
                <Card className="min-w-0 overflow-hidden">
                  <div className="flex items-center justify-between border-b border-white/5 px-5 py-3">
                    <h3 className="text-sm font-semibold text-white">Инструменты</h3>
                    {check?.at && <span className="text-[11px] text-slate-500">проверено {new Date(check.at).toLocaleString("ru-RU")}</span>}
                  </div>
                  {busy === "check" || busy === "save" ? (
                    <p className="flex items-center gap-2 px-5 py-4 text-xs text-slate-400"><Spinner className="h-3.5 w-3.5" /> Запускаю сервер в контейнере агента… первый запуск через npx скачивает пакет и может занять минуту.</p>
                  ) : !check ? (
                    <p className="px-5 py-4 text-xs text-slate-400">Ещё не проверен: нажмите «Проверить» — панель запустит сервер так же, как в сессии, и покажет его инструменты.</p>
                  ) : check.ok ? (
                    <ul className="divide-y divide-white/5">
                      {check.tools.map((t) => (
                        <li key={t.name} className="px-5 py-2.5">
                          <p className="font-mono text-xs text-accent-soft">{t.name}</p>
                          {t.description && <p className="mt-0.5 text-xs leading-relaxed text-slate-400">{t.description}</p>}
                        </li>
                      ))}
                      {check.tools.length === 0 && <li className="px-5 py-4 text-xs text-slate-400">Сервер запустился, но инструментов не предлагает.</li>}
                    </ul>
                  ) : (
                    <p className="px-5 py-4 text-xs leading-relaxed text-signal-bad [overflow-wrap:anywhere]">Не запустился: {check.error}</p>
                  )}
                </Card>
              )}
            </div>
          )}
        </div>
      )}
      {list === null && <ErrorNote message={error} />}
    </>
  );
}
