import clsx from "clsx";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { FeedStep, type Answer } from "../components/agent/Feed";
import FilesModal from "../components/agent/FilesModal";
import GoalModal from "../components/agent/GoalModal";
import { ArrowUpIcon, BranchIcon, CompressIcon, FolderIcon, PlusIcon, ReviewIcon, SlidersIcon, TargetIcon, TerminalIcon, UndoIcon } from "../components/agent/icons";
import SessionList from "../components/agent/SessionList";
import SessionStats from "../components/agent/SessionStats";
import SettingsModal from "../components/agent/SettingsModal";
import ModelSelect from "../components/ModelSelect";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, Spinner } from "../components/ui";
import { buildFeed, canSend, lastStepRunning, readComposer, turnFinished, usableProfiles, waitingForUser } from "../lib/agentModel";
import { applyQuick, groupSteps, isUserBlock, sessionsSummary } from "../lib/agentSessions";
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

const chip = "rounded-md border border-white/10 px-2 py-0.5 text-slate-400";
const WEB_SEARCH: Record<string, string> = { auto: "авто", on: "включён", off: "выключен" };

/** The buttons above the message box: Codex-style slash commands and `!` for a command of your own. */
const QUICK: { label: string; text: string; hint: string }[] = [
  { label: "/goal", text: "/goal", hint: "Поставить цель: агент работает сам, пока не достигнет её" },
  { label: "/compact", text: "/compact", hint: "Сжать контекст" },
  { label: "/review", text: "/review", hint: "Код-ревью изменений (можно указать ветку)" },
  { label: "/undo", text: "/undo", hint: "Откатить последний ход" },
  { label: "/fork", text: "/fork", hint: "Ответвить копию сессии" },
  { label: "!команда", text: "!", hint: "Выполнить свою команду в контейнере, без участия модели" },
];

function ToolButton({ icon, children, ...rest }: { icon: ReactNode; children: string; title: string; disabled?: boolean; onClick: () => void }) {
  return (
    <Button className="h-9 px-3 text-[13px]" {...rest}>
      {icon}
      {children}
    </Button>
  );
}

