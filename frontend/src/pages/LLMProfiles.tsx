import clsx from "clsx";
import { motion } from "framer-motion";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, Spinner, rise, stagger } from "../components/ui";
import {
  createProfile,
  deleteProfile,
  errorMessage,
  fetchModels,
  listPresets,
  listProfiles,
  updateProfile,
  type LLMProfile,
  type Provider,
  type ProviderPreset,
} from "../lib/api";
import { canFetchModels as computeCanFetchModels, mergeModelOptions, modelHint } from "../lib/llmProfile";

interface Form {
  name: string;
  provider: Provider;
  model: string;
  base_url: string;
  api_key: string;
}
const emptyForm: Form = { name: "", provider: "claude", model: "", base_url: "", api_key: "" };

export default function LLMProfiles() {
  const [profiles, setProfiles] = useState<LLMProfile[] | null>(null);
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<LLMProfile | null>(null);
  const [form, setForm] = useState<Form>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);

  // "Загрузить модели": a model list fetched from the provider, offered as datalist
  // options alongside the preset's own suggestions — the field stays free text either way.
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [modelsBusy, setModelsBusy] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const resetFetchedModels = () => {
    setFetchedModels([]);
    setModelsError(null);
  };

  const reload = useCallback(async () => {
    try {
      const [p, pr] = await Promise.all([listProfiles(), listPresets()]);
      setProfiles(p);
      setPresets(pr);
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const preset = presets.find((p) => p.provider === form.provider);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setFormError(null);
    resetFetchedModels();
    setOpen(true);
  };
  const openEdit = (p: LLMProfile) => {
    setEditing(p);
    setForm({ name: p.name, provider: p.provider, model: p.model, base_url: p.base_url ?? "", api_key: "" });
    setFormError(null);
    resetFetchedModels();
    setOpen(true);
  };

  const canFetch = computeCanFetchModels({
    provider: form.provider,
    baseUrl: form.base_url,
    apiKey: form.api_key,
    editingProvider: editing?.provider ?? null,
  });

  const doFetchModels = async () => {
    setModelsError(null);
    setModelsBusy(true);
    try {
      const key = form.api_key.trim();
      const reuseProfile = !key && editing && editing.provider === form.provider;
      const list = await fetchModels({
        provider: form.provider,
        base_url: form.provider === "custom" ? form.base_url.trim() || null : null,
        ...(key ? { api_key: key } : reuseProfile ? { profile_id: editing.id } : {}),
      });
      setFetchedModels(list);
    } catch (e) {
      setFetchedModels([]);
      setModelsError(errorMessage(e));
    } finally {
      setModelsBusy(false);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setBusy(true);
    try {
      const base_url = form.base_url.trim() || null;
      if (editing) {
        await updateProfile(editing.id, {
          name: form.name,
          provider: form.provider,
          model: form.model,
          base_url,
          ...(form.api_key.trim() ? { api_key: form.api_key } : {}),
        });
      } else {
        await createProfile({ name: form.name, provider: form.provider, model: form.model, base_url, api_key: form.api_key });
      }
      setForm((f) => ({ ...f, api_key: "" })); // don't keep the secret in memory
      setOpen(false);
      await reload();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: LLMProfile) => {
    if (!window.confirm(`Удалить профиль «${p.name}»?`)) return;
    setDeleting(p.id);
    try {
      await deleteProfile(p.id);
      await reload();
    } catch (e) {
      setLoadError(errorMessage(e));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <>
      <PageHeader
        title="LLM-профили"
        subtitle="Личные ключи и модели. Ключи шифруются и никогда не показываются повторно."
        action={<Button variant="primary" onClick={openCreate}>+ Новый профиль</Button>}
      />
      <ErrorNote message={loadError} />

      {profiles === null && !loadError && (
        <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>
      )}

      {profiles?.length === 0 && (
        <EmptyState
          title="Профилей пока нет"
          text="Добавьте ключ Claude, OpenAI, Grok или свой OpenAI-совместимый endpoint — профиль выбирается при запуске скана."
          action={<Button variant="primary" onClick={openCreate}>Добавить профиль</Button>}
        />
      )}

      {profiles && profiles.length > 0 && (
        <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(22rem,1fr))] [&>*]:max-w-[44rem]">
          {profiles.map((p) => (
            <motion.div key={p.id} variants={rise}>
              <Card className="flex h-full flex-col p-5 transition-colors hover:border-accent/30">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="truncate text-sm font-semibold text-white">{p.name}</h3>
                    <p className="mt-0.5 truncate font-mono text-xs text-slate-500">{p.strix_model}</p>
                  </div>
                  <Badge tone="accent">{presets.find((x) => x.provider === p.provider)?.label ?? p.provider}</Badge>
                </div>
                <dl className="mt-4 space-y-1.5 text-xs">
                  <div className="flex justify-between gap-4">
                    <dt className="text-slate-500">Ключ</dt>
                    <dd className="font-mono text-slate-300">{p.api_key_hint}</dd>
                  </div>
                  {p.base_url && (
                    <div className="flex justify-between gap-4">
                      <dt className="text-slate-500">Endpoint</dt>
                      <dd className="truncate font-mono text-slate-300">{p.base_url}</dd>
                    </div>
                  )}
                </dl>
                <div className="mt-5 flex gap-2 pt-1">
                  <Button onClick={() => openEdit(p)} className="flex-1">Изменить</Button>
                  <Button variant="danger" loading={deleting === p.id} onClick={() => remove(p)}>Удалить</Button>
                </div>
              </Card>
            </motion.div>
          ))}
        </motion.div>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title={editing ? "Изменить профиль" : "Новый профиль"}>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Название">
            <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Рабочий Claude" required maxLength={100} />
          </Field>

          <div>
            <span className="mb-1.5 block text-xs font-medium text-slate-400">Провайдер</span>
            <div className="rounded-xl bg-ink-800/80 p-1">
              {/* The sliding highlight is a plain CSS transform: a framer-motion
                  layoutId inside this exiting modal left it stuck open. */}
              <div className="relative grid grid-cols-4">
                <span
                  aria-hidden
                  className="absolute inset-y-0 left-0 w-1/4 p-0 transition-transform duration-300 ease-out"
                  style={{ transform: `translateX(${Math.max(0, presets.findIndex((p) => p.provider === form.provider)) * 100}%)` }}
                >
                  <span className="block h-full w-full rounded-lg border border-accent/40 bg-accent/15" />
                </span>
                {presets.map((p) => (
                  <button
                    type="button"
                    key={p.provider}
                    onClick={() => {
                      setForm({ ...form, provider: p.provider });
                      resetFetchedModels();
                    }}
                    className={clsx(
                      "relative rounded-lg px-2 py-2 text-xs font-medium transition-colors",
                      form.provider === p.provider ? "text-white" : "text-slate-500 hover:text-slate-300"
                    )}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
            {preset && <p className="mt-1.5 text-[11px] text-slate-500">{preset.hint}</p>}
          </div>

          <Field
            label={preset?.base_url_required ? "Base URL" : "Base URL (необязательно)"}
            hint={preset?.base_url_required ? "OpenAI-совместимый endpoint, например http://host:11434/v1" : "Только если ходите через прокси или шлюз"}
          >
            <input className="field font-mono" value={form.base_url} onChange={(e) => { setForm({ ...form, base_url: e.target.value }); resetFetchedModels(); }} placeholder="https://…" required={preset?.base_url_required} />
          </Field>

          <Field label="API-ключ" hint={editing ? `Сейчас: ${editing.api_key_hint}. Оставьте пустым, чтобы не менять.` : undefined}>
            <input className="field font-mono" type="password" autoComplete="off" value={form.api_key} onChange={(e) => { setForm({ ...form, api_key: e.target.value }); resetFetchedModels(); }} placeholder={editing ? "••••••••" : "sk-…"} required={!editing} />
          </Field>

          <Field label="Модель" hint={modelHint(form.provider, form.model)}>
            <input className="field font-mono" list="model-suggestions" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder={preset?.model_placeholder} required maxLength={200} />
            <datalist id="model-suggestions">
              {mergeModelOptions(preset?.suggestions ?? [], fetchedModels).map((s) => <option key={s} value={s} />)}
            </datalist>
          </Field>

          <div className="flex flex-wrap items-center gap-2 -mt-1.5">
            <Button
              type="button"
              className="px-3 py-1.5 text-xs"
              loading={modelsBusy}
              disabled={!canFetch}
              title={!canFetch ? (preset?.base_url_required ? "Укажите base_url и ключ" : "Введите API-ключ") : undefined}
              onClick={doFetchModels}
            >
              Загрузить модели
            </Button>
            {!modelsBusy && !modelsError && fetchedModels.length > 0 && (
              <span className="text-[11px] text-slate-500">Загружено {fetchedModels.length} — выберите из списка в поле «Модель» или впишите свою</span>
            )}
          </div>
          <ErrorNote message={modelsError} />

          <ErrorNote message={formError} />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setOpen(false)}>Отмена</Button>
            <Button type="submit" variant="primary" loading={busy}>{editing ? "Сохранить" : "Создать"}</Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
