import clsx from "clsx";
import { motion } from "framer-motion";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import Markdown from "../components/Markdown";
import ModelSelect from "../components/ModelSelect";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner } from "../components/ui";
import {
  createAgentSession,
  deleteAgentSession,
  errorMessage,
  getAgentHealth,
  listAgentSessions,
  listProfiles,
  sendAgentMessage,
  stopAgentTurn,
  type AgentSession,
  type LLMProfile,
} from "../lib/api";
import { buildFeed, canSend, formatTokens, lastStepRunning, usableProfiles, type FeedEntry } from "../lib/agentModel";
import { defaultChoice, modelToSend, type Choice } from "../lib/modelChoice";
import { useAgentStream } from "../lib/useAgentStream";

function Pre({ children, className }: { children: string; className?: string }) {
  return (
    <pre className={clsx("max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-slate-300", className)}>
      {children}
    </pre>
  );
}

function Step({ entry }: { entry: FeedEntry }) {
  const [open, setOpen] = useState(false);
  switch (entry.type) {
    case "user":
      return (
        <div className="ml-auto max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md border border-accent/20 bg-accent/10 px-4 py-2.5 text-sm text-slate-100">
          {entry.text}
        </div>
      );
    case "message":
      return (
        <div className="max-w-[92%] text-sm">
          <Markdown>{entry.text}</Markdown>
        </div>
      );
    case "reasoning":
      return <p className="max-w-[92%] whitespace-pre-wrap break-words text-xs italic text-slate-500">{entry.text}</p>;
    case "command": {
      const failed = entry.exitCode !== null && entry.exitCode !== 0;
      return (
        <div className="max-w-[92%] rounded-xl border border-white/5 bg-ink-900/50 p-3">
          <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((o) => !o)}>
            {entry.running ? <Spinner className="h-3 w-3" /> : <span className={clsx("text-xs", failed ? "text-signal-bad" : "text-signal-ok")}>{failed ? "✕" : "✓"}</span>}
            <code className="min-w-0 flex-1 truncate font-mono text-xs text-slate-200">$ {entry.command}</code>
            {entry.exitCode !== null && entry.exitCode !== 0 && <span className="font-mono text-[11px] text-signal-bad">код {entry.exitCode}</span>}
          </button>
          {(open || entry.running) && entry.output && <Pre className="mt-2">{entry.output}</Pre>}
          {open && !entry.output && <p className="mt-2 text-[11px] text-slate-600">Вывода нет.</p>}
        </div>
      );
    }
    case "files":
      return (
        <div className="max-w-[92%] rounded-xl border border-white/5 bg-ink-900/50 p-3 text-xs">
          <div className="mb-1 flex items-center gap-2 text-slate-400">
            {entry.running && <Spinner className="h-3 w-3" />}
            Правки файлов
          </div>
          <ul className="space-y-0.5 font-mono text-[11px] text-slate-300">
            {entry.changes.map((c, i) => (
              <li key={i}>
                <span className="mr-2 text-slate-500">{c.kind}</span>
                {c.path}
              </li>
            ))}
          </ul>
        </div>
      );
    case "todo":
      return (
        <ul className="max-w-[92%] space-y-0.5 rounded-xl border border-white/5 bg-ink-900/50 p-3 text-xs">
          {entry.items.map((t, i) => (
            <li key={i} className={clsx(t.completed ? "text-slate-500 line-through" : "text-slate-300")}>
              {t.completed ? "☑" : "☐"} {t.text}
            </li>
          ))}
        </ul>
      );
    case "tool":
      return (
        <div className="flex max-w-[92%] items-center gap-2 rounded-xl border border-white/5 bg-ink-900/50 px-3 py-2 text-xs text-slate-400">
          {entry.running && <Spinner className="h-3 w-3" />}
          <span className="font-mono text-slate-300">{entry.title}</span>
          {entry.detail && <span className="min-w-0 truncate text-slate-500">{entry.detail}</span>}
        </div>
      );
    case "notice":
      return (
        <p className={clsx("max-w-[92%] whitespace-pre-wrap break-words text-xs", entry.tone === "bad" ? "text-signal-bad" : entry.tone === "warn" ? "text-signal-warn" : "text-slate-600")}>
          {entry.text}
        </p>
      );
    case "usage":
      return (
        <p className="text-[10px] text-slate-600">
          токенов: {formatTokens(entry.inputTokens)} на входе · {formatTokens(entry.outputTokens)} на выходе
        </p>
      );
  }
}

