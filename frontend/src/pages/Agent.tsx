import clsx from "clsx";
import { motion } from "framer-motion";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { FeedStep, type Answer } from "../components/agent/Feed";
import FilesModal from "../components/agent/FilesModal";
import GoalModal from "../components/agent/GoalModal";
import SettingsModal from "../components/agent/SettingsModal";
import ModelSelect from "../components/ModelSelect";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner } from "../components/ui";
import { buildFeed, canSend, contextView, formatTokens, goalResumable, goalStatusLabel, lastStepRunning, readComposer, turnFinished, usableProfiles, waitingForUser } from "../lib/agentModel";
import {
  answerAgentApproval,
  createAgentSession,
  deleteAgentSession,
  errorMessage,
  forkAgentSession,
  getAgentHealth,
  listAgentSessions,
  listProfiles,
  runAgentAction,
  sendAgentMessage,
  steerAgentTurn,
  stopAgentTurn,
  type AgentSession,
  type LLMProfile,
} from "../lib/api";
import { defaultChoice, modelToSend, type Choice } from "../lib/modelChoice";
import { useAgentStream } from "../lib/useAgentStream";

function MenuItem({ children, onClick, danger = false, disabled = false }: { children: string; onClick: () => void; danger?: boolean; disabled?: boolean }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={clsx("block w-full px-3 py-2 text-left text-xs hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-40", danger ? "text-signal-bad" : "text-slate-200")}
    >
      {children}
    </button>
  );
}

