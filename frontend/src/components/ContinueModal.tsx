import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { continueScan, errorMessage, listProfiles, type LLMProfile, type Scan } from "../lib/api";
import { budgetProblem, defaultBudget, instructionRequired, parseBudget } from "../lib/continueScan";
import { formatCost } from "../lib/format";
import { Button, ErrorNote, Field, Modal } from "./ui";

/**
 * "Продолжить" — `strix --resume` for a finished or stopped scan.
 *
 * It creates a NEW scan: Strix works on a copy of this scan's run (every agent remembers what it did, and the instruction
 * reaches the root agent), so this scan stays exactly as it is.
 */
export default function ContinueModal({ scan, open, onClose }: { scan: Scan; open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const needsInstruction = instructionRequired(scan);
  const needsProfile = scan.runner_kind !== null && scan.runner_kind !== "mock";

  const [instruction, setInstruction] = useState("");
  const [budget, setBudget] = useState(() => defaultBudget(scan));
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [profileId, setProfileId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Fresh form each time it opens; the profiles are fetched lazily.
  useEffect(() => {
    if (!open) return;
    setInstruction("");
    setBudget(defaultBudget(scan));
    setError(null);
    if (!needsProfile) return;
    let alive = true;
    listProfiles()
      .then((list) => {
        if (!alive) return;
        setProfiles(list);
        // The scan's own profile if it is one of yours, otherwise the first you have.
        setProfileId(list.find((p) => p.id === scan.llm_profile_id)?.id ?? list[0]?.id ?? null);
      })
      .catch((e) => alive && setError(errorMessage(e)));
    return () => {
      alive = false;
    };
  }, [open, scan, needsProfile]);

  const spent = scan.cost;
  const budgetError = budgetProblem(budget, spent);
  const canSubmit =
    !busy && (!needsInstruction || instruction.trim() !== "") && budgetError === null && (!needsProfile || profileId !== null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const child = await continueScan(scan.id, {
        instruction: instruction.trim() || null,
        max_budget: parseBudget(budget),
        llm_profile_id: needsProfile ? profileId : null,
      });
      onClose();
      navigate(`/scans/${child.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={`Продолжить скан #${scan.id}`}>
      <form onSubmit={submit} className="space-y-4">
        <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-3 text-xs leading-relaxed text-slate-400">
          Strix продолжит этот же прогон: агенты помнят всё, что уже сделали, а ваша инструкция уйдёт корневому агенту. Создаётся{" "}
          <span className="text-slate-200">новый скан</span>, а этот остаётся без изменений. Находки и стоимость в новом скане
          накопительные — включают то, что найдено здесь.
        </p>

        <Field
          label={needsInstruction ? "Что делать дальше" : "Что делать дальше (необязательно)"}
          hint={
            needsInstruction
              ? "Скан закончился сам, поэтому без новой инструкции ему нечего продолжать. Например: «Проверь подробнее админку и работу с токенами»."
              : "Скан был остановлен или упал — можно просто продолжить с того же места, а можно добавить указание."
          }
        >
          <textarea
            className="field h-28 resize-y text-sm"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            maxLength={10000}
            autoFocus
            required={needsInstruction}
          />
        </Field>

        {needsProfile && (
          <Field label="LLM-профиль">
            <select className="field" value={profileId ?? ""} onChange={(e) => setProfileId(e.target.value ? Number(e.target.value) : null)} required>
              {profiles === null && <option value="">Загрузка…</option>}
              {profiles?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {p.strix_model}
                </option>
              ))}
            </select>
            {profiles?.length === 0 && <span className="mt-1.5 block text-[11px] text-signal-warn">У вас нет профилей — добавьте ключ в разделе «LLM-профили».</span>}
          </Field>
        )}

        <Field
          label="Лимит бюджета на весь прогон, $ (необязательно)"
          hint={spent !== null && spent > 0 ? `Strix считает лимит по всему прогону, а не по продолжению. Уже потрачено ${formatCost(spent)}.` : "Strix считает лимит по всему прогону, а не по продолжению."}
        >
          <input className="field font-mono" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="без лимита" aria-invalid={budgetError !== null} />
        </Field>
        {budgetError && <p className="text-xs text-signal-bad">{budgetError}</p>}

        <ErrorNote message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>Отмена</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!canSubmit}>Продолжить</Button>
        </div>
      </form>
    </Modal>
  );
}