function Chat({ session, profileAvailable, onChanged }: { session: AgentSession; profileAvailable: boolean; onChanged: () => void }) {
  const { events, status, state } = useAgentStream(session.id);
  const feed = buildFeed(events);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [events.length, status]);

  // The list on the left shows the status too: refresh it when a turn starts or ends.
  const prevStatus = useRef(status);
  useEffect(() => {
    if (prevStatus.current !== status) onChanged();
    prevStatus.current = status;
  }, [status, onChanged]);

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!canSend({ text, status, profileAvailable }) || busy) return;
    setBusy(true);
    setError(null);
    try {
      await sendAgentMessage(session.id, text.trim());
      setText("");
      stick.current = true;
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    try {
      await stopAgentTurn(session.id);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
  };

  const running = status === "running";
  return (
    <Card className="flex h-[calc(100vh-14rem)] min-h-[28rem] flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/5 px-5 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-white">{session.title}</h2>
          <p className="text-[11px] text-slate-500">профиль: {session.llm_profile_name ?? "удалён"}{session.llm_model ? ` · ${session.llm_model}` : ""}</p>
        </div>
        <div className="flex items-center gap-2">
          {state !== "live" && <Badge tone={state === "error" ? "bad" : "warn"}>{state === "error" ? "нет связи" : "подключение…"}</Badge>}
          {running && <Badge tone="accent">думает…</Badge>}
          {running && (
            <Button variant="danger" className="px-3 py-1.5 text-xs" onClick={stop} disabled={session.cancel_requested}>
              Стоп
            </Button>
          )}
        </div>
      </div>

      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="flex-1 space-y-3 overflow-y-auto px-5 py-4"
      >
        {feed.length === 0 && state === "live" && (
          <p className="pt-10 text-center text-sm text-slate-500">Напишите задачу — агент будет выполнять команды и править файлы в своём контейнере.</p>
        )}
        {feed.map((entry) => (
          <Step key={entry.key} entry={entry} />
        ))}
        {running && !lastStepRunning(feed) && <Spinner className="h-3.5 w-3.5" />}
      </div>

      <form onSubmit={send} className="border-t border-white/5 p-4">
        <ErrorNote message={error} />
        {!profileAvailable && <p className="mb-2 text-xs text-signal-warn">Профиль LLM этой сессии удалён: создайте новую сессию.</p>}
        <div className="flex items-end gap-3">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            rows={2}
            maxLength={50000}
            placeholder={running ? "Агент отвечает… можно нажать «Стоп»" : "Что сделать? (Ctrl+Enter — отправить)"}
            className="min-h-[3.25rem] flex-1 resize-y rounded-xl border border-white/10 bg-ink-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/50"
          />
          <Button type="submit" variant="primary" loading={busy} disabled={!canSend({ text, status, profileAvailable })}>
            Отправить
          </Button>
        </div>
      </form>
    </Card>
  );
}

