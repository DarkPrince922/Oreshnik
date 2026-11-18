import clsx from "clsx";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, PageHeader, Spinner } from "../components/ui";
import {
  createSkill, deleteSkill, deleteSkillFile, errorMessage, exportSkill, importSkills, listSkills, putSkillFile, readSkillFile, updateSkill,
  type Skill, type SkillInput, type SkillScope,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { SCOPE_LABEL } from "../lib/personas";

const TEMPLATE = `---
name: subdomain-recon
description: Разведка поддоменов цели. Применяй, когда нужно найти поддомены и живые хосты домена.
---

# Разведка поддоменов

## Шаги
1. Собери поддомены: \`subfinder -d <домен> -silent\`.
2. Проверь, какие живы: \`httpx -silent\` по полученному списку.
3. Сохрани результат в recon/<домен>.txt и кратко перечисли интересное.

## Правила
- Работай только с доменом, который назвал пользователь.
`;

const EMPTY: SkillInput = { skill_md: TEMPLATE, scope: "both", enabled: true, shared: false };

function size(n: number): string {
  return n < 1024 ? `${n} Б` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} КБ` : `${(n / 1024 / 1024).toFixed(1)} МБ`;
}

function Item({ skill, active, onPick }: { skill: Skill; active: boolean; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={active ? "true" : undefined}
      className={clsx(
        "w-full rounded-2xl border px-4 py-3 text-left transition-all duration-200",
        active ? "border-accent/50 bg-gradient-to-br from-accent/[0.20] to-accent/[0.05] shadow-[0_10px_30px_-14px_rgba(124,92,255,0.8)]" : "border-white/[0.08] bg-ink-900/60 hover:-translate-y-px hover:border-white/15",
        !skill.enabled || skill.overridden ? "opacity-60" : ""
      )}
    >
      <span className="flex items-center gap-2">
        <span className="min-w-0 truncate font-mono text-[14px] font-semibold text-white">{skill.name}</span>
        {!skill.enabled && <span className="shrink-0 text-[10px] uppercase tracking-wider text-slate-500">выкл</span>}
        {skill.overridden && <span className="shrink-0 text-[10px] uppercase tracking-wider text-signal-warn">заменён вашим</span>}
      </span>
      <span className="mt-0.5 line-clamp-2 block text-[12.5px] leading-snug text-slate-300">{skill.description}</span>
      <span className="mt-1 block text-[11px] text-slate-400">
        {[SCOPE_LABEL[skill.scope], skill.files.length ? `файлов: ${skill.files.length + 1}` : ""].filter(Boolean).join(" · ")}
      </span>
    </button>
  );
}

/** «Навыки»: Agent Skills — SKILL.md plus its files. The agent sees every skill's name and description and reads the rest when needed. */
export default function Skills() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [list, setList] = useState<Skill[] | null>(null);
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [form, setForm] = useState<SkillInput>(EMPTY);
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
    setForm({ skill_md: s.skill_md, scope: s.scope, enabled: s.enabled, shared: s.shared });
    setNotes(keepNotes);
    setPreview(null);
    setError(null);
  }

  const startNew = () => {
    setSelected("new");
    setForm(EMPTY);
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

  const refreshTo = async (saved: Skill, keepNotes: string[] = saved.notes) => {
    const rows = await load();
    const fresh = rows?.find((s) => s.id === saved.id);
    if (fresh) pick(fresh, keepNotes);
  };

  const save = (e: FormEvent) => {
    e.preventDefault();
    void run("save", async () => {
      const saved = selected === "new" ? await createSkill(form) : await updateSkill(selected as number, { skill_md: form.skill_md, scope: form.scope, enabled: form.enabled });
      await refreshTo(saved);
    });
  };

  const remove = () => {
    if (!current || !window.confirm(`Удалить навык ${current.name}? Агенты перестанут его видеть со следующего сообщения.`)) return;
    void run("delete", async () => {
      await deleteSkill(current.id);
      const rows = await load();
      if (rows && rows.length > 0) pick(rows[0]);
      else setSelected(null);
    });
  };

  const onImport = (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    void run("import", async () => {
      const got = await importSkills(file, importShared);
      const rows = await load();
      const first = rows?.find((s) => s.id === got[0]?.id);
      if (first) pick(first, got.length > 1 ? [`Импортировано навыков: ${got.length} — ${got.map((s) => s.name).join(", ")}`, ...got[0].notes] : got[0].notes);
    });
    if (importInput.current) importInput.current.value = "";
  };

  const onAddFile = (files: FileList | null) => {
    const file = files?.[0];
    if (!file || !current) return;
    const dir = folder.trim().replace(/^\/+|\/+$/g, "");
    void run("file", async () => refreshTo(await putSkillFile(current.id, dir ? `${dir}/${file.name}` : file.name, file), []));
    if (fileInput.current) fileInput.current.value = "";
  };

  const own = list?.filter((s) => !s.shared) ?? [];
  const shared = list?.filter((s) => s.shared) ?? [];

  return (
    <>
      <PageHeader
        title="Навыки"
        subtitle="Готовые приёмы для агента: SKILL.md с описанием и инструкциями, плюс скрипты и шаблоны рядом. Агент видит список навыков и сам берёт подходящий; назвать навык явно — $имя в сообщении."
        action={
          <div className="flex flex-wrap items-center gap-2">
            {isAdmin && (
              <label className="flex items-center gap-2 text-xs text-slate-300">
                <input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={importShared} onChange={(e) => setImportShared(e.target.checked)} />
                импорт как общие
              </label>
            )}
            <input ref={importInput} type="file" accept=".zip,.md,text/markdown,application/zip" className="hidden" onChange={(e) => onImport(e.target.files)} />
            <Button loading={busy === "import"} onClick={() => importInput.current?.click()}>Импорт .zip / SKILL.md</Button>
            <Button variant="primary" onClick={startNew}>+ Новый навык</Button>
          </div>
        }
      />

      {list === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {list !== null && list.length === 0 && selected !== "new" && (
        <EmptyState
          title="Навыков пока нет"
          text="Напишите свой или импортируйте готовый: папку навыка из Claude Code (~/.claude/skills), Codex (~/.codex/skills), OpenHands (~/.agents/skills) или репозиторий с GitHub, скачанный как .zip. Можно и один файл SKILL.md."
          action={<Button variant="primary" onClick={startNew}>Создать навык</Button>}
        />
      )}

      {list !== null && (list.length > 0 || selected === "new") && (
        <div className="grid items-start gap-5 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="min-w-0 space-y-2.5">
            {own.length > 0 && <p className="px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Мои</p>}
            {own.map((s) => <Item key={s.id} skill={s} active={s.id === selected} onPick={() => pick(s)} />)}
            {shared.length > 0 && <p className="px-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Общие — от администратора</p>}
            {shared.map((s) => <Item key={s.id} skill={s} active={s.id === selected} onPick={() => pick(s)} />)}
            <p className="px-1 pt-2 text-[11px] leading-relaxed text-slate-500">
              Свой навык с тем же именем, что у общего, заменяет общий. Изменения доходят до агента с вашим следующим сообщением.
            </p>
          </div>

          {selected !== null && (
            <div className="min-w-0 space-y-5">
              <Card className="p-6">
                <form onSubmit={save} className="space-y-4">
                  <div className="flex flex-wrap items-center gap-3">
                    <h2 className="min-w-0 flex-1 truncate font-mono text-lg font-semibold text-white">{selected === "new" ? "Новый навык" : current?.name}</h2>
                    {current?.shared && <Badge tone="warn">общий</Badge>}
                    {current && <Button type="button" onClick={() => void exportSkill(current.id, current.name)}>Скачать .zip</Button>}
                  </div>
                  {readOnly && <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-2.5 text-xs text-slate-300">Это общий навык: агенты его получают, а изменить его может только администратор. Чтобы поправить под себя — скачайте и импортируйте как свой, он заменит общий.</p>}
                  {notes.length > 0 && (
                    <ul className="space-y-1 rounded-lg border border-signal-warn/25 bg-signal-warn/[0.07] px-3.5 py-2.5 text-xs text-slate-200">
                      {notes.map((n) => <li key={n}>• {n}</li>)}
                    </ul>
                  )}

                  <Field
                    label="SKILL.md"
                    hint={`В шапке между --- обязательны name (латиница, цифры, дефисы) и description — по описанию агент решает, когда брать навык, пишите в нём «когда применять». Ниже — сами инструкции. ${form.skill_md.length}/100000`}
                  >
                    <textarea
                      className="field min-h-[22rem] font-mono text-xs leading-relaxed"
                      value={form.skill_md}
                      maxLength={100000}
                      required
                      disabled={readOnly}
                      spellCheck={false}
                      onChange={(e) => setForm({ ...form, skill_md: e.target.value })}
                    />
                  </Field>

                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label="Каким агентам">
                      <select className="field" value={form.scope} disabled={readOnly} onChange={(e) => setForm({ ...form, scope: e.target.value as SkillScope })}>
                        {(Object.keys(SCOPE_LABEL) as SkillScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
                      </select>
                    </Field>
                    <div className="flex flex-col justify-end gap-2 pb-2">
                      <label className="flex items-center gap-3 text-sm text-slate-300">
                        <input type="checkbox" className="h-4 w-4 accent-accent" checked={form.enabled} disabled={readOnly} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                        Включён — агенты его получают
                      </label>
                      {selected === "new" && isAdmin && (
                        <label className="flex items-center gap-3 text-sm text-slate-300">
                          <input type="checkbox" className="h-4 w-4 accent-accent" checked={Boolean(form.shared)} onChange={(e) => setForm({ ...form, shared: e.target.checked })} />
                          Общий: для всех пользователей
                        </label>
                      )}
                    </div>
                  </div>

                  <ErrorNote message={error} />
                  {!readOnly && (
                    <div className="flex flex-wrap justify-end gap-2 pt-1">
                      {current && <Button type="button" variant="danger" onClick={remove} disabled={busy !== null}>Удалить</Button>}
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
                          <button type="button" className="shrink-0 text-[11px] text-signal-bad hover:underline" onClick={() => window.confirm(`Удалить ${f.path}?`) && void run("rm", async () => refreshTo(await deleteSkillFile(current.id, f.path), []))}>
                            удалить
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                  {current.files.length === 0 && <p className="px-5 pb-4 text-xs text-slate-500">Скрипты, шаблоны и справочники кладите рядом: в SKILL.md ссылайтесь на них по пути, например scripts/run.sh — агент запустит их сам.</p>}
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
          )}
        </div>
      )}
      {list === null && <ErrorNote message={error} />}
    </>
  );
}
