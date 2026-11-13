/**
 * Helpers for showing Strix finding fields.
 *
 * Every value here originates from an LLM that read the scanned target, so it is untrusted. These
 * helpers only turn values into *strings*; they are always rendered as React text nodes
 * (never as HTML), so markup inside them is displayed literally.
 */

export function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const FENCE = /^\s*(`{3,})[^\n`]*\r?\n([\s\S]*?)\r?\n?\1\s*$/;

/** Strix stores PoC code wrapped in a markdown fence (```bash ... ```); show just the code. */
export function stripFence(code: string): string {
  const match = FENCE.exec(code);
  return match ? match[2] : code;
}

export interface CodeLocation {
  file: string;
  lines: string;
  label: string;
  snippet: string;
}

export function codeLocations(value: unknown): CodeLocation[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((v): v is Record<string, unknown> => typeof v === "object" && v !== null)
    .map((loc) => {
      const start = loc.start_line;
      const end = loc.end_line;
      const lines =
        start === undefined || start === null ? "" : end && end !== start ? `${asText(start)}–${asText(end)}` : asText(start);
      return { file: asText(loc.file) || "unknown", lines, label: asText(loc.label), snippet: asText(loc.snippet) };
    });
}

/** Non-empty text of the first present key, for fields that Strix may name differently across versions. */
export function pick(data: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const text = asText(data[key]).trim();
    if (text) return text;
  }
  return "";
}
