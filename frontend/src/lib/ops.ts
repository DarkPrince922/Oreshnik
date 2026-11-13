/** Pure logic behind the «Операции» page, kept out of the component so it can be unit-tested like the rest of
 * src/lib (the component itself is covered by hand-driven e2e against the real stack). */

import type { OpsHostStatus, OpsRun, OpsRunStatus } from "./api";

export const MAX_RUNNERS_PER_TASK = 50;

export function canSubmit(args: { goal: string; llmProfileId: number | null; runnerIds: number[] }): boolean {
  return args.goal.trim().length > 0 && args.llmProfileId !== null && args.runnerIds.length > 0 && args.runnerIds.length <= MAX_RUNNERS_PER_TASK;
}

/** Cancel only makes sense while the task could still touch another host. */
export function canCancel(run: Pick<OpsRun, "status" | "cancel_requested">): boolean {
  return (run.status === "drafting" || run.status === "running") && !run.cancel_requested;
}

const RUN_STATUS_LABEL: Record<OpsRunStatus, string> = {
  drafting: "Пишет скрипт…",
  running: "Выполняется…",
  done: "Завершено",
  failed: "Не выполнено",
};
export const runStatusLabel = (status: OpsRunStatus): string => RUN_STATUS_LABEL[status];

const RUN_STATUS_TONE: Record<OpsRunStatus, "ok" | "warn" | "bad" | "muted" | "accent"> = {
  drafting: "accent",
  running: "accent",
  done: "ok",
  failed: "bad",
};
export const runStatusTone = (status: OpsRunStatus) => RUN_STATUS_TONE[status];

const HOST_STATUS_LABEL: Record<OpsHostStatus, string> = {
  queued: "в очереди",
  running: "выполняется",
  succeeded: "успешно",
  failed: "ошибка",
  blocked: "заблокировано",
  unreachable: "недоступен",
  cancelled: "отменено",
};
export const hostStatusLabel = (status: OpsHostStatus): string => HOST_STATUS_LABEL[status];

const HOST_STATUS_TONE: Record<OpsHostStatus, "ok" | "warn" | "bad" | "muted" | "accent"> = {
  queued: "muted",
  running: "accent",
  succeeded: "ok",
  failed: "bad",
  blocked: "bad",
  unreachable: "warn",
  cancelled: "muted",
};
export const hostStatusTone = (status: OpsHostStatus) => HOST_STATUS_TONE[status];

/** One line summarising how the fleet went — for the run list, where there is no room for a per-host table. */
export function fleetSummary(hosts: OpsRun["hosts"]): string {
  if (hosts.length === 0) return "нет хостов";
  const counts = new Map<OpsHostStatus, number>();
  for (const h of hosts) counts.set(h.status, (counts.get(h.status) ?? 0) + 1);
  return [...counts.entries()].map(([status, n]) => `${hostStatusLabel(status)}: ${n}`).join(", ");
}