const AgentMark = () => (
  <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/20 text-accent-soft" title="Агент">
    <TerminalIcon />
  </span>
);

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
  const blocks = groupSteps(feed);
  const [text, setText] = useState(initialDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState(false);
  const [files, setFiles] = useState(false);
  const [goalOpen, setGoalOpen] = useState<string | null>(null); // the text to start the goal form with; null = closed
  const [current, setCurrent] = useState(session);
  const scroller = useRef<HTMLDivElement>(null);
  const composerBox = useRef<HTMLTextAreaElement>(null);
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
    void attempt(fn, false);
  };
  const compact = () => action(() => runAgentAction(session.id, { action: "compact" }));
  /** A quick-command button: put the command into the box and leave the cursor there. */
  const quick = (command: string) => {
    setText((t) => applyQuick(t, command));
    composerBox.current?.focus();
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
    <Card className="flex h-[calc(100vh-9.5rem)] min-h-[44rem] flex-col">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 pb-3 pt-4">
        <div className="min-w-0 space-y-1.5">
          <h2 className="truncate text-lg font-semibold text-white">{current.title}</h2>
          <div className="flex flex-wrap gap-1.5 text-[11px]">
            <span className={clsx(chip, "max-w-full truncate font-mono text-slate-300")}>
              {current.llm_profile_name ?? "профиль удалён"}
              {current.llm_model ? ` · ${current.llm_model}` : ""}
            </span>
            <span className={chip}>подтверждения: {current.approval_mode === "ask" ? "спрашивает" : "без вопросов"}</span>
            <span className={chip}>веб-поиск: {WEB_SEARCH[current.web_search] ?? current.web_search}</span>
            {current.instructions && <span className={chip}>свои инструкции</span>}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {state !== "live" && <Badge tone={state === "error" ? "bad" : "warn"}>{state === "error" ? "нет связи" : "подключение…"}</Badge>}
          {running && <Badge tone={waiting ? "warn" : "accent"}>{waiting ? "ждёт ответа" : turnFinished(feed) ? "завершается…" : "работает…"}</Badge>}
          {running && (
            <Button variant="danger" className="h-10 px-3.5 text-xs" onClick={stop} disabled={current.cancel_requested}>
              Стоп
            </Button>
          )}
          <Button className="h-10 w-10 !p-0" onClick={() => setSettings(true)} aria-label="Настройки сессии" title="Настройки сессии">
            <SlidersIcon className="h-[18px] w-[18px]" />
          </Button>
        </div>
      </div>

      {/* What used to hide behind «⋯»: every action of the session, in sight. All but «Файлы» wait for the agent to stop. */}
      <div className="flex flex-wrap gap-2 border-b border-white/5 px-5 pb-3">
        <ToolButton icon={<FolderIcon />} title="Файлы сессии в контейнере агента" onClick={() => setFiles(true)}>
          Файлы
        </ToolButton>
        <ToolButton icon={<TargetIcon />} title="Поставить цель: агент работает сам, пока не достигнет её или не потратит бюджет" disabled={running} onClick={() => setGoalOpen("")}>
          Цель
        </ToolButton>
        <ToolButton icon={<CompressIcon />} title="Сжать контекст: заменить историю кратким пересказом" disabled={running} onClick={compact}>
          Сжать
        </ToolButton>
        <ToolButton icon={<ReviewIcon />} title="Код-ревью изменений, сделанных в сессии" disabled={running} onClick={() => action(() => runAgentAction(session.id, { action: "review" }))}>
          Ревью
        </ToolButton>
        <ToolButton icon={<UndoIcon />} title="Откатить последний ход" disabled={running} onClick={() => action(() => runAgentAction(session.id, { action: "revert" }))}>
          Откатить
        </ToolButton>
        <ToolButton icon={<BranchIcon />} title="Ответвить копию сессии" disabled={running} onClick={() => action(doFork)}>
          Ответвить
        </ToolButton>
      </div>

      <SessionStats
        session={current}
        status={status}
        waiting={waiting}
        onCompact={compact}
        onGoalSet={() => setGoalOpen("")}
        onGoalResume={() => action(() => runAgentAction(session.id, { action: "goal" }))}
        onGoalClear={() => action(() => runAgentAction(session.id, { action: "goal_clear" }))}
      />

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
        {blocks.map((block, i) => {
          if (block.type === "single" && isUserBlock(block)) return <FeedStep key={block.key} entry={block.entry} answer={answer} />;
          // The agent's side: one mark where its turn begins, everything under it aligned to the same column.
          const first = i === 0 || isUserBlock(blocks[i - 1]);
          return (
            <div key={block.key} className="flex gap-3">
              <span className="w-7 shrink-0">{first && <AgentMark />}</span>
              <div className="min-w-0 flex-1">
                {block.type === "steps" ? (
                  <div className="max-w-[92%] divide-y divide-white/5 overflow-hidden rounded-xl border border-white/[0.08] bg-ink-800/60">
                    {block.entries.map((entry) => (
                      <FeedStep key={entry.key} entry={entry} answer={answer} grouped />
                    ))}
                  </div>
                ) : (
                  <FeedStep entry={block.entry} answer={answer} />
                )}
              </div>
            </div>
          );
        })}
        {running && !lastStepRunning(feed) && !turnFinished(feed) && !waiting && <Spinner className="ml-10 h-3.5 w-3.5" />}
      </div>

      <form onSubmit={send} className="border-t border-white/5 px-4 pb-4 pt-2">
        <ErrorNote message={error} />
        {!profileAvailable && <p className="mb-2 text-xs text-signal-warn">Профиль LLM этой сессии удалён: создайте новую сессию.</p>}
        <div className="mb-1.5 flex flex-wrap gap-0.5">
          {QUICK.map((q) => (
            <button
              key={q.text}
              type="button"
              disabled={running}
              title={running ? "Пока агент работает, сообщение дополняет ход — команды недоступны" : q.hint}
              onClick={() => quick(q.text)}
              className={clsx(
                "rounded-lg px-2 py-1.5 font-mono text-xs font-medium transition-colors hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent",
                q.text === "!" ? "text-signal-ok" : "text-accent-soft"
              )}
            >
              {q.label}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2 rounded-2xl border border-white/[0.12] bg-ink-800/80 py-2 pl-3.5 pr-2 transition-colors focus-within:border-accent/50">
          <textarea
            ref={composerBox}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            rows={2}
            maxLength={50000}
            placeholder={placeholder}
            aria-label="Сообщение агенту"
            className="max-h-60 min-h-[3rem] flex-1 resize-y bg-transparent py-1.5 text-sm text-slate-100 outline-none placeholder:text-slate-500"
          />
          <Button type="submit" variant="primary" loading={busy} disabled={!sendable} className="h-10 px-4">
            {!busy && <ArrowUpIcon />}
            {running ? "Дополнить" : "Отправить"}
          </Button>
        </div>
        <p className="mt-1.5 text-[11px] text-slate-500">Enter — отправить, Shift+Enter — новая строка{composer.kind === "shell" ? " · будет выполнено как команда, без участия модели" : ""}</p>
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
  const summary = sessionsSummary(sessions ?? []);
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
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight text-white">Агент</h1>
            {health && (
              <Badge tone={health.ok ? "ok" : "bad"}>
                <span className={clsx("h-1.5 w-1.5 rounded-full", health.ok ? "bg-signal-ok" : "bg-signal-bad")} />
                {health.ok ? "на связи" : health.detail}
              </Badge>
            )}
          </div>
          <p className="text-sm text-slate-400">
            Codex в изолированном контейнере{summary ? ` · ${summary}` : ": задача словами, а команды, правки и ответ — по мере появления"}
          </p>
        </div>
        <Button
          variant="primary"
          disabled={health?.ok === false}
          onClick={() => {
            setChoice(defaultChoice(usable));
            setCreating(true);
          }}
        >
          <PlusIcon />
          Новая сессия
        </Button>
      </div>
      <ErrorNote message={error} />

      {sessions === null ? (
        <Spinner />
      ) : sessions.length === 0 ? (
        <EmptyState
          title="Сессий пока нет"
          text="Создайте сессию: выберите модель (OpenAI, Grok или custom с Responses API) и поставьте агенту задачу. Скан можно разобрать в агенте со страницы самого скана."
        />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[19rem_minmax(0,1fr)]">
          {/* `min-w-0` on every grid item: without it a CSS grid track sizes to its content's min-content width, so one long
              unbroken line (a diff, a path, a command) in the chat makes this track — and the whole page — wider than the
              viewport. `body` has `overflow-x: hidden`, so that overflow does not scroll; it is silently clipped off the right
              edge instead, which looked like the chat running off the monitor. */}
          <div className="min-w-0">
            <SessionList sessions={sessions} selected={selected} onSelect={setSelected} onRemove={(s) => void remove(s)} />
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
