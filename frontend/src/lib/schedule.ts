/** Pure helpers for the schedule form and list. */

export interface CronPreset {
  label: string;
  cron: string;
}

export const CRON_PRESETS: CronPreset[] = [
  { label: "Каждый час", cron: "0 * * * *" },
  { label: "Каждый день в 03:00", cron: "0 3 * * *" },
  { label: "По будням в 09:00", cron: "0 9 * * 1-5" },
  { label: "По понедельникам в 03:00", cron: "0 3 * * 1" },
  { label: "1-го числа в 03:00", cron: "0 3 1 * *" },
];

/** Whitespace-insensitive form of a cron expression, as the server stores it. */
export const normalizeCronInput = (text: string): string => text.trim().split(/\s+/).filter(Boolean).join(" ");

/** A friendly name when the expression is one of the presets, otherwise null. */
export function describeCron(cron: string): string | null {
  const wanted = normalizeCronInput(cron);
  return CRON_PRESETS.find((p) => p.cron === wanted)?.label ?? null;
}

const COMMON_ZONES = [
  "UTC",
  "Europe/Moscow",
  "Europe/Kyiv",
  "Europe/Minsk",
  "Europe/Berlin",
  "Europe/London",
  "Asia/Almaty",
  "Asia/Tashkent",
  "Asia/Dubai",
  "Asia/Tokyo",
  "America/New_York",
  "America/Los_Angeles",
];

export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** The zones offered in the form: the browser's own first, then the common ones, plus the schedule's current zone. */
export function zoneOptions(current?: string): string[] {
  return [...new Set([current, browserZone(), ...COMMON_ZONES].filter((z): z is string => Boolean(z)))];
}

/** A moment in a given time zone, e.g. "13 нояб. 2026 г., 03:00". */
export function formatWhen(iso: string | null, zone?: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short", timeZone: zone });
  } catch {
    return new Date(iso).toLocaleString("ru-RU");
  }
}

/** "через 5 мин" / "через 2 ч" / "через 3 дн" / "сейчас" for a time in the future. */
export function relativeIn(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "";
  const secs = Math.round((new Date(iso).getTime() - now) / 1000);
  if (secs <= 30) return "сейчас";
  if (secs < 3600) return `через ${Math.max(1, Math.round(secs / 60))} мин`;
  if (secs < 86400) return `через ${Math.round(secs / 3600)} ч`;
  return `через ${Math.round(secs / 86400)} дн`;
}

/** The server refuses a real-runner schedule without a budget limit; the form can say so before sending. */
export function budgetRequired(runnerKind: string | null | undefined): boolean {
  return runnerKind !== undefined && runnerKind !== null && runnerKind !== "mock";
}
