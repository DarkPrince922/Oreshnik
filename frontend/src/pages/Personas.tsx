import clsx from "clsx";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Button, Card, EmptyState, ErrorNote, Field, PageHeader, Spinner } from "../components/ui";
import { createPersona, deletePersona, errorMessage, listPersonas, listSkills, updatePersona, type Persona, type PersonaInput, type PersonaScope, type Skill } from "../lib/api";
import { useAuth } from "../lib/auth";
import { SCOPE_LABEL, personaInitial } from "../lib/personas";
import { confirm } from "../lib/confirm";

const EMPTY: PersonaInput = { name: "", description: "", instructions: "", scope: "both", shared: false, is_default: false, skills: null };

const EXAMPLE = `Ты — опытный специалист по тестированию на проникновение.
Отвечай по-русски, коротко и по делу.
Перед каждым действием формулируй гипотезу, после — что подтвердилось.
Работай только с целями, которые назвал пользователь.
Перед удалением файлов и остановкой сервисов спрашивай подтверждение.
В конце задачи пиши итог: что сделано, что найдено, что осталось.`;

function Badge({ name, shared }: { name: string; shared: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={clsx(
        "flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-[15px] font-semibold ring-1 ring-inset ring-white/10",
        shared ? "bg-signal-warn/15 text-signal-warn" : "bg-gradient-to-br from-accent/45 to-accent-glow/25 text-white"
      )}
    >
      {personaInitial(name)}
    </span>
  );
}

function Card_({ persona, active, onPick }: { persona: Persona; active: boolean; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={active ? "true" : undefined}
      className={clsx(
        "flex w-full gap-3 rounded-2xl border px-4 py-3.5 text-left transition-all duration-200",
        active ? "border-accent/50 bg-gradient-to-br from-accent/[0.20] to-accent/[0.05] shadow-[0_10px_30px_-14px_rgba(124,92,255,0.8)]" : "border-white/[0.08] bg-ink-900/60 hover:-translate-y-px hover:border-white/15"
      )}
    >
      <Badge name={persona.name} shared={persona.shared} />
      <span className="min-w-0">
        <span className="block truncate text-[15px] font-semibold text-white">{persona.name}</span>
        {persona.description && <span className="block truncate text-[13px] text-slate-300">{persona.description}</span>}
        <span className="mt-1 block text-[11px] text-slate-400">
          {[persona.is_default ? "по умолчанию" : "", SCOPE_LABEL[persona.scope], persona.skills ? `навыков: ${persona.skills.length}` : "", persona.sessions ? `сессий: ${persona.sessions}` : ""].filter(Boolean).join(" · ")}
        </span>
      </span>
    </button>
  );
}

/** Which skills an agent with the persona gets: every enabled one (null), or exactly the ones ticked — disabled ones too, so a skill
 * can be kept for one role only. */