export default function Agent() {
  const [sessions, setSessions] = useState<AgentSession[] | null>(null);
  const [profiles, setProfiles] = useState<LLMProfile[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [health, setHealth] = useState<{ ok: boolean; detail: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [choice, setChoice] = useState<Choice | null>(null);
  const profileId = choice?.profileId ?? null;
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const list = await listAgentSessions();
      setSessions(list);
      setSelected((cur) => (cur !== null && list.some((s) => s.id === cur) ? cur : list[0]?.id ?? null));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    listProfiles().then(setProfiles).catch((e) => setError(errorMessage(e)));
    getAgentHealth().then(setHealth).catch(() => setHealth({ ok: false, detail: "Не удалось проверить агента" }));
  }, [refresh]);

  const usable = usableProfiles(profiles);
  const current = sessions?.find((s) => s.id === selected) ?? null;

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (profileId === null) return;
    setSaving(true);
    setError(null);
    try {
      const s = await createAgentSession({ title: "", llm_profile_id: profileId, llm_model: modelToSend(usable, choice) });
      setCreating(false);
      await refresh();
      setSelected(s.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (s: AgentSession) => {
    if (!window.confirm(`Удалить сессию «${s.title}» вместе с её файлами в контейнере агента?`)) return;
    try {
      await deleteAgentSession(s.id);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <div>
      <PageHeader
        title="Агент"
        subtitle="Кодинг-агент Codex в изолированном контейнере: даёте задачу словами, видите каждую команду и правку"
        action={
          <div className="flex items-center gap-3">
            {health && <Badge tone={health.ok ? "ok" : "bad"}>{health.detail}</Badge>}
            <Button
              variant="primary"
              disabled={health?.ok === false}
              onClick={() => {
                setChoice(defaultChoice(usable));
                setCreating(true);
              }}
            >
              Новая сессия
            </Button>
          </div>
        }
      />
      <ErrorNote message={error} />

      {sessions === null ? (
        <Spinner />
      ) : sessions.length === 0 ? (
        <EmptyState
          title="Сессий пока нет"
          text="Создайте сессию: выберите профиль LLM (OpenAI, Grok или custom с Responses API) и поставьте агенту задачу."
        />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[18rem_1fr]">
          <div className="space-y-2">
            {sessions.map((s) => (
              <motion.div key={s.id} layout>
                <div
                  className={clsx(
                    "group flex w-full items-start justify-between gap-2 rounded-xl border px-3 py-2.5 text-left transition-colors",
                    s.id === selected ? "border-accent/40 bg-accent/10" : "border-white/5 bg-white/[0.03] hover:bg-white/5"
                  )}
                >
                  <button type="button" className="min-w-0 flex-1 text-left" onClick={() => setSelected(s.id)}>
                    <span className="block truncate text-sm text-slate-100">{s.title}</span>
                    <span className="text-[11px] text-slate-500">
                      {s.status === "running" ? "отвечает…" : `ходов: ${s.turn_count}`}
                      {s.last_error && <span className="text-signal-bad"> · ошибка</span>}
                    </span>
                  </button>
                  <button
                    type="button"
                    title="Удалить"
                    disabled={s.status === "running"}
                    className="text-slate-600 opacity-0 hover:text-signal-bad disabled:hidden group-hover:opacity-100"
                    onClick={() => void remove(s)}
                  >
                    ✕
                  </button>
                </div>
              </motion.div>
            ))}
          </div>
          {current && <Chat key={current.id} session={current} profileAvailable={current.llm_profile_id !== null} onChanged={refresh} />}
        </div>
      )}

      <Modal open={creating} onClose={() => setCreating(false)} title="Новая сессия агента">
        <form onSubmit={create} className="space-y-4">
          {usable.length === 0 ? (
            <p className="text-sm text-slate-400">
              Нужен профиль LLM с OpenAI, Grok или custom (OpenAI-совместимый endpoint с Responses API). Профили Claude агент не поддерживает —
              Codex говорит только на Responses API.
            </p>
          ) : (
            <Field label="Модель" hint="Любая модель вашего профиля (OpenAI, Grok или custom с Responses API). Ключ из профиля уходит в контейнер агента вместе с каждым сообщением и нигде не сохраняется там.">
              <ModelSelect profiles={usable} value={choice} onChange={setChoice} required label="Модель" />
            </Field>
          )}
          <p className="text-[11px] leading-relaxed text-slate-500">
            Агент выполняет команды без подтверждений — внутри своего контейнера, без доступа к базе, Docker и секретам панели. Всё, что он
            делает, видно в ленте.
          </p>
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={() => setCreating(false)}>
              Отмена
            </Button>
            <Button type="submit" variant="primary" loading={saving} disabled={profileId === null || usable.length === 0}>
              Создать
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