function Chat({
  session,
  profileAvailable,
  initialDraft,
  onChanged,
  onForked,
}: {
  session: AgentSession;
  profileAvailable: boolean;
  initialDraft: string;
  onChanged: () => void;
  onForked: (s: AgentSession) => void;
}) {
  const { events, live, status, state } = useAgentStream(session.id);
  const feed = buildFeed(events, live);
  const [text, setText] = useState(initialDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const [settings, setSettings] = useState(false);
  const [files, setFiles] = useState(false);
  const [goalOpen, setGoalOpen] = useState<string | null>(null); // the text to start the goal form with; null = closed
  const [current, setCurrent] = useState(session);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => setCurrent(session), [session]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [events.length, live, status]);

  // The list on the left shows the status too: refresh it when a turn starts or ends.
  const prevStatus = useRef(status);
  useEffect(() => {
    if (prevStatus.current !== status) onChanged();
    prevStatus.current = status;
  }, [status, onChanged]);

  // The header shows the goal's progress and the context: refresh them when a turn ends or the goal changes, not only when the session stops.
  const marks = events.reduce((n, e) => n + (e.kind === "turn.completed" || e.kind.startsWith("goal.") ? 1 : 0), 0);
  useEffect(() => {
    if (marks > 0) onChanged();
  }, [marks, onChanged]);

  const running = status === "running";
  const context = contextView(current.context_used, current.context_window);
  const goal = current.goal;
  const waiting = waitingForUser(feed);
  const composer = readComposer(text, status);

  const attempt = async (fn: () => Promise<unknown>, clear = true) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      if (clear) setText("");
      stick.current = true;
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const doFork = () =>
    attempt(async () => {
      onForked(await forkAgentSession(session.id));
    }, false);

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    if (busy || composer.kind === "empty") return;
    switch (composer.kind) {
      case "message":
        return attempt(() => sendAgentMessage(session.id, composer.text));
      case "steer":
        return attempt(() => steerAgentTurn(session.id, composer.text));
      case "shell":
        return attempt(() => runAgentAction(session.id, { action: "shell", command: composer.command }));
      case "action":
        if (composer.action === "fork") return doFork();
        if (composer.action === "goal") {
          setGoalOpen(composer.argument);
          setText("");
          return;
        }
        if (composer.action === "review") {
          const target = composer.argument ? ({ type: "baseBranch", branch: composer.argument } as const) : undefined;
          return attempt(() => runAgentAction(session.id, { action: "review", target }));
        }
        return attempt(() => runAgentAction(session.id, { action: composer.action === "compact" ? "compact" : "revert" }));
    }
  };

  const action = (fn: () => Promise<unknown>) => {
    setMenu(false);
    void attempt(fn, false);
  };

  const stop = async () => {
    try {
      await stopAgentTurn(session.id);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const answer: Answer = async (requestId, decision, answers) => {
    try {
      await answerAgentApproval(session.id, requestId, decision, answers);
    } catch (err) {
      throw new Error(errorMessage(err));
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const sendable = canSend({ text, status, profileAvailable });
  const placeholder = waiting
    ? "Агент ждёт вашего ответа выше…"
    : running
      ? "Агент работает. Напишите — это дополнит ход. «Стоп» — прервать"
      : "Что сделать?  !команда — выполнить самому · /goal /compact /review /undo /fork";

  return (
    <Card className="flex h-[calc(100vh-14rem)] min-h-[30rem] flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/5 px-5 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-white">{current.title}</h2>
          <p className="truncate text-[11px] text-slate-500">
            {current.llm_profile_name ?? "профиль удалён"}
            {current.llm_model ? ` · ${current.llm_model}` : ""}
            {current.approval_mode === "ask" ? " · спрашивает" : ""}
            {current.instructions ? " · свои инструкции" : ""}
          </p>
        </div>
        <div className="relative flex items-center gap-2">
          {state !== "live" && <Badge tone={state === "error" ? "bad" : "warn"}>{state === "error" ? "нет связи" : "подключение…"}</Badge>}
          {running && <Badge tone={waiting ? "warn" : "accent"}>{waiting ? "ждёт ответа" : turnFinished(feed) ? "завершается…" : "работает…"}</Badge>}
          {running && (
            <Button variant="danger" className="px-3 py-1.5 text-xs" onClick={stop} disabled={current.cancel_requested}>
              Стоп
            </Button>
          )}
          <Button className="px-3 py-1.5 text-xs" onClick={() => setFiles(true)}>
            Файлы
          </Button>
          <Button className="px-3 py-1.5 text-xs" onClick={() => setMenu((m) => !m)} aria-label="Действия">
            ⋯
          </Button>
          {menu && (
            <>
              <button type="button" aria-label="Закрыть меню" className="fixed inset-0 z-10 cursor-default" onClick={() => setMenu(false)} />
              <div className="absolute right-0 top-full z-20 mt-1 w-56 overflow-hidden rounded-xl border border-white/10 bg-ink-900 shadow-xl">
                <MenuItem onClick={() => { setMenu(false); setSettings(true); }}>Настройки сессии…</MenuItem>
                <MenuItem disabled={running} onClick={() => { setMenu(false); setGoalOpen(""); }}>Поставить цель…</MenuItem>
                <MenuItem disabled={running} onClick={() => action(() => runAgentAction(session.id, { action: "compact" }))}>Сжать контекст</MenuItem>
                <MenuItem disabled={running} onClick={() => action(() => runAgentAction(session.id, { action: "review" }))}>Код-ревью изменений</MenuItem>
                <MenuItem disabled={running} onClick={() => action(() => runAgentAction(session.id, { action: "revert" }))}>Откатить последний ход</MenuItem>
                <MenuItem disabled={running} onClick={() => action(doFork)}>Ответвить копию сессии</MenuItem>
              </div>
            </>
          )}
        </div>
      </div>

      {(context.percent !== null || current.tokens_in > 0 || goal) && (
        <div className="space-y-2 border-b border-white/5 px-5 py-2">
          {(context.percent !== null || current.tokens_in > 0) && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
              {context.percent !== null && (
                <span className="flex items-center gap-2" title={current.context_window_known ? "Сколько места в контексте модели занято после последнего хода" : "Codex не знает эту модель: размер окна взят по умолчанию и может не совпадать с настоящим"}>
                  контекст
                  <span className="h-1.5 w-28 overflow-hidden rounded-full bg-white/10">
                    <span className={clsx("block h-full rounded-full", context.tone === "bad" ? "bg-signal-bad" : context.tone === "warn" ? "bg-signal-warn" : "bg-accent")} style={{ width: `${Math.max(2, context.percent)}%` }} />
                  </span>
                  <span className={context.tone === "bad" ? "text-signal-bad" : context.tone === "warn" ? "text-signal-warn" : "text-slate-300"}>
                    {current.context_window_known ? "" : "≈"}
                    {context.percent}%
                  </span>
                  <span>
                    {formatTokens(current.context_used ?? 0)} из {formatTokens(current.context_window ?? 0)}
                  </span>
                </span>
              )}
              <span>
                всего за сессию: {formatTokens(current.tokens_in)} на входе · {formatTokens(current.tokens_out)} на выходе
              </span>
              {context.suggestCompact && !running && (
                <button type="button" className="text-accent-soft underline decoration-dotted" onClick={() => action(() => runAgentAction(session.id, { action: "compact" }))}>
                  сжать контекст
                </button>
              )}
            </div>
          )}
          {goal && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <Badge tone={goal.status === "complete" ? "ok" : goal.status === "active" ? "accent" : "warn"}>цель {goalStatusLabel(goal.status)}</Badge>
              <span className="min-w-0 flex-1 truncate text-slate-300" title={goal.objective}>
                {goal.objective}
              </span>
              <span className="text-[11px] text-slate-500">
                {formatTokens(goal.tokens_used)}
                {goal.token_budget ? ` из ${formatTokens(goal.token_budget)}` : ""} токенов
              </span>
              {goalResumable(goal, status) && (
                <button type="button" className="text-[11px] text-accent-soft underline decoration-dotted" onClick={() => action(() => runAgentAction(session.id, { action: "goal" }))}>
                  продолжить
                </button>
              )}
              {!running && (
                <button type="button" className="text-[11px] text-slate-500 underline decoration-dotted hover:text-slate-300" onClick={() => action(() => runAgentAction(session.id, { action: "goal_clear" }))}>
                  снять
                </button>
              )}
            </div>
          )}
        </div>
      )}

      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="flex-1 space-y-3 overflow-y-auto px-5 py-4"
      >
        {feed.length === 0 && state === "live" && (
          <p className="pt-10 text-center text-sm text-slate-500">Напишите задачу — агент будет выполнять команды и править файлы в своём контейнере. Файлы сессии — по кнопке «Файлы».</p>
        )}
        {feed.map((entry) => (
          <FeedStep key={entry.key} entry={entry} answer={answer} />
        ))}
        {running && !lastStepRunning(feed) && !turnFinished(feed) && !waiting && <Spinner className="h-3.5 w-3.5" />}
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
            placeholder={placeholder}
            className="min-h-[3.25rem] flex-1 resize-y rounded-xl border border-white/10 bg-ink-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/50"
          />
          <Button type="submit" variant="primary" loading={busy} disabled={!sendable}>
            {running ? "Дополнить" : "Отправить"}
          </Button>
        </div>
        <p className="mt-1.5 text-[10px] text-slate-600">Enter — отправить, Shift+Enter — новая строка{composer.kind === "shell" ? " · будет выполнено как команда, без участия модели" : ""}</p>
      </form>

      <SettingsModal open={settings} session={current} onClose={() => setSettings(false)} onSaved={(s) => { setCurrent(s); onChanged(); }} />
      <FilesModal open={files} session={current} onClose={() => setFiles(false)} />
      <GoalModal
        open={goalOpen !== null}
        initial={goalOpen ?? ""}
        onClose={() => setGoalOpen(null)}
        onStart={async (objective, budget) => {
          try {
            await runAgentAction(session.id, { action: "goal", objective, token_budget: budget });
            stick.current = true;
          } catch (err) {
            throw new Error(errorMessage(err));
          }
        }}
      />
    </Card>
  );
}

export default function Agent() {
  const navigate = useNavigate();
  const location = useLocation();
  const [sessions, setSessions] = useState<AgentSession[] | null>(null);
  const [profiles, setProfiles] = useState<LLMProfile[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [health, setHealth] = useState<{ ok: boolean; detail: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [saving, setSaving] = useState(false);
  // From «Разобрать в агенте» on a scan page: which session to open and the task proposed for it.
  const incoming = (location.state ?? null) as { sessionId?: number; draft?: string } | null;
  const [draft] = useState<{ sessionId: number; text: string } | null>(incoming?.sessionId && incoming.draft ? { sessionId: incoming.sessionId, text: incoming.draft } : null);

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

  useEffect(() => {
    if (incoming?.sessionId && sessions?.some((s) => s.id === incoming.sessionId)) {
      setSelected(incoming.sessionId);
      navigate(location.pathname, { replace: true, state: null }); // the draft is taken once
    }
  }, [incoming?.sessionId, sessions, navigate, location.pathname]);

  const usable = usableProfiles(profiles);
  const current = sessions?.find((s) => s.id === selected) ?? null;

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!choice) return;
    setSaving(true);
    setError(null);
    try {
      const s = await createAgentSession({ title: "", llm_profile_id: choice.profileId, llm_model: modelToSend(usable, choice) });
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
        subtitle="Кодинг-агент Codex в изолированном контейнере: задача словами, а команды, правки и ответ — по мере появления"
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
          text="Создайте сессию: выберите модель (OpenAI, Grok или custom с Responses API) и поставьте агенту задачу. Скан можно разобрать в агенте со страницы самого скана."
        />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[18rem_1fr]">
          {/* `min-w-0` on every grid item: without it a CSS grid track sizes to its content's min-content width, so one long
              unbroken line (a diff, a path, a command) in the chat makes this track — and the whole page — wider than the
              viewport. `body` has `overflow-x: hidden`, so that overflow does not scroll; it is silently clipped off the right
              edge instead, which looked like the chat running off the monitor. */}
          <div className="min-w-0 space-y-2">
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
                      {s.status === "running" ? "работает…" : `ходов: ${s.turn_count}`}
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
          {current && (
            <div className="min-w-0">
              <Chat
                key={current.id}
                session={current}
                profileAvailable={current.llm_profile_id !== null}
                initialDraft={draft?.sessionId === current.id ? draft.text : ""}
                onChanged={refresh}
                onForked={async (s) => {
                  await refresh();
                  setSelected(s.id);
                }}
              />
            </div>
          )}
        </div>
      )}

      <Modal open={creating} onClose={() => setCreating(false)} title="Новая сессия агента">
        <form onSubmit={create} className="space-y-4">
          {usable.length === 0 ? (
            <p className="text-sm text-slate-400">
              Нужен профиль LLM с OpenAI, Grok или custom (OpenAI-совместимый endpoint с Responses API). Профили Claude агент не поддерживает — Codex говорит только на Responses API.
            </p>
          ) : (
            <Field label="Модель" hint="Любая модель вашего профиля. Ключ из профиля уходит в контейнер агента вместе с каждым сообщением и нигде не сохраняется там.">
              <ModelSelect profiles={usable} value={choice} onChange={setChoice} required label="Модель" />
            </Field>
          )}
          <p className="text-[11px] leading-relaxed text-slate-500">
            По умолчанию агент выполняет команды без вопросов — внутри своего контейнера, без доступа к базе, Docker и секретам панели. Режим «спрашивать», свои инструкции и глубину
            размышлений можно включить в настройках сессии.
          </p>
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={() => setCreating(false)}>
              Отмена
            </Button>
            <Button type="submit" variant="primary" loading={saving} disabled={!choice || usable.length === 0}>
              Создать
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
