import clsx from "clsx";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { MODE_META } from "../components/scan";
import { Button, Card, ErrorNote, Field, PageHeader, Spinner } from "../components/ui";
import ModelSelect from "../components/ModelSelect";
import { createScan, errorMessage, listProfiles, listRunners, type LLMProfile, type Runner, type ScanMode } from "../lib/api";
import { defaultChoice, modelToSend, type Choice } from "../lib/modelChoice";
import { useAuth } from "../lib/auth";
import { parseTargets } from "../lib/targets";

const MODES: ScanMode[] = ["quick", "standard", "deep"];
const KIND_LABEL = { mock: "симуляция", local: "этот сервер", ssh: "SSH" } as const;
const MOCK_DIRECTIVES = [
  { tag: "MOCK:clean", hint: "без находок" },
  { tag: "MOCK:fail", hint: "завершится ошибкой" },
  { tag: "MOCK:slow", hint: "долгий — чтобы попробовать отмену" },
];

export default function NewScan() {
  const navigate = useNavigate();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [runners, setRunners] = useState<Runner[] | null>(null);
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [targetsText, setTargetsText] = useState("");
  const [mode, setMode] = useState<ScanMode>("quick");
  const [runnerId, setRunnerId] = useState<number | null>(null);
  const [choice, setChoice] = useState<Choice | null>(null);
  const profileId = choice?.profileId ?? null;
  const [budget, setBudget] = useState("");
  const [instruction, setInstruction] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([listRunners(), listProfiles()])
      .then(([r, p]) => {
        if (!alive) return;
        setRunners(r);
        setProfiles(p);
        const preferred = r.find((x) => x.status === "online" && x.kind !== "ssh") ?? r.find((x) => x.kind !== "ssh");
        setRunnerId(preferred?.id ?? null);
        setChoice(defaultChoice(p));
      })
      .catch((e) => alive && setLoadError(errorMessage(e)));
    return () => {
      alive = false;
    };
  }, []);

  const runner = runners?.find((r) => r.id === runnerId) ?? null;
  const needsProfile = runner !== null && runner.kind !== "mock";
  const targets = useMemo(() => parseTargets(targetsText), [targetsText]);
  const budgetValue = budget.trim() === "" ? null : Number(budget);
  const budgetInvalid = budgetValue !== null && !(budgetValue > 0);
  const canSubmit = targets.length > 0 && runner !== null && !budgetInvalid && (!needsProfile || profileId !== null) && !busy;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit || !runner) return;
    setError(null);
    setBusy(true);
    try {
      const scan = await createScan({
        targets,
        scan_mode: mode,
        instruction: instruction.trim() || null,
        max_budget: budgetValue,
        runner_id: runner.id,
        llm_profile_id: needsProfile || profileId !== null ? profileId : null,
        llm_model: modelToSend(profiles ?? [], choice),
      });
      navigate(`/scans/${scan.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  if (loadError) return <ErrorNote message={loadError} />;
  if (!runners || !profiles) return <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>;

  return (
    <div className="w-full">
      <PageHeader title="Новый скан" subtitle="Тестируйте только системы, которыми владеете или на которые есть письменное разрешение." />
      <form onSubmit={submit} className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="space-y-6">
          <Card className="space-y-5 p-6">
            <Field
              label={`Цели${targets.length ? ` (${targets.length})` : ""}`}
              hint={isAdmin ? "URL, домен, IP, git-репозиторий; как администратор можете указывать и локальные пути. По одной на строку." : "URL, домен, IP или git-репозиторий. По одной на строку."}
            >
              <textarea className="field h-28 resize-y font-mono text-xs" value={targetsText} onChange={(e) => setTargetsText(e.target.value)} placeholder={"https://app.example.com\napi.example.com\n192.168.1.10"} spellCheck={false} autoFocus required />
            </Field>

            <div>
              <span className="mb-1.5 block text-xs font-medium text-slate-400">Режим</span>
              <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="Режим скана">
                {MODES.map((m) => (
                  <button
                    type="button"
                    role="radio"
                    aria-checked={mode === m}
                    key={m}
                    onClick={() => setMode(m)}
                    className={clsx(
                      "rounded-xl border px-3 py-2.5 text-left transition-all duration-200",
                      mode === m ? "border-accent/50 bg-accent/15 shadow-[0_0_24px_-8px_rgba(124,92,255,0.6)]" : "border-white/10 bg-white/5 hover:border-white/20"
                    )}
                  >
                    <span className={clsx("block text-sm font-medium", mode === m ? "text-white" : "text-slate-300")}>{MODE_META[m].label}</span>
                    <span className="mt-0.5 block text-[11px] text-slate-500">{m}</span>
                  </button>
                ))}
              </div>
              <p className={clsx("mt-2 text-[11px] leading-relaxed", mode === "deep" ? "text-signal-warn" : "text-slate-500")}>{MODE_META[mode].hint}</p>
            </div>

            <Field label="Инструкция (необязательно)" hint="Например: «Сфокусируйтесь на аутентификации» или тестовые учётные данные. Видна только вам и администраторам.">
              <textarea className="field h-24 resize-y text-sm" value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={10000} />
            </Field>
            {runner?.kind === "mock" && (
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                Для mock-раннера:
                {MOCK_DIRECTIVES.map((d) => (
                  <button type="button" key={d.tag} title={d.hint} onClick={() => setInstruction((v) => (v ? `${v.trim()} ${d.tag}` : d.tag))} className="rounded-md border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-slate-400 transition hover:border-accent/40 hover:text-white">
                    {d.tag}
                  </button>
                ))}
              </div>
            )}
          </Card>
        </div>

        <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
          <Card className="space-y-5 p-6">
            <Field label="Раннер" hint={runner ? { mock: "Симуляция: токены не тратятся.", local: "Настоящий Strix с Docker на сервере панели.", ssh: "Strix запустится на удалённом хосте. Ключ LLM передаётся туда в окружение процесса Strix; результаты возвращаются в панель." }[runner.kind] : undefined}>
              <select className="field" value={runnerId ?? ""} onChange={(e) => setRunnerId(Number(e.target.value))} required>
                {runners.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name} · {KIND_LABEL[r.kind]}{r.status !== "online" ? ` · ${r.status}` : ""}
                  </option>
                ))}
              </select>
            </Field>
            {runner && runner.kind !== "mock" && runner.status !== "online" && (
              <p className="rounded-lg border border-signal-warn/20 bg-signal-warn/10 px-3 py-2 text-xs text-signal-warn">
                Раннер сейчас не online{runner.status_detail ? `: ${runner.status_detail}` : ""}. Проверьте его на странице «Раннеры».
              </p>
            )}

            <Field
              label={needsProfile ? "Модель" : "Модель (для mock не нужна)"}
              hint="Любая модель любого вашего профиля. Список пополняется кнопкой «Загрузить модели» в разделе «LLM-профили»."
            >
              <ModelSelect profiles={profiles} value={choice} onChange={setChoice} required={needsProfile} noneLabel={needsProfile ? undefined : "— не использовать —"} label="Модель" />
            </Field>
            {needsProfile && profiles.length === 0 && (
              <p className="text-xs text-signal-warn">Нет ни одного профиля. <Link to="/llm" className="underline">Добавьте ключ</Link>.</p>
            )}

            <Field label="Лимит бюджета, $ (необязательно)" hint="Strix остановит скан при достижении лимита.">
              <input className="field font-mono" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="5" aria-invalid={budgetInvalid} />
            </Field>
            {needsProfile && mode === "deep" && budgetValue === null && (
              <p className="text-xs text-signal-warn">Глубокий скан без лимита бюджета может обойтись дорого.</p>
            )}
            {budgetInvalid && <p className="text-xs text-signal-bad">Укажите положительное число.</p>}

            <ErrorNote message={error} />
            <Button type="submit" variant="primary" loading={busy} disabled={!canSubmit} className="w-full py-3">
              Запустить скан
            </Button>
          </Card>
        </aside>
      </form>
    </div>
  );
}
