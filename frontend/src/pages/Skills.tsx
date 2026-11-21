import clsx from "clsx";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, PageHeader, Spinner, Switch } from "../components/ui";
import {
  createSkill, deleteSkill, deleteSkillFile, errorMessage, exportSkill, importSkills, listSkills, putSkillFile, readSkillFile, updateSkill,
  type Skill, type SkillScope,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { SCOPE_LABEL } from "../lib/personas";
import { SKILL_TEMPLATES, composeSkillMd, splitSkillMd, type SkillFields } from "../lib/workshop";
import { confirm } from "../lib/confirm";

type Mode = "fields" | "raw";
interface Draft {
  md: string;
  fields: SkillFields;
  scope: SkillScope;
  enabled: boolean;
  shared: boolean;
}

function draftOf(md: string, scope: SkillScope = "both", enabled = true, shared = false): Draft {
  return { md, fields: splitSkillMd(md), scope, enabled, shared };
}

function size(n: number): string {
  return n < 1024 ? `${n} Б` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} КБ` : `${(n / 1024 / 1024).toFixed(1)} МБ`;
}

function Item({ skill, active, onPick, onToggle, onDelete }: { skill: Skill; active: boolean; onPick: () => void; onToggle: (v: boolean) => void; onDelete: () => void }) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onPick}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onPick()}
      aria-current={active ? "true" : undefined}
      className={clsx(
        "w-full cursor-pointer rounded-2xl border px-4 py-3 text-left transition-all duration-200",
        active ? "border-accent/50 bg-gradient-to-br from-accent/[0.20] to-accent/[0.05] shadow-[0_10px_30px_-14px_rgba(124,92,255,0.8)]" : "border-white/[0.08] bg-ink-900/60 hover:-translate-y-px hover:border-white/15"
      )}
    >
      <span className="flex items-center gap-2">
        <span className={clsx("min-w-0 truncate font-mono text-[14px] font-semibold", skill.enabled && !skill.overridden ? "text-white" : "text-slate-500")}>{skill.name}</span>
        {skill.overridden && <span className="shrink-0 text-[10px] uppercase tracking-wider text-signal-warn">заменён вашим</span>}
        {skill.editable && (
          <span className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              title="Удалить навык"
              aria-label={`Удалить навык ${skill.name}`}
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-500 transition hover:bg-signal-bad/15 hover:text-signal-bad"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
              </svg>
            </button>
            <Switch checked={skill.enabled} onChange={onToggle} label={skill.enabled ? "Выключить" : "Включить"} />
          </span>
        )}
      </span>
      <span className={clsx("mt-0.5 line-clamp-2 block text-[12.5px] leading-snug", skill.enabled ? "text-slate-300" : "text-slate-500")}>{skill.description}</span>
      <span className="mt-1 block text-[11px] text-slate-500">
        {[SCOPE_LABEL[skill.scope], skill.files.length ? `файлов: ${skill.files.length + 1}` : "", skill.enabled ? "" : "выключен"].filter(Boolean).join(" · ")}
      </span>
    </div>
  );
}

/** «Навыки»: Agent Skills — SKILL.md plus its files. The agent sees every skill's name and description and reads the rest when needed. */
export default function Skills({ embedded = false, onChanged }: { embedded?: boolean; onChanged?: () => void }) {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [list, setList] = useState<Skill[] | null>(null);
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState<Draft>(draftOf(composeSkillMd(SKILL_TEMPLATES[0].fields)));
  const [mode, setMode] = useState<Mode>("fields");
  const [notes, setNotes] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [importShared, setImportShared] = useState(false);
  const [preview, setPreview] = useState<{ path: string; text: string | null } | null>(null);
  const [folder, setFolder] = useState("scripts");
  const importInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const rows = await listSkills();
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

  function pick(s: Skill, keepNotes: string[] = []) {
    setSelected(s.id);
    setDraft(draftOf(s.skill_md, s.scope, s.enabled, s.shared));
    setNotes(keepNotes);
    setPreview(null);
    setError(null);
  }

  const startNew = (fields: SkillFields = SKILL_TEMPLATES[0].fields) => {
    setSelected("new");
    setDraft(draftOf(composeSkillMd(fields)));
    setMode("fields");
    setNotes([]);
    setPreview(null);
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

  const reloadTo = async (id: number, keepNotes: string[] = []) => {
    const rows = await load();
    const fresh = rows?.find((s) => s.id === id);
    if (fresh) pick(fresh, keepNotes);
    onChanged?.();
  };

  // The two views edit one text: the fields are put into SKILL.md as they change, and SKILL.md is read back into fields.
  const setFields = (patch: Partial<SkillFields>) => {
    const fields = { ...draft.fields, ...patch };
    setDraft({ ...draft, fields, md: composeSkillMd(fields) });
  };
  const setMd = (md: string) => setDraft({ ...draft, md, fields: splitSkillMd(md) });

  const save = (e: FormEvent) => {
    e.preventDefault();
    void run("save", async () => {
      const saved = selected === "new"
        ? await createSkill({ skill_md: draft.md, scope: draft.scope, enabled: draft.enabled, shared: draft.shared })
        : await updateSkill(selected as number, { skill_md: draft.md, scope: draft.scope, enabled: draft.enabled });
      await reloadTo(saved.id, saved.notes);
    });
  };

  const toggle = (s: Skill, enabled: boolean) => void run(`t${s.id}`, async () => {
    await updateSkill(s.id, { enabled });
    const rows = await load();
    if (s.id === selected) {
      const fresh = rows?.find((r) => r.id === s.id);
      if (fresh) setDraft((d) => ({ ...d, enabled: fresh.enabled }));
    }
    onChanged?.();
  });

  const remove = async (target: Skill | null = current) => {
    if (!target || !(await confirm(`Удалить навык ${target.name}? Агенты перестанут его видеть со следующего сообщения.`))) return;
    void run("delete", async () => {
      await deleteSkill(target.id);
      const rows = await load();
      if (target.id === selected) {
        if (rows && rows.length > 0) pick(rows[0]);
        else setSelected(null);
      }
      onChanged?.();
    });
  };

  const onImport = (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    void run("import", async () => {
      const { skills: got, skipped } = await importSkills(file, importShared);
      const head = got.length > 1 ? [`Импортировано навыков: ${got.length} — ${got.map((s) => s.name).join(", ")}`] : [];
      const skip = skipped.length ? [`Пропущено ${skipped.length}: ${skipped.join("; ")}`] : [];
      if (got[0]) await reloadTo(got[0].id, [...head, ...skip, ...(got.length > 1 ? [] : got[0].notes)]);
    });
    if (importInput.current) importInput.current.value = "";
  };

  const onAddFile = (files: FileList | null) => {
    const file = files?.[0];
    if (!file || !current) return;
    const dir = folder.trim().replace(/^\/+|\/+$/g, "");
    void run("file", async () => {
      await putSkillFile(current.id, dir ? `${dir}/${file.name}` : file.name, file);
      await reloadTo(current.id);
    });
    if (fileInput.current) fileInput.current.value = "";
  };

  const own = list?.filter((s) => !s.shared) ?? [];
  const shared = list?.filter((s) => s.shared) ?? [];
  const actions = (
    <div className="flex flex-wrap items-center gap-2">
      {isAdmin && (
        <label className="flex items-center gap-2 text-xs text-slate-300">
          <input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={importShared} onChange={(e) => setImportShared(e.target.checked)} />
          импорт как общие
        </label>
      )}
      <input ref={importInput} type="file" accept=".zip,.md,text/markdown,application/zip" className="hidden" onChange={(e) => onImport(e.target.files)} />
      <Button loading={busy === "import"} onClick={() => importInput.current?.click()} title="Папка навыка из Claude Code, Codex, OpenHands или репозиторий с GitHub, упакованные в .zip, либо один SKILL.md">Импорт .zip / SKILL.md</Button>
      <Button variant="primary" onClick={() => startNew()}>+ Новый навык</Button>
    </div>
  );

  return (
    <>
      {embedded ? (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-3xl text-sm text-slate-400">Готовые приёмы для агента. Он видит название и «когда применять» каждого включённого навыка и сам берёт подходящий; назвать явно — <span className="font-mono text-accent-soft">$имя</span> в сообщении.</p>
          {actions}
        </div>
      ) : (
        <PageHeader title="Навыки" subtitle="Готовые приёмы для агента." action={actions} />
      )}

      {list === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {list !== null && list.length === 0 && selected !== "new" && (
        <EmptyState
          title="Навыков пока нет"
          text="Начните с шаблона или импортируйте готовый: папку навыка из Claude Code (~/.claude/skills), Codex (~/.codex/skills), OpenHands (~/.agents/skills) или репозиторий с GitHub, скачанный как .zip."
          action={<Button variant="primary" onClick={() => startNew()}>Создать навык</Button>}
        />
      )}

      {list !== null && (list.length > 0 || selected === "new") && (
        <div className="grid items-start gap-5 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="min-w-0 space-y-2.5">
            {own.length > 0 && <p className="px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Мои</p>}
            {own.map((s) => <Item key={s.id} skill={s} active={s.id === selected} onPick={() => pick(s)} onToggle={(v) => toggle(s, v)} onDelete={() => remove(s)} />)}
            {shared.length > 0 && <p className="px-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Общие — от администратора</p>}
            {shared.map((s) => <Item key={s.id} skill={s} active={s.id === selected} onPick={() => pick(s)} onToggle={(v) => toggle(s, v)} onDelete={() => remove(s)} />)}
            <p className="px-1 pt-2 text-[11px] leading-relaxed text-slate-500">Свой навык с тем же именем, что у общего, заменяет общий. Изменения доходят до агента с вашим следующим сообщением.</p>
          </div>

          {selected !== null && (
            <div className={clsx("grid min-w-0 items-start gap-5", selected === "new" && "2xl:grid-cols-[minmax(0,1fr)_17rem]")}>
              <div className="min-w-0 space-y-5">
                <Card className="p-6">
                  <form onSubmit={save} className="space-y-4">
                    <div className="flex flex-wrap items-center gap-3">
                      <h2 className="min-w-0 flex-1 truncate font-mono text-lg font-semibold text-white">{selected === "new" ? "Новый навык" : current?.name}</h2>
                      {current?.shared && <Badge tone="warn">общий</Badge>}
                      <div className="flex rounded-xl border border-white/10 p-0.5 text-xs" role="tablist" aria-label="Вид редактора">
                        {([["fields", "По полям"], ["raw", "SKILL.md"]] as const).map(([m, t]) => (
                          <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => setMode(m)} className={clsx("rounded-lg px-3 py-1 transition-colors", mode === m ? "bg-accent/25 text-white" : "text-slate-400 hover:text-white")}>{t}</button>
                        ))}
                      </div>
                      {current && <Button type="button" onClick={() => void exportSkill(current.id, current.name)}>Скачать .zip</Button>}
                    </div>
                    {readOnly && <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-2.5 text-xs text-slate-300">Это общий навык: агенты его получают, а изменить его может только администратор. Чтобы поправить под себя — скачайте и импортируйте как свой, он заменит общий.</p>}
                    {notes.length > 0 && (
                      <ul className="space-y-1 rounded-lg border border-signal-warn/25 bg-signal-warn/[0.07] px-3.5 py-2.5 text-xs text-slate-200">
                        {notes.map((n) => <li key={n}>• {n}</li>)}
                      </ul>
                    )}

                    {mode === "fields" ? (
                      <>
                        <div className="grid gap-4 sm:grid-cols-2">
                          <Field label="Имя" hint="Латиница, цифры, дефисы. Явный вызов: $имя">
                            <input className="field font-mono text-sm" value={draft.fields.name} required maxLength={64} disabled={readOnly} onChange={(e) => setFields({ name: e.target.value })} placeholder="jwt-checks" />
                          </Field>
                          <Field label="Каким агентам">
                            <select className="field" value={draft.scope} disabled={readOnly} onChange={(e) => setDraft({ ...draft, scope: e.target.value as SkillScope })}>
                              {(Object.keys(SCOPE_LABEL) as SkillScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
                            </select>
                          </Field>
                        </div>
                        <Field label="Когда применять" hint={`Агент видит это всегда и по этому решает, брать ли навык: пишите, в какой ситуации он нужен. ${draft.fields.description.length}/1024`}>
                          <textarea className="field min-h-[4.5rem] text-[13px] leading-relaxed" value={draft.fields.description} required maxLength={1024} disabled={readOnly} onChange={(e) => setFields({ description: e.target.value })} placeholder="Когда в запросах или ответах встречается JWT…" />
                        </Field>
                        <Field label="Что делать" hint="Шаги и правила — Markdown. Агент читает их, когда берёт навык. Файлы навыка упоминайте по пути: scripts/run.sh">
                          <textarea className="field min-h-[16rem] font-mono text-xs leading-relaxed" value={draft.fields.body} disabled={readOnly} spellCheck={false} onChange={(e) => setFields({ body: e.target.value })} />
                        </Field>
                      </>
                    ) : (
                      <>
                        <Field label="SKILL.md" hint={`Целиком, как в других инструментах: шапка между --- (name, description) и инструкции. ${draft.md.length}/600000`}>
                          <textarea className="field min-h-[26rem] font-mono text-xs leading-relaxed" value={draft.md} maxLength={600000} required disabled={readOnly} spellCheck={false} onChange={(e) => setMd(e.target.value)} />
                        </Field>
                        <Field label="Каким агентам">
                          <select className="field max-w-xs" value={draft.scope} disabled={readOnly} onChange={(e) => setDraft({ ...draft, scope: e.target.value as SkillScope })}>
                            {(Object.keys(SCOPE_LABEL) as SkillScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
                          </select>
                        </Field>
                      </>
                    )}

                    <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                      <label className="flex items-center gap-3 text-sm text-slate-300">
                        <Switch checked={draft.enabled} disabled={readOnly} onChange={(v) => setDraft({ ...draft, enabled: v })} label="Включён" />
                        Включён — агенты его получают
                      </label>
                      {selected === "new" && isAdmin && (
                        <label className="flex items-center gap-3 text-sm text-slate-300">
                          <input type="checkbox" className="h-4 w-4 accent-accent" checked={draft.shared} onChange={(e) => setDraft({ ...draft, shared: e.target.checked })} />
                          Общий: для всех пользователей
                        </label>
                      )}
                    </div>

                    <ErrorNote message={error} />
                    {!readOnly && (
                      <div className="flex flex-wrap justify-end gap-2 pt-1">
                        {current && <Button type="button" variant="danger" onClick={() => remove()} disabled={busy !== null}>Удалить</Button>}
                        <Button type="submit" variant="primary" loading={busy === "save"}>{selected === "new" ? "Создать" : "Сохранить"}</Button>
                      </div>
                    )}
                  </form>
                </Card>

                {current && (
                  <Card className="min-w-0 overflow-hidden">
                    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/5 px-5 py-3">
                      <h3 className="text-sm font-semibold text-white">Файлы навыка</h3>
                      {!readOnly && (
                        <div className="flex items-center gap-2">
                          <input className="field h-8 w-32 py-1 font-mono text-xs" value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="папка" aria-label="Папка для файла" />
                          <input ref={fileInput} type="file" className="hidden" onChange={(e) => onAddFile(e.target.files)} />
                          <Button loading={busy === "file"} onClick={() => fileInput.current?.click()}>Добавить файл</Button>
                        </div>
                      )}
                    </div>
                    <ul className="divide-y divide-white/5 text-sm">
                      <li className="flex items-center gap-3 px-5 py-2.5 font-mono text-xs text-slate-400">SKILL.md <span className="text-slate-500">— в редакторе выше</span></li>
                      {current.files.map((f) => (
                        <li key={f.path} className="flex items-center gap-3 px-5 py-2.5">
                          <button type="button" className="min-w-0 flex-1 truncate text-left font-mono text-xs text-accent-soft hover:underline" onClick={() => void run("preview", async () => setPreview(await readSkillFile(current.id, f.path)))}>
                            {f.path}
                          </button>
                          <span className="shrink-0 text-[11px] text-slate-500">{size(f.size)}</span>
                          {!readOnly && (
                            <button type="button" className="shrink-0 text-[11px] text-signal-bad hover:underline" onClick={() => void confirm(`Удалить ${f.path}?`).then((ok) => void (ok && run("rm", async () => { await deleteSkillFile(current.id, f.path); await reloadTo(current.id); })))}>
                              удалить
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                    {current.files.length === 0 && <p className="px-5 pb-4 text-xs text-slate-500">Скрипты, шаблоны и справочники кладите рядом: в «Что делать» ссылайтесь на них по пути, например scripts/run.sh — агент запустит их сам.</p>}
                    {preview && (
                      <div className="border-t border-white/5">
                        <div className="flex items-center justify-between px-5 py-2 text-xs text-slate-400">
                          <span className="font-mono">{preview.path}</span>
                          <button type="button" className="hover:text-white" onClick={() => setPreview(null)}>закрыть</button>
                        </div>
                        <pre className="max-h-[24rem] overflow-auto whitespace-pre-wrap px-5 pb-4 font-mono text-[11px] leading-relaxed text-slate-300 [overflow-wrap:anywhere]">{preview.text ?? "Это не текстовый файл — его содержимое здесь не показывается."}</pre>
                      </div>
                    )}
                  </Card>
                )}
              </div>

              {selected === "new" && (
                <Card className="p-5">
                  <p className="mb-3 text-sm font-semibold text-white">Начать с шаблона</p>
                  <div className="space-y-2">
                    {SKILL_TEMPLATES.map((t) => (
                      <button key={t.title} type="button" onClick={() => startNew(t.fields)} className={clsx("w-full rounded-xl border px-3 py-2.5 text-left transition-colors hover:border-accent/40", draft.fields.name === t.fields.name ? "border-accent/50 bg-accent/[0.12]" : "border-white/[0.08] bg-ink-800/60")}>
                        <span className="block text-[13px] text-white">{t.title}</span>
                        <span className="block text-[11.5px] text-slate-400">{t.hint}</span>
                      </button>
                    ))}
                  </div>
                  <p className="mt-4 text-[11.5px] leading-relaxed text-slate-500">Или импортируйте готовый навык из другого инструмента кнопкой «Импорт».</p>
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
