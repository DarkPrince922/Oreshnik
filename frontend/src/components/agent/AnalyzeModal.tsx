import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { createAgentFromScan, errorMessage, listProfiles, type LLMProfile } from "../../lib/api";
import { usableProfiles } from "../../lib/agentModel";
import { defaultChoice, modelToSend, type Choice } from "../../lib/modelChoice";
import ModelSelect from "../ModelSelect";
import { Button, ErrorNote, Field, Modal, Spinner } from "../ui";

/** «Разобрать в агенте»: a new agent session with this scan's findings and report already in its folder, and a task proposed to start with. */
export default function AnalyzeModal({ open, scanId, onClose }: { open: boolean; scanId: number; onClose: () => void }) {
  const navigate = useNavigate();
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    listProfiles()
      .then((all) => {
        const usable = usableProfiles(all);
        setProfiles(usable);
        setChoice(defaultChoice(usable));
      })
      .catch((e) => setError(errorMessage(e)));
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!choice || !profiles) return;
    setBusy(true);
    setError(null);
    try {
      const result = await createAgentFromScan(scanId, { llm_profile_id: choice.profileId, llm_model: modelToSend(profiles, choice) });
      onClose();
      navigate("/agent", { state: { sessionId: result.session.id, draft: result.draft } });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Разобрать скан в агенте">
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm leading-relaxed text-slate-400">
          Создадим сессию агента и положим в её папку находки скана, отчёт и файлы Strix (папка <span className="font-mono text-slate-300">scan/</span>). Первое сообщение —
          готовое задание, вы прочитаете его и отправите сами.
        </p>
        {profiles === null && !error && <Spinner />}
        {profiles && profiles.length === 0 && (
          <p className="text-sm text-signal-warn">
            Нужен профиль LLM с OpenAI, Grok или custom (Responses API). Профили Claude агент не поддерживает. <Link to="/llm" className="underline">Добавить профиль</Link>
          </p>
        )}
        {profiles && profiles.length > 0 && (
          <Field label="Модель">
            <ModelSelect profiles={profiles} value={choice} onChange={setChoice} required label="Модель" />
          </Field>
        )}
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!choice}>
            Создать сессию
          </Button>
        </div>
      </form>
    </Modal>
  );
}