function SkillPicker({ skills, value, disabled, onChange }: { skills: Skill[]; value: string[] | null; disabled: boolean; onChange: (v: string[] | null) => void }) {
  const names = [...new Set(skills.map((s) => s.name))].sort((a, b) => a.localeCompare(b));
  const byName = new Map(skills.map((s) => [s.name, s]));
  const chosen = new Set(value ?? []);
  const missing = (value ?? []).filter((n) => !byName.has(n)); // removed since: shown so they can be unticked
  return (
    <fieldset className="space-y-2.5 rounded-xl border border-white/[0.08] bg-white/[0.02] p-4" disabled={disabled}>
      <legend className="px-1 text-sm text-slate-300">Навыки этой личности</legend>
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Какие навыки получает агент">
        {([[null, "Все включённые"], [[], "Только выбранные"]] as const).map(([v, t]) => {
          const on = v === null ? value === null : value !== null;
          return (
            <button
              key={t}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => onChange(v === null ? null : value ?? [])}
              className={clsx("rounded-lg border px-3 py-1.5 text-xs transition-colors", on ? "border-accent/50 bg-accent/15 text-white" : "border-white/10 text-slate-400 hover:text-white")}
            >
              {t}
            </button>
          );
        })}
      </div>
      {value === null ? (
        <p className="text-[11.5px] leading-relaxed text-slate-500">Агент с этой личностью получает все навыки, включённые в «Мастерской».</p>
      ) : names.length === 0 && missing.length === 0 ? (
        <p className="text-[11.5px] text-slate-500">Навыков пока нет — добавьте их во вкладке «Навыки».</p>
      ) : (
        <>
          <p className="text-[11.5px] leading-relaxed text-slate-500">Агент получит ровно отмеченные — даже выключенные в общем списке. Так навык можно держать только для одной роли.</p>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {[...names, ...missing].map((n) => {
              const s = byName.get(n);
              return (
                <label key={n} className="flex min-w-0 items-start gap-2.5 rounded-lg px-2 py-1.5 text-sm hover:bg-white/[0.03]">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
                    checked={chosen.has(n)}
                    onChange={(e) => onChange(e.target.checked ? [...(value ?? []), n] : (value ?? []).filter((x) => x !== n))}
                  />
                  <span className="min-w-0">
                    <span className={clsx("block truncate font-mono text-[12.5px]", s ? "text-white" : "text-signal-warn")}>
                      {n}
                      {s && !s.enabled ? <span className="ml-1.5 font-sans text-[10.5px] text-slate-500">выкл.</span> : null}
                    </span>
                    <span className="block truncate text-[11px] text-slate-500">{s ? s.description : "навык удалён"}</span>
                  </span>
                </label>
              );
            })}
          </div>
        </>
      )}
    </fieldset>
  );
}

