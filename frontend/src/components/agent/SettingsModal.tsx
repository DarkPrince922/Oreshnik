import clsx from "clsx";
import { useEffect, useState, type FormEvent } from "react";

import {
  REASONING_EFFORTS, closeAgentListener, errorMessage, getAgentListenerRange, openAgentListener, updateAgentSession,
  type AgentListenerRange, type AgentSession, type ApprovalMode, type WebSearchMode,
} from "../../lib/api";
import { Button, ErrorNote, Field, Modal } from "../ui";

const EFFORT_LABEL: Record<string, string> = { minimal: "минимальная", low: "низкая", medium: "средняя", high: "высокая", xhigh: "максимальная" };

/** Settings of one session: its own system prompt, how hard the model thinks, whether the agent asks before acting. */
export default function SettingsModal({ open, session, onClose, onSaved }: { open: boolean; session: AgentSession; onClose: () => void; onSaved: (s: AgentSession) => void }) {
  const [title, setTitle] = useState(session.title);
  const [instructions, setInstructions] = useState(session.instructions ?? "");
  const [effort, setEffort] = useState(session.reasoning_effort ?? "");
  const [mode, setMode] = useState<ApprovalMode>(session.approval_mode);
  const [web, setWeb] = useState<WebSearchMode>(session.web_search);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [listenerRange, setListenerRange] = useState<AgentListenerRange | null>(null);
  const [listenerPort, setListenerPort] = useState("");
  const [listenerBusy, setListenerBusy] = useState(false);
  const [listenerError, setListenerError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setTitle(session.title);
    setInstructions(session.instructions ?? "");
    setEffort(session.reasoning_effort ?? "");
    setMode(session.approval_mode);
    setWeb(session.web_search);
    setError(null);
    setListenerError(null);
    getAgentListenerRange().then(setListenerRange, () => setListenerRange(null));
  }, [open, session]);

  useEffect(() => {
    if (listenerRange?.available && !listenerPort) setListenerPort(String(listenerRange.min));
  }, [listenerRange]); // eslint-disable-line react-hooks/exhaustive-deps

  const openListener = async () => {
    const port = Number(listenerPort);
    setListenerBusy(true);
    setListenerError(null);
    try {
      const listener = await openAgentListener(session.id, port);
      onSaved({ ...session, listener_port: listener.port });
    } catch (err) {
      setListenerError(errorMessage(err));
    } finally {
      setListenerBusy(false);
    }
  };
  const closeListener = async () => {
    setListenerBusy(true);
    setListenerError(null);
    try {
      await closeAgentListener(session.id);
      onSaved({ ...session, listener_port: null });
    } catch (err) {
      setListenerError(errorMessage(err));
    } finally {
      setListenerBusy(false);
    }
  };

  const changedPrompt = instructions.trim() !== (session.instructions ?? "");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const saved = await updateAgentSession(session.id, { title: title.trim() || session.title, instructions: instructions.trim(), reasoning_effort: effort, approval_mode: mode, web_search: web });
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Настройки сессии" wide>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Название">
          <input className="field" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        </Field>

        <Field
          label="Свои инструкции (системный промпт)"
          hint={`Что агент должен помнить всегда: стиль ответа, правила, контекст проекта. Для новой сессии они закладываются в начало разговора; если измените позже, агент получит их с вашим следующим сообщением (прежние отменяются), разговор и файлы сохраняются. ${instructions.length}/20000`}
        >
          <textarea className="field min-h-[8rem] font-mono text-xs" value={instructions} maxLength={20000} onChange={(e) => setInstructions(e.target.value)} placeholder="Например: отвечай по-русски, перед правкой кода показывай план, тесты запускай через pytest." />
        </Field>
        {changedPrompt && session.turn_count > 0 && <p className="text-[11px] text-signal-warn">Агент получит новые инструкции в начале вашего следующего сообщения: он увидит, что прежние отменены.</p>}

        <Field label="Глубина размышлений" hint="Как долго модель думает над каждым ответом. «По умолчанию» — как решит сама модель; поддерживают не все модели и шлюзы.">
          <select className="field" value={effort} onChange={(e) => setEffort(e.target.value)}>
            <option value="">по умолчанию</option>
            {REASONING_EFFORTS.map((x) => (
              <option key={x} value={x}>
                {EFFORT_LABEL[x]}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Поиск в интернете"
          hint="Встроенный веб-поиск Codex. «Авто»: включён для OpenAI и выключен для шлюзов (custom, Grok) — они обычно отклоняют этот инструмент ошибкой «function tools only». «Включён» для шлюза ставьте, только если он точно его поддерживает."
        >
          <select className="field" value={web} onChange={(e) => setWeb(e.target.value as WebSearchMode)}>
            <option value="auto">авто</option>
            <option value="on">включён</option>
            <option value="off">выключен</option>
          </select>
        </Field>

        <div>
          <span className="mb-1.5 block text-xs font-medium text-slate-400">Подтверждения</span>
          <div className="grid gap-2 sm:grid-cols-2">
            {(
              [
                ["auto", "Без вопросов", "Агент сам выполняет команды и правит файлы в своём контейнере."],
                ["ask", "Спрашивать", "Перед командами и правками файлов агент ждёт вашего «Разрешить»."],
              ] as const
            ).map(([value, label, hint]) => (
              <button key={value} type="button" onClick={() => setMode(value)} className={clsx("rounded-xl border p-3 text-left transition-colors", mode === value ? "border-accent/50 bg-accent/10" : "border-white/10 hover:bg-white/5")}>
                <span className="block text-sm text-slate-100">{label}</span>
                <span className="mt-0.5 block text-[11px] leading-relaxed text-slate-500">{hint}</span>
              </button>
            ))}
          </div>
        </div>

        {listenerRange?.available && (
          <Field
            label="Листенер"
            hint={`Контейнер агента сам не доступен снаружи: это проксирует порт панели на такой же порт внутри контейнера, пока агент сам его слушает (например, командой nc -lp <порт>). Диапазон: ${listenerRange.min}–${listenerRange.max}. Закрывается сам, если долго нет данных.`}
          >
            {session.listener_port ? (
              <div className="flex items-center gap-2">
                <span className="field flex-1 text-slate-300">Открыт порт {session.listener_port}</span>
                <Button type="button" onClick={closeListener} loading={listenerBusy}>
                  Закрыть
                </Button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <input
                  className="field w-28"
                  type="number"
                  min={listenerRange.min ?? undefined}
                  max={listenerRange.max ?? undefined}
                  value={listenerPort}
                  onChange={(e) => setListenerPort(e.target.value)}
                />
                <Button type="button" onClick={openListener} loading={listenerBusy}>
                  Открыть
                </Button>
              </div>
            )}
            <ErrorNote message={listenerError} />
          </Field>
        )}

        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" variant="primary" loading={busy}>
            Сохранить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
