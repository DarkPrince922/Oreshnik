import { Link } from "react-router-dom";

import type { Persona } from "../../lib/api";
import { personasFor } from "../../lib/personas";
import { Field } from "../ui";

/** Picking who the agent is, for a session of «Код». `current`: the name of a persona the session has but the list no longer offers. */
export default function PersonaField({ personas, value, onChange, current }: { personas: Persona[]; value: number | null; onChange: (id: number | null) => void; current?: string | null }) {
  const options = personasFor(personas, "code");
  const missing = value !== null && !options.some((p) => p.id === value);
  return (
    <Field label="Личность" hint="Кем быть агенту: её текст уходит ему первым, до инструкций сессии. Свои личности — в «Мастерской».">
      <select className="field" value={value ?? ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
        <option value="">— без личности —</option>
        {missing && <option value={value ?? ""}>{current ?? "текущая"}</option>}
        {options.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
            {p.shared ? " (общая)" : ""}
            {p.description ? ` — ${p.description}` : ""}
          </option>
        ))}
      </select>
      {options.length === 0 && (
        <span className="mt-1.5 block text-[11px] text-slate-400">
          Личностей пока нет — <Link to="/workshop?tab=personas" className="text-accent-soft underline decoration-dotted">создать</Link>.
        </span>
      )}
    </Field>
  );
}