/** «Личности»: who an AI agent is — role, tone, language, rules. Picked when a session of «Код» (later «Агент») is created. */
export default function Personas({ embedded = false, onChanged }: { embedded?: boolean; onChanged?: () => void }) {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [list, setList] = useState<Persona[] | null>(null);
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [form, setForm] = useState<PersonaInput>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  useEffect(() => {
    listSkills().then(setSkills).catch(() => setSkills([]));
  }, []);

  const load = useCallback(async () => {
    try {
      const rows = await listPersonas();
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

  const current = typeof selected === "number" ? list?.find((p) => p.id === selected) ?? null : null;
  const readOnly = current !== null && !current.editable;

  function pick(p: Persona) {
    setSelected(p.id);
    setForm({ name: p.name, description: p.description, instructions: p.instructions, scope: p.scope, shared: p.shared, is_default: p.is_default, skills: p.skills });
    setError(null);
  }
  const startNew = () => {
    setSelected("new");
    setForm({ ...EMPTY, instructions: list && list.length > 0 ? "" : EXAMPLE });
    setError(null);
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const saved =
        selected === "new"
          ? await createPersona(form)
          : await updatePersona(selected as number, { name: form.name, description: form.description, instructions: form.instructions, scope: form.scope, is_default: form.is_default, skills: form.skills });
      const rows = await load();
      const fresh = rows?.find((p) => p.id === saved.id);
      if (fresh) pick(fresh);
      onChanged?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!current || !await confirm(`Удалить личность «${current.name}»? Сессии с ней продолжат работать без неё.`)) return;
    setBusy(true);
    try {
      await deletePersona(current.id);
      const rows = await load();
      if (rows && rows.length > 0) pick(rows[0]);
      else setSelected(null);
      onChanged?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const own = list?.filter((p) => !p.shared) ?? [];
  const shared = list?.filter((p) => p.shared) ?? [];

  return (
    <>
      {embedded ? (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-3xl text-sm text-slate-400">Кем быть агенту: роль, тон, язык ответов и правила. Выбирается при создании сессии; её текст уходит агенту первым. Личность «по умолчанию» подставляется в новые сессии сама.</p>
          <Button variant="primary" onClick={startNew}>+ Новая личность</Button>
        </div>
      ) : (
        <PageHeader
          title="Личности"
          subtitle="Кем быть агенту: роль, тон, язык ответов и правила. Выбирается при создании сессии в «Коде»; её текст уходит агенту первым."
          action={<Button variant="primary" onClick={startNew}>+ Новая личность</Button>}
        />
      )}

      {list === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {list !== null && list.length === 0 && selected !== "new" && (
        <EmptyState
          title="Личностей пока нет"
          text="Создайте первую: опишите, кем агенту быть и как работать. Например — пентестер, который всё документирует и спрашивает перед опасными действиями."
          action={<Button variant="primary" onClick={startNew}>Создать личность</Button>}
        />
      )}

      {list !== null && (list.length > 0 || selected === "new") && (
        <div className="grid items-start gap-5 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="min-w-0 space-y-2.5">
            {own.length > 0 && <p className="px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Мои</p>}
            {own.map((p) => <Card_ key={p.id} persona={p} active={p.id === selected} onPick={() => pick(p)} />)}
            {shared.length > 0 && <p className="px-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Общие — от администратора</p>}
            {shared.map((p) => <Card_ key={p.id} persona={p} active={p.id === selected} onPick={() => pick(p)} />)}
          </div>

          {selected !== null && (
            <Card className="min-w-0 p-6">
              <form onSubmit={save} className="space-y-4">
                <div className="flex items-center gap-3">
                  <Badge name={form.name || "?"} shared={Boolean(form.shared)} />
                  <h2 className="text-lg font-semibold text-white">{selected === "new" ? "Новая личность" : form.name}</h2>
                </div>
                {readOnly && <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-2.5 text-xs text-slate-300">Это общая личность: её можно выбрать в сессии, а изменить — только администратору.</p>}

                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Имя">
                    <input className="field" value={form.name} maxLength={80} required disabled={readOnly} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Пентестер" />
                  </Field>
                  <Field label="Коротко, для списка">
                    <input className="field" value={form.description} maxLength={200} disabled={readOnly} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Методичный, всё документирует" />
                  </Field>
                </div>

                <Field label="Кто ты и как работаешь" hint={`Уходит агенту первым, до инструкций самой сессии. Изменения доходят до идущих сессий с вашим следующим сообщением. ${form.instructions.length}/20000`}>
                  <textarea
                    className="field min-h-[12rem] font-mono text-xs leading-relaxed"
                    value={form.instructions}
                    maxLength={20000}
                    required
                    disabled={readOnly}
                    onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                    placeholder={EXAMPLE}
                  />
                </Field>

                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Где доступна">
                    <select className="field" value={form.scope} disabled={readOnly} onChange={(e) => setForm({ ...form, scope: e.target.value as PersonaScope })}>
                      {(Object.keys(SCOPE_LABEL) as PersonaScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
                    </select>
                  </Field>
                  {selected === "new" && isAdmin && (
                    <label className="flex items-center gap-3 self-end pb-3 text-sm text-slate-300">
                      <input type="checkbox" className="h-4 w-4 accent-accent" checked={Boolean(form.shared)} onChange={(e) => setForm({ ...form, shared: e.target.checked, is_default: e.target.checked ? false : form.is_default })} />
                      Общая: видна всем пользователям
                    </label>
                  )}
                </div>

                <SkillPicker skills={skills} value={form.skills ?? null} disabled={readOnly} onChange={(v) => setForm({ ...form, skills: v })} />

                {!form.shared && !readOnly && (
                  <label className="flex items-center gap-3 text-sm text-slate-300">
                    <input type="checkbox" className="h-4 w-4 accent-accent" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
                    Использовать по умолчанию для новых сессий
                  </label>
                )}

                <ErrorNote message={error} />
                {!readOnly && (
                  <div className="flex flex-wrap justify-end gap-2 pt-1">
                    {current && <Button type="button" variant="danger" onClick={remove} disabled={busy}>Удалить</Button>}
                    <Button type="submit" variant="primary" loading={busy}>{selected === "new" ? "Создать" : "Сохранить"}</Button>
                  </div>
                )}
              </form>
            </Card>
          )}
        </div>
      )}
      {list === null && <ErrorNote message={error} />}
    </>
  );
}
