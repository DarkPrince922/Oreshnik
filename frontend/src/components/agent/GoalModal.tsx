import { useEffect, useState, type FormEvent } from "react";

import { DEFAULT_GOAL_BUDGET, formatTokens, parseBudget } from "../../lib/agentModel";
import { Button, ErrorNote, Field, Modal } from "../ui";

/** A goal: the agent then works by itself, turn after turn, until it is reached or the token budget is spent. */
export default function GoalModal({ open, initial, onClose, onStart }: { open: boolean; initial: string; onClose: () => void; onStart: (objective: string, budget: number) => Promise<void> }) {
  const [objective, setObjective] = useState(initial);
  const [budget, setBudget] = useState(String(DEFAULT_GOAL_BUDGET));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setObjective(initial);
      setError(null);
    }
  }, [open, initial]);

  const parsed = parseBudget(budget);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!parsed || !objective.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onStart(objective.trim(), parsed);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось поставить цель");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Поставить цель">
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm leading-relaxed text-slate-400">
          С целью агент работает <span className="text-slate-200">сам, ход за ходом</span>, без ваших сообщений — пока не достигнет её, не упрётся или не потратит бюджет. «Стоп» ставит цель на
          паузу; продолжить можно позже.
        </p>
        <Field label="Цель" hint="Проверяемый результат: по нему агент решит, что работа закончена.">
          <textarea className="field min-h-[5rem]" value={objective} maxLength={4000} onChange={(e) => setObjective(e.target.value)} placeholder="Например: все тесты в tests/ проходят, линтер без ошибок" required />
        </Field>
        <Field label="Бюджет токенов" hint={parsed ? `До ${formatTokens(parsed)} токенов на всю цель: это главный ограничитель расхода.` : "Число не меньше 10000; можно 500k или 2m."}>
          <input className="field font-mono" value={budget} onChange={(e) => setBudget(e.target.value)} aria-invalid={!parsed} />
        </Field>
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!parsed || !objective.trim()}>
            Запустить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
