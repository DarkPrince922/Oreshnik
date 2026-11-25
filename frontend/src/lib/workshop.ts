import type { McpServer, PersonaScope, Skill } from "./api";

type Where = Exclude<PersonaScope, "both">;

/** Match the API: filter by scope and selection before resolving duplicate names. */
export function skillsForPersona(skills: Skill[], persona: { skills: string[] | null } | null | undefined, where: Where): Skill[] {
  const wanted = persona?.skills == null ? null : new Set(persona.skills);
  const chosen = new Map<string, Skill>();
  for (const skill of skills) {
    if (skill.scope !== "both" && skill.scope !== where) continue;
    if (wanted ? !wanted.has(skill.name) : !skill.enabled) continue;
    const previous = chosen.get(skill.name);
    if (!previous || (!skill.shared && previous.shared) || (skill.shared === previous.shared && skill.id > previous.id)) {
      chosen.set(skill.name, skill);
    }
  }
  return [...chosen.values()].sort((a, b) => a.name.localeCompare(b.name));
}

export function effectiveSkills(skills: Skill[], where: Where): Skill[] {
  return skillsForPersona(skills, null, where);
}

/** The same for MCP servers. */
export function effectiveMcp(servers: McpServer[], where: Where): McpServer[] {
  return servers.filter((s) => s.enabled && !s.overridden && (s.scope === "both" || s.scope === where)).sort((a, b) => a.name.localeCompare(b.name));
}

export type McpHealth = "ok" | "bad" | "unknown";
export function mcpHealth(server: McpServer): McpHealth {
  return !server.last_check ? "unknown" : server.last_check.ok ? "ok" : "bad";
}

/** A `$name` being typed right before the caret: the part after `$` and where the `$` is; null when the caret is not in one. */
export function skillMention(text: string, caret: number): { query: string; start: number } | null {
  const m = /(^|\s)\$([a-z0-9-]*)$/.exec(text.slice(0, caret));
  return m ? { query: m[2], start: caret - m[2].length - 1 } : null;
}

/** The text with the `$…` being typed replaced by `$name ` and where the caret goes after it. */
export function insertSkill(text: string, start: number, caret: number, name: string): { text: string; caret: number } {
  const rest = text.slice(caret).replace(/^[a-z0-9-]*/, "");
  const inserted = `$${name} `;
  const after = rest.startsWith(" ") ? rest.slice(1) : rest;
  return { text: text.slice(0, start) + inserted + after, caret: start + inserted.length };
}

/** Skills matching what is typed after `$`: names that start with it first, then names that contain it, then descriptions. */
export function matchSkills(skills: Skill[], query: string, limit = 8): Skill[] {
  const q = query.toLowerCase();
  const rank = (s: Skill) => (s.name.startsWith(q) ? 0 : s.name.includes(q) ? 1 : s.description.toLowerCase().includes(q) ? 2 : 3);
  return skills.filter((s) => rank(s) < 3).sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name)).slice(0, limit);
}

// ── SKILL.md by fields ───────────────────────────────────────────────────────

export interface SkillFields {
  name: string;
  description: string;
  body: string;
  /** Other front-matter lines (license, metadata…) kept as they are. */
  extra: string;
}

const FRONT = /^﻿?---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

function unquote(v: string): string {
  const t = v.trim();
  if (t.length >= 2 && t[0] === '"' && t.endsWith('"')) {
    try {
      return JSON.parse(t) as string;
    } catch {
      return t.slice(1, -1);
    }
  }
  if (t.length >= 2 && t[0] === "'" && t.endsWith("'")) return t.slice(1, -1).replace(/''/g, "'");
  return t;
}

/** SKILL.md split into the fields the editor shows. A block value (`description: >` and indented lines) is read as one line. */
export function splitSkillMd(md: string): SkillFields {
  const m = FRONT.exec(md);
  if (!m) return { name: "", description: "", body: md.trim(), extra: "" };
  const lines = m[1].split(/\r?\n/);
  const out: SkillFields = { name: "", description: "", body: md.slice(m[0].length).replace(/^\n+/, ""), extra: "" };
  const extra: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const first = i;
    const km = /^([A-Za-z0-9_-]+):[ \t]*(.*)$/.exec(lines[i]);
    const cont: string[] = [];
    while (i + 1 < lines.length && (/^[ \t]/.test(lines[i + 1]) || !lines[i + 1].trim())) cont.push(lines[++i].trim());
    if (km && (km[1] === "name" || km[1] === "description")) {
      const value = /^[|>]/.test(km[2]) ? cont.filter(Boolean).join(" ") : [km[2], ...cont.filter(Boolean)].join(" ");
      out[km[1]] = unquote(value);
    } else {
      extra.push(lines.slice(first, i + 1).join("\n"));
    }
  }
  out.extra = extra.join("\n").trim();
  return out;
}

export function composeSkillMd(f: SkillFields): string {
  const head = [`name: ${f.name.trim()}`, `description: ${JSON.stringify(f.description.replace(/\s+/g, " ").trim())}`, f.extra.trim()].filter(Boolean).join("\n");
  return `---\n${head}\n---\n\n${f.body.trim()}\n`;
}

export const SKILL_TEMPLATES: { title: string; hint: string; fields: SkillFields }[] = [
  {
    title: "Разведка",
    hint: "поддомены, порты, живые хосты",
    fields: {
      name: "recon",
      description: "Разведка цели: поддомены, открытые порты, живые веб-сервисы. Применяй, когда пользователь просит изучить домен или диапазон адресов.",
      body: "# Разведка\n\n1. Поддомены: `subfinder -d <домен> -silent`.\n2. Живые веб-сервисы: `httpx -silent -title -status-code` по списку.\n3. Порты интересных хостов: `nmap -sV --top-ports 1000 <хост>`.\n4. Итог сохрани в recon/<цель>.md: что нашёл и что выглядит интересно.\n\n## Правила\n- Только цели, которые назвал пользователь.",
      extra: "",
    },
  },
  {
    title: "Проверка уязвимости",
    hint: "шаги проверки и что приложить",
    fields: {
      name: "verify-finding",
      description: "Как подтвердить найденную веб-уязвимость и оформить доказательства. Применяй, когда есть подозрение на уязвимость и его надо проверить.",
      body: "# Проверка уязвимости\n\n1. Сформулируй гипотезу: что за уязвимость и где.\n2. Воспроизведи минимальным запросом; сохрани запрос и ответ целиком.\n3. Убедись, что это не ложное срабатывание: повтори с контрольным значением.\n4. Оцени влияние: что может сделать атакующий.\n\n## Что приложить\n- Запрос и ответ, шаги воспроизведения, оценку CVSS.",
      extra: "",
    },
  },
  {
    title: "Отчёт",
    hint: "шаблон находки с CVSS",
    fields: {
      name: "finding-report",
      description: "Оформление находки для отчёта заказчику. Применяй, когда пользователь просит описать или записать найденную уязвимость.",
      body: "# Находка\n\nПиши по шаблону:\n\n## Название\n## Критичность (CVSS 3.1, вектор)\n## Где\n## Описание\n## Шаги воспроизведения\n## Доказательства (запрос/ответ)\n## Рекомендации",
      extra: "",
    },
  },
  { title: "Пустой", hint: "свои правила с нуля", fields: { name: "my-skill", description: "", body: "# Название\n\n1. …", extra: "" } },
];
