import clsx from "clsx";
import { useState } from "react";

import type { FeedEntry } from "../../lib/agentModel";
import { formatTokens } from "../../lib/agentModel";
import { changeLines } from "../../lib/diff";
import type { ApprovalDecision } from "../../lib/api";
import Markdown from "../Markdown";
import { Button, Spinner } from "../ui";
import { FileChange, TurnDiff } from "./DiffView";

function Pre({ children, className }: { children: string; className?: string }) {
  return (
    <pre className={clsx("max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-slate-300", className)}>
      {children}
    </pre>
  );
}

const Cursor = () => <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse rounded-sm bg-accent-soft/70" />;

export type Answer = (requestId: string, decision: ApprovalDecision, answers?: Record<string, string | string[]>) => Promise<void>;

/** What the agent asks the user before it goes on: approve a command or a file change, or answer its question. */
function ApprovalCard({ entry, answer }: { entry: Extract<FeedEntry, { type: "approval" }>; answer: Answer }) {
  const { approval, resolved, requestId } = entry;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [typed, setTyped] = useState<Record<string, string>>({});

  const decide = async (decision: ApprovalDecision, answers?: Record<string, string | string[]>) => {
    setBusy(decision);
    setError(null);
    try {
      await answer(requestId, decision, answers);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось отправить ответ");
    } finally {
      setBusy(null);
    }
  };

  const title = approval.kind === "command" ? "Агент хочет выполнить команду" : approval.kind === "file" ? "Агент хочет изменить файлы" : approval.kind === "input" ? "Агент спрашивает" : "Агент просит дополнительные права";
  return (
    <div className={clsx("max-w-[92%] rounded-xl border p-3", resolved ? "border-white/5 bg-ink-900/40" : "border-signal-warn/30 bg-signal-warn/5")}>
      <div className="flex items-center gap-2 text-xs">
        <span className={resolved ? "text-slate-500" : "text-signal-warn"}>{resolved ? "✓" : "⏸"}</span>
        <span className={resolved ? "text-slate-500" : "text-slate-200"}>{title}</span>
        {resolved && <span className="text-[11px] text-slate-600">— вопрос закрыт</span>}
      </div>

      {approval.kind === "command" && approval.command && (
        <>
          <code className="mt-2 block whitespace-pre-wrap break-all rounded-lg bg-black/40 px-3 py-2 font-mono text-xs text-slate-100">$ {approval.command}</code>
          <p className="mt-1 text-[11px] text-slate-500">в папке: {approval.cwd ?? "."}</p>
        </>
      )}
      {approval.reason && <p className="mt-1.5 text-xs text-slate-400">{approval.reason}</p>}
      {approval.kind === "file" && !approval.reason && <p className="mt-1.5 text-xs text-slate-500">Файлы и правки — в шаге выше.</p>}

      {approval.kind === "input" && !resolved && (
        <div className="mt-2 space-y-3">
          {approval.questions.map((q) => (
            <div key={q.id}>
              {q.header && <p className="text-[11px] uppercase tracking-wide text-slate-500">{q.header}</p>}
              <p className="text-sm text-slate-200">{q.question}</p>
              {q.options.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-2">
                  {q.options.map((o) => (
                    <button
                      key={o.label}
                      type="button"
                      title={o.description}
                      onClick={() => setTyped((t) => ({ ...t, [q.id]: o.label }))}
                      className={clsx("rounded-lg border px-2.5 py-1 text-xs", typed[q.id] === o.label ? "border-accent/60 bg-accent/15 text-white" : "border-white/10 text-slate-300 hover:bg-white/5")}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              )}
              {(q.other || q.options.length === 0) && (
                <input
                  className="field mt-1.5 text-sm"
                  type={q.secret ? "password" : "text"}
                  autoComplete="off"
                  placeholder={q.options.length ? "Свой вариант" : "Ответ"}
                  value={q.options.some((o) => o.label === typed[q.id]) ? "" : typed[q.id] ?? ""}
                  onChange={(e) => setTyped((t) => ({ ...t, [q.id]: e.target.value }))}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {!resolved && (
        <div className="mt-3 flex flex-wrap gap-2">
          {approval.kind === "input" ? (
            <Button variant="primary" className="px-3 py-1.5 text-xs" loading={busy === "answer"} disabled={!approval.questions.every((q) => (typed[q.id] ?? "").trim() !== "")} onClick={() => void decide("answer", Object.fromEntries(approval.questions.map((q) => [q.id, typed[q.id].trim()])))}>
              Ответить
            </Button>
          ) : approval.kind === "permissions" ? null : (
            <>
              <Button variant="primary" className="px-3 py-1.5 text-xs" loading={busy === "accept"} onClick={() => void decide("accept")}>
                Разрешить
              </Button>
              <Button className="px-3 py-1.5 text-xs" loading={busy === "accept_session"} onClick={() => void decide("accept_session")} title="Больше не спрашивать похожее в этой сессии">
                Разрешить на сессию
              </Button>
            </>
          )}
          <Button variant="danger" className="px-3 py-1.5 text-xs" loading={busy === "decline"} onClick={() => void decide("decline")}>
            Отклонить
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-xs text-signal-bad">{error}</p>}
    </div>
  );
}

const STATUS_NOTE: Record<string, string> = { declined: "отклонено", failed: "не удалось" };

export function FeedStep({ entry, answer }: { entry: FeedEntry; answer: Answer }) {
  const [open, setOpen] = useState(false);
  switch (entry.type) {
    case "user":
      return (
        <div className={clsx("ml-auto max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md border px-4 py-2.5 text-sm text-slate-100", entry.steer ? "border-white/10 bg-white/5" : "border-accent/20 bg-accent/10")}>
          {entry.steer && <span className="mb-0.5 block text-[10px] uppercase tracking-wide text-slate-500">добавлено по ходу</span>}
          {entry.text}
        </div>
      );
    case "action":
      return <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-md border border-white/10 bg-white/5 px-4 py-2 font-mono text-xs text-slate-300">{entry.text}</div>;
    case "message":
      return (
        <div className="max-w-[92%] text-sm">
          <Markdown>{entry.text || " "}</Markdown>
          {entry.live && <Cursor />}
        </div>
      );
    case "reasoning":
      return (
        <p className="max-w-[92%] whitespace-pre-wrap break-words text-xs italic text-slate-500">
          {entry.text}
          {entry.live && <Cursor />}
        </p>
      );
    case "command": {
      const failed = (entry.exitCode !== null && entry.exitCode !== 0) || entry.status === "failed" || entry.status === "declined";
      return (
        <div className="max-w-[92%] rounded-xl border border-white/5 bg-ink-900/50 p-3">
          <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((o) => !o)}>
            {entry.running ? <Spinner className="h-3 w-3" /> : <span className={clsx("text-xs", failed ? "text-signal-bad" : "text-signal-ok")}>{failed ? "✕" : "✓"}</span>}
            <code className="min-w-0 flex-1 truncate font-mono text-xs text-slate-200">
              {entry.user ? "!" : "$"} {entry.command}
            </code>
            {STATUS_NOTE[entry.status] && <span className="text-[11px] text-slate-500">{STATUS_NOTE[entry.status]}</span>}
            {entry.exitCode !== null && entry.exitCode !== 0 && <span className="font-mono text-[11px] text-signal-bad">код {entry.exitCode}</span>}
          </button>
          {(open || entry.running) && entry.output && <Pre className="mt-2">{entry.output}</Pre>}
          {open && !entry.output && <p className="mt-2 text-[11px] text-slate-600">Вывода нет.</p>}
        </div>
      );
    }
    case "files":
      return (
        <div className="max-w-[92%] space-y-1.5 rounded-xl border border-white/5 bg-ink-900/50 p-3">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            {entry.running && <Spinner className="h-3 w-3" />}
            Правки файлов
            {STATUS_NOTE[entry.status] && <span className="text-slate-500">— {STATUS_NOTE[entry.status]}</span>}
          </div>
          {entry.changes.map((c, i) => (
            <FileChange key={`${c.path}${i}`} path={c.path} kind={c.kind} lines={changeLines(c.kind, c.diff)} open={entry.changes.length === 1} />
          ))}
        </div>
      );
    case "diff":
      return <TurnDiff diff={entry.diff} live={entry.live} />;
    case "plan":
      return (
        <div className="max-w-[92%] rounded-xl border border-white/5 bg-ink-900/50 p-3 text-xs">
          <p className="mb-1 text-slate-400">План{entry.live ? " (обновляется)" : ""}</p>
          {entry.explanation && <p className="mb-1 text-slate-500">{entry.explanation}</p>}
          <ul className="space-y-0.5">
            {entry.steps.map((s, i) => (
              <li key={i} className={clsx(s.completed ? "text-slate-500 line-through" : s.inProgress ? "text-slate-100" : "text-slate-400")}>
                {s.completed ? "☑" : s.inProgress ? "▶" : "☐"} {s.text}
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
    case "approval":
      return <ApprovalCard entry={entry} answer={answer} />;
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
