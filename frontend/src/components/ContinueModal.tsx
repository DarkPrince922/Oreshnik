import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../lib/auth";

import ModelSelect from "./ModelSelect";
import { continueScan, errorMessage, listProfiles, type LLMProfile, type Scan } from "../lib/api";
import { modelToSend, resolveChoice, type Choice } from "../lib/modelChoice";
import { budgetProblem, canContinueInPlace, defaultBudget, instructionRequired, parseBudget, type ContinueKind } from "../lib/continueScan";
import { formatCost } from "../lib/format";
import { Button, ErrorNote, Field, Modal } from "./ui";

/**
 * "Продолжить" — `strix --resume` for a finished or stopped scan.
 *
 * By default the scan continues IN PLACE: the same scan (same number, log, agents and findings) goes back to work and the
 * instruction reaches the root agent. Optionally a NEW scan is created on a copy of the run, and this one stays as it is.
 *
 * `kind="restart"`: the scan failed or was stopped before Strix created a run (e.g. the model was unreachable), so there is
 * nothing to resume — the same scan starts again from the beginning. No instruction is asked for: it keeps its own.
 */
export default function ContinueModal({
  scan,
  kind = "resume",
  open,
  onClose,
  onContinued,
}: {
  scan: Scan;
  kind?: ContinueKind;
  open: boolean;
  onClose: () => void;
  /** The scan went back to work in place (it is the same scan, updated). */
  onContinued: (scan: Scan) => void;
}) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const inPlaceAllowed = canContinueInPlace(scan, user?.id);
  const [asNew, setAsNew] = useState(false);
  const restart = kind === "restart";
  const newScan = (asNew && !restart) || !inPlaceAllowed;
  const needsInstruction = !restart && instructionRequired(scan);
  const needsProfile = scan.runner_kind !== null && scan.runner_kind !== "mock";

  const [instruction, setInstruction] = useState("");
  const [budget, setBudget] = useState(() => defaultBudget(scan));
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [choice, setChoice] = useState<Choice | null>(null);
  const profileId = choice?.profileId ?? null;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Fresh form each time it opens; the profiles are fetched lazily.
  useEffect(() => {
    if (!open) return;
    setInstruction("");
    setBudget(defaultBudget(scan));
    setAsNew(false);
    setError(null);
    setBusy(false);
    if (!needsProfile) return;
    let alive = true;
    listProfiles()
      .then((list) => {
        if (!alive) return;
        setProfiles(list);
        // The scan's own profile if it is one of yours, otherwise the first you have.
        setChoice(resolveChoice(list, scan.llm_profile_id, scan.llm_model));
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
      const result = await continueScan(scan.id, {
        instruction: restart ? null : instruction.trim() || null,
        max_budget: parseBudget(budget),
        llm_profile_id: needsProfile ? profileId : null,
        llm_model: needsProfile ? modelToSend(profiles ?? [], choice) : null,
        as_new_scan: newScan,
      });
      onClose();
      setBusy(false); // the dialog stays mounted on the same page when the scan continues in place
      if (newScan) navigate(`/scans/${result.id}`);
      else onContinued(result);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={`Продолжить скан #${scan.id}`}>
      <form onSubmit={submit} className="space-y-4">
        <p className="rounded-lg border border-white/10 bg-white/5 px-3.5 py-3 text-xs leading-relaxed text-slate-400" data-testid="continue-explainer">
          {restart ? (
            <>
              Скан не дошёл до старта Strix (например, модель была недоступна), поэтому продолжать нечего:{" "}
              <span className="text-slate-200">он запустится заново</span> — с теми же целями, режимом и инструкцией.{" "}
              {newScan ? "Будет создан новый скан, а этот останется как есть." : "Номер скана тот же, лог дописывается в конец."}
            </>
          ) : (
            <>Strix продолжит этот же прогон: агенты помнят всё, что уже сделали, а ваша инструкция уйдёт корневому агенту. </>
          )}
          {restart ? null : newScan ? (
            <>
              Создаётся <span className="text-slate-200">новый скан</span>, а этот остаётся без изменений. Находки и стоимость в
              новом скане накопительные — включают то, что найдено здесь.
            </>
          ) : (
            <>
              Скан продолжится <span className="text-slate-200">здесь же</span>: тот же номер, лог и находки, новое дописывается в
              конец. Стоимость и находки накопительные.
            </>
          )}
        </p>

        {!restart && (
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
        )}

        {restart && !inPlaceAllowed && <p className="text-xs text-signal-warn">Это чужой скан: он запустится как новый скан на вашем ключе.</p>}

        {!restart && (
        <label className="flex items-start gap-3 text-xs leading-relaxed text-slate-400">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4 shrink-0 accent-[#7c5cff]"
            checked={newScan}
            disabled={!inPlaceAllowed}
            onChange={(e) => setAsNew(e.target.checked)}
          />
          <span>
            <span className="text-slate-200">Создать новый скан</span> вместо продолжения в этом — например, чтобы проверить другое
            направление и сохранить этот скан как есть.
            {!inPlaceAllowed && (
              <span className="mt-1 block text-signal-warn">Это чужой скан: он продолжится как новый скан на вашем ключе.</span>
            )}
          </span>
        </label>
        )}

        {needsProfile && (
          <Field label="Модель">
            {profiles === null ? (
              <p className="text-xs text-slate-500">Загрузка…</p>
            ) : (
              <ModelSelect profiles={profiles} value={choice} onChange={setChoice} required label="Модель" />
            )}
            {profiles?.length === 0 && <span className="mt-1.5 block text-[11px] text-signal-warn">У вас нет профилей — добавьте ключ в разделе «LLM-профили».</span>}
          </Field>
        )}

        <Field
          label={restart ? "Лимит бюджета, $ (необязательно)" : "Лимит бюджета на весь прогон, $ (необязательно)"}
          hint={restart ? undefined : spent !== null && spent > 0 ? `Strix считает лимит по всему прогону, а не по продолжению. Уже потрачено ${formatCost(spent)}.` : "Strix считает лимит по всему прогону, а не по продолжению."}
        >
          <input className="field font-mono" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="без лимита" aria-invalid={budgetError !== null} />
        </Field>
        {budgetError && <p className="text-xs text-signal-bad">{budgetError}</p>}

        <ErrorNote message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>Отмена</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!canSubmit}>{restart ? "Запустить заново" : "Продолжить"}</Button>
        </div>
      </form>
    </Modal>
  );
}
