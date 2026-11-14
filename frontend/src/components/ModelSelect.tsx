import { useState } from "react";

import type { LLMProfile } from "../lib/api";
import { decodeChoice, encodeChoice, totalModels, visibleModels, type Choice } from "../lib/modelChoice";

const PROVIDER: Record<string, string> = { claude: "Claude", openai: "OpenAI", grok: "Grok", custom: "свой endpoint" };

/** One select for "which model, of which provider": every model of every profile the user has, grouped by profile. With many
 * models (a provider can serve hundreds) a search box narrows the list. */
export default function ModelSelect({
  profiles,
  value,
  onChange,
  required = false,
  noneLabel,
  label,
}: {
  profiles: LLMProfile[];
  value: Choice | null;
  onChange: (c: Choice | null) => void;
  required?: boolean;
  /** When set, the select offers "no profile" under this label (a simulated runner needs none). */
  noneLabel?: string;
  label?: string;
}) {
  const [query, setQuery] = useState("");
  const total = totalModels(profiles);
  return (
    <div className="space-y-2">
      {total > 12 && (
        <input className="field" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Найти модель среди ${total}…`} aria-label="Поиск модели" />
      )}
      <select
        className="field"
        value={value ? encodeChoice(value) : ""}
        onChange={(e) => onChange(e.target.value ? decodeChoice(e.target.value) : null)}
        required={required}
        aria-label={label ?? "Модель"}
      >
        {noneLabel !== undefined && <option value="">{noneLabel}</option>}
        {profiles.map((p) => {
          const models = visibleModels(p, query, value, p.id);
          if (models.length === 0) return null;
          return (
            <optgroup key={p.id} label={`${p.name} · ${PROVIDER[p.provider] ?? p.provider}`}>
              {models.map((m) => (
                <option key={m} value={encodeChoice({ profileId: p.id, model: m })}>
                  {m}
                  {m === p.model ? "  (по умолчанию)" : ""}
                </option>
              ))}
            </optgroup>
          );
        })}
      </select>
    </div>
  );
}
