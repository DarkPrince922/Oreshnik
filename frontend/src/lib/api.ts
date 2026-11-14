import axios, { AxiosError } from "axios";

const baseURL = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export const TOKEN_KEY = "oreshnik_token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable — session lasts until reload */
  }
}

export const api = axios.create({ baseURL, timeout: 20000 });

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}
/** For non-axios transports (the log WebSocket) that learn the session is invalid. */
export function notifyUnauthorized(): void {
  onUnauthorized?.();
}
export function apiWsUrl(path: string): string {
  return baseURL.replace(/^http/, "ws") + path;
}

api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err: AxiosError) => {
    const isLogin = err.config?.url?.includes("/auth/login");
    if (err.response?.status === 401 && !isLogin) onUnauthorized?.();
    return Promise.reject(err);
  }
);

/** Human-readable message from any error thrown by the API client. */
export function errorMessage(e: unknown): string {
  if (axios.isAxiosError(e)) {
    if (!e.response) return "Не удалось связаться с API";
    const detail = (e.response.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (typeof d?.msg === "string" ? d.msg.replace(/^Value error, /, "") : "ошибка"))
        .join("; ");
    }
    return `Ошибка ${e.response.status}`;
  }
  return e instanceof Error ? e.message : "Неизвестная ошибка";
}

// ── Types ────────────────────────────────────────────────────────────────

export type Role = "admin" | "operator" | "viewer";
export type Provider = "claude" | "openai" | "grok" | "custom";
export type RunnerKind = "mock" | "local" | "ssh";
export type RunnerStatus = "online" | "offline" | "provisioning" | "error";

export interface User {
  id: number;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface LLMProfile {
  id: number;
  name: string;
  provider: Provider;
  model: string;
  base_url: string | null;
  /** Every model the provider served when «Загрузить модели» last ran: what a scan can pick from (plus `model`, the default). */
  models: string[];
  api_key_hint: string;
  strix_model: string;
  created_at: string;
  updated_at: string;
}

export interface ProviderPreset {
  provider: Provider;
  label: string;
  hint: string;
  model_placeholder: string;
  suggestions: string[];
  base_url_required: boolean;
}

export interface Runner {
  id: number;
  name: string;
  kind: RunnerKind;
  status: RunnerStatus;
  status_detail: string | null;
  last_seen_at: string | null;
  created_at: string;
  ssh_host: string | null;
  ssh_port: number | null;
  ssh_user: string | null;
  has_ssh_key: boolean;
  has_ssh_password: boolean;
  /** The pinned host key (every connection is checked against it) and a key still waiting for confirmation. */
  ssh_host_key_fingerprint: string | null;
  ssh_host_key_pending_fingerprint: string | null;
  ssh_host_key_algorithm: string | null;
  has_provision_log: boolean;
  /** As set (null = the panel default) and what is applied; `max_targets` null = no limit. */
  max_scans_per_user: number | null;
  effective_max_scans_per_user: number;
  max_targets: number | null;
}

export interface Health {
  status: "ok" | "degraded";
  version: string;
  env: string;
  services: { database: boolean; redis: boolean };
}

export type ScanStatus = "queued" | "running" | "cancelling" | "succeeded" | "vulns_found" | "failed" | "cancelled";
export type ScanMode = "quick" | "standard" | "deep";
export type Severity = "critical" | "high" | "medium" | "low" | "info" | "unknown";

export interface ResumeEntry {
  at: string;
  instruction: string | null;
  max_budget: number | null;
}

export interface Scan {
  id: number;
  user_id: number;
  owner_email: string;
  runner_id: number | null;
  runner_name: string | null;
  runner_kind: RunnerKind | null;
  llm_profile_id: number | null;
  llm_profile_name: string | null;
  /** The model the scan runs with: the one chosen, else the profile's default. */
  llm_model: string | null;
  targets: string[];
  scan_mode: ScanMode;
  instruction: string | null;
  max_budget: number | null;
  status: ScanStatus;
  status_detail: string | null;
  exit_code: number | null;
  run_name: string | null;
  run_status: string | null;
  cost: number | null;
  total_tokens: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  severity_counts: Partial<Record<Severity, number>>;
  findings_total: number;
  schedule_id: number | null;
  schedule_name: string | null;
  parent_scan_id: number | null;
  /** What the parent had cost: `cost` is cumulative across a continuation. */
  cost_base: number | null;
  continuations: number[];
  /** How many times this scan was continued in place (same scan, same id). */
  resume_count: number;
  /** Seconds it was active before the current run; started_at restarts on every continuation. */
  elapsed_base: number;
  resume_history: ResumeEntry[];
}

export interface ScanList {
  items: Scan[];
  total: number;
  limit: number;
  offset: number;
}

export interface Finding {
  id: number;
  vuln_id: string;
  title: string;
  severity: Severity;
  target: string | null;
  endpoint: string | null;
  method: string | null;
  cve: string | null;
  cwe: string | null;
  cvss: number | null;
  found_at: string | null;
}

/** `data` is the untouched Strix record. Its text comes from the scanned target: treat as untrusted. */
export interface FindingDetail extends Finding {
  data: Record<string, unknown>;
}

// ── Calls ────────────────────────────────────────────────────────────────

export const getHealth = async () => (await api.get<Health>("/api/health")).data;

export const login = async (email: string, password: string) =>
  (await api.post<{ access_token: string; user: User }>("/api/auth/login", { email, password })).data;
export const getMe = async () => (await api.get<User>("/api/auth/me")).data;
export const changePassword = (current_password: string, new_password: string) =>
  api.post("/api/auth/change-password", { current_password, new_password });

export const listUsers = async () => (await api.get<User[]>("/api/users")).data;
export const createUser = async (body: { email: string; password: string; role: Role }) =>
  (await api.post<User>("/api/users", body)).data;
export const updateUser = async (id: number, body: Partial<{ role: Role; is_active: boolean; password: string }>) =>
  (await api.patch<User>(`/api/users/${id}`, body)).data;
export const deleteUser = (id: number) => api.delete(`/api/users/${id}`);

export const listPresets = async () => (await api.get<ProviderPreset[]>("/api/llm-profiles/presets")).data;
export const listProfiles = async () => (await api.get<LLMProfile[]>("/api/llm-profiles")).data;
/** "Загрузить модели": ask the provider what it serves. Either `api_key` (freshly typed,
 * not yet saved) or `profile_id` (reuse an existing profile's stored key) must be given. */
export const fetchModels = async (body: {
  provider: Provider;
  base_url: string | null;
  api_key?: string;
  profile_id?: number;
}) => (await api.post<{ models: string[]; saved: boolean }>("/api/llm-profiles/models", body)).data;
/** Ask the provider again, with the profile's stored key, and keep the whole list. */
export const refreshProfileModels = async (id: number) => (await api.post<LLMProfile>(`/api/llm-profiles/${id}/refresh-models`)).data;
export const createProfile = async (body: {
  name: string;
  provider: Provider;
  model: string;
  base_url: string | null;
  api_key: string;
  models?: string[];
}) => (await api.post<LLMProfile>("/api/llm-profiles", body)).data;
export const updateProfile = async (
  id: number,
  body: { name: string; provider: Provider; model: string; base_url: string | null; api_key?: string; models?: string[] }
) => (await api.patch<LLMProfile>(`/api/llm-profiles/${id}`, body)).data;
export const deleteProfile = (id: number) => api.delete(`/api/llm-profiles/${id}`);

export interface RunnerInput {
  name: string;
  kind?: RunnerKind;
  ssh_host?: string | null;
  ssh_port?: number | null;
  ssh_user?: string | null;
  ssh_private_key?: string | null;
  ssh_password?: string | null;
  /** Remove a stored key / password (at least one way to log in must remain). */
  clear_ssh_key?: boolean;
  clear_ssh_password?: boolean;
  /** Active scans one account may have on the runner (null = the panel default). */
  max_scans_per_user?: number | null;
  /** Distinct targets (domains / hosts) scanned on the runner at once, all accounts together (null = no limit). */
  max_targets?: number | null;
}
export const listRunners = async () => (await api.get<Runner[]>("/api/runners")).data;
export const createRunner = async (body: RunnerInput) => (await api.post<Runner>("/api/runners", body)).data;
export const updateRunner = async (id: number, body: RunnerInput) =>
  (await api.patch<Runner>(`/api/runners/${id}`, body)).data;
export const deleteRunner = (id: number) => api.delete(`/api/runners/${id}`);
export const checkRunner = async (id: number) =>
  (await api.post<Runner>(`/api/runners/${id}/check`, null, { timeout: 70000 })).data;
/** The fingerprint is sent back so a key that changed meanwhile is never pinned by a stale click. */
export const trustHostKey = async (id: number, fingerprint: string) =>
  (await api.post<Runner>(`/api/runners/${id}/trust-host-key`, { fingerprint }, { timeout: 70000 })).data;
export const forgetHostKey = async (id: number) => (await api.post<Runner>(`/api/runners/${id}/forget-host-key`)).data;
export const provisionRunner = async (id: number) => (await api.post<Runner>(`/api/runners/${id}/provision`, null, { timeout: 30000 })).data;
export interface ProvisionLog {
  running: boolean;
  log: string;
}
export const getProvisionLog = async (id: number) => (await api.get<ProvisionLog>(`/api/runners/${id}/provision-log`)).data;

// ── Scans ────────────────────────────────────────────────────────────────

export interface ScanCreateInput {
  targets: string[];
  scan_mode: ScanMode;
  instruction: string | null;
  max_budget: number | null;
  runner_id: number | null;
  llm_profile_id: number | null;
  /** One of the profile's models; null = the profile's own default. */
  llm_model?: string | null;
}
export const createScan = async (body: ScanCreateInput) => (await api.post<Scan>("/api/scans", body)).data;
export const listScans = async (params: { status?: ScanStatus; schedule_id?: number; limit?: number; offset?: number } = {}) =>
  (await api.get<ScanList>("/api/scans", { params })).data;
export const getScan = async (id: number) => (await api.get<Scan>(`/api/scans/${id}`)).data;
export const cancelScan = async (id: number) => (await api.post<Scan>(`/api/scans/${id}/cancel`)).data;
export const deleteScan = (id: number) => api.delete(`/api/scans/${id}`);
export const listFindings = async (id: number) => (await api.get<Finding[]>(`/api/scans/${id}/findings`)).data;
export const getFinding = async (scanId: number, findingId: number) =>
  (await api.get<FindingDetail>(`/api/scans/${scanId}/findings/${findingId}`)).data;
export const getReport = async (id: number) =>
  (await api.get<{ markdown: string | null }>(`/api/scans/${id}/report`)).data.markdown;

export type ExportFormat = "json" | "sarif" | "md";
const EXPORT_NAMES: Record<ExportFormat, string> = {
  json: "vulnerabilities.json",
  sarif: "findings.sarif",
  md: "penetration_test_report.md",
};
/** Authenticated download (a plain <a href> cannot send the bearer token). */
export async function downloadExport(id: number, fmt: ExportFormat): Promise<void> {
  const res = await api.get<Blob>(`/api/scans/${id}/export/${fmt}`, { responseType: "blob" });
  const url = URL.createObjectURL(res.data);
  const link = document.createElement("a");
  link.href = url;
  link.download = `oreshnik-scan-${id}-${EXPORT_NAMES[fmt]}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ── Agent activity ───────────────────────────────────────────────────────

export interface AgentInfo {
  id: string;
  name: string;
  parent_id: string | null;
  status: string; // running | waiting | budget_paused | completed | stopped | crashed | failed
  error: string | null;
}

/** One step of the agents' work. All text quotes the scanned target — untrusted, render as plain text. */
export interface ActivityEvent {
  seq: number;
  ts: string;
  agent: string;
  kind: "task" | "message" | "agent_message" | "system" | "tool_call" | "tool_output";
  role: string | null;
  text: string | null;
  tool: string | null;
  call_id: string | null;
  args: Record<string, unknown> | null;
  summary: string | null;
  truncated: boolean;
}

export interface ActivityPage {
  events: ActivityEvent[];
  next_cursor: number;
  agents: AgentInfo[];
  finished: boolean;
}

export const getActivity = async (id: number, cursor: number) =>
  (await api.get<ActivityPage>(`/api/scans/${id}/activity`, { params: { cursor } })).data;

export interface ContinueInput {
  instruction: string | null;
  max_budget: number | null;
  llm_profile_id: number | null;
  llm_model?: string | null;
  /** false (default): continue this very scan. true: create a new scan on a copy of the run. */
  as_new_scan?: boolean;
}
/** Continue a finished/stopped scan (`strix --resume`): in place by default, or as a NEW scan on a copy of its run. */
export const continueScan = async (id: number, body: ContinueInput) =>
  (await api.post<Scan>(`/api/scans/${id}/continue`, body)).data;

// ── Schedules ────────────────────────────────────────────────────────────

export interface Schedule {
  id: number;
  user_id: number;
  owner_email: string;
  name: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  targets: string[];
  scan_mode: ScanMode;
  instruction: string | null;
  max_budget: number | null;
  runner_id: number | null;
  runner_name: string | null;
  runner_kind: RunnerKind | null;
  llm_profile_id: number | null;
  llm_profile_name: string | null;
  llm_model: string | null;
  next_run_at: string | null;
  next_runs: string[];
  last_run_at: string | null;
  last_scan_id: number | null;
  last_result: "started" | "skipped" | "error" | null;
  last_detail: string | null;
  disabled_reason: string | null;
  created_at: string;
}

export interface ScheduleInput {
  name: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  targets: string[];
  scan_mode: ScanMode;
  instruction: string | null;
  max_budget: number | null;
  runner_id: number | null;
  llm_profile_id: number | null;
  llm_model?: string | null;
}

export interface SchedulePreview {
  valid: boolean;
  error: string | null;
  next_runs: string[];
  min_interval_minutes: number | null;
}

export const listSchedules = async () => (await api.get<Schedule[]>("/api/schedules")).data;
export const createSchedule = async (body: ScheduleInput) => (await api.post<Schedule>("/api/schedules", body)).data;
export const updateSchedule = async (id: number, body: Partial<ScheduleInput>) =>
  (await api.patch<Schedule>(`/api/schedules/${id}`, body)).data;
export const deleteSchedule = (id: number) => api.delete(`/api/schedules/${id}`);
export const runSchedule = async (id: number) => (await api.post<{ scan_id: number }>(`/api/schedules/${id}/run`)).data;
export const previewSchedule = async (cron: string, timezone: string, count = 5) =>
  (await api.get<SchedulePreview>("/api/schedules/preview", { params: { cron, timezone, count } })).data;

// ── Costs ────────────────────────────────────────────────────────────────

export interface CostSummary {
  days: number;
  periods: { today: number; week: number; month: number; window: number; all_time: number };
  running: { count: number; cost: number };
  tokens: number;
  per_day: { date: string; cost: number; scans: number }[];
  per_user: { user_id: number; email: string; cost: number; scans: number }[];
  top_scans: { id: number; target: string; owner_email: string; status: string; cost: number }[];
}
export const getCosts = async (days = 30) => (await api.get<CostSummary>("/api/costs", { params: { days } })).data;

// ── Операции ─────────────────────────────────────────────────────────────
//
// A goal in plain words, turned into a shell script by an LLM and run as root over SSH on existing SSH runners —
// admin-only, no approval step before it runs (see docs/ARCHITECTURE.md for why). What *is* always kept: the exact
// script, and each host's own output and exit code.

export type OpsRunStatus = "drafting" | "running" | "done" | "failed";
export type OpsHostStatus = "queued" | "running" | "succeeded" | "failed" | "blocked" | "unreachable" | "cancelled";

export interface OpsRunHost {
  id: number;
  runner_id: number | null;
  runner_name: string;
  status: OpsHostStatus;
  exit_code: number | null;
  output: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface OpsRun {
  id: number;
  user_id: number;
  owner_email: string;
  name: string;
  goal: string;
  llm_profile_id: number | null;
  llm_profile_name: string | null;
  llm_model: string | null;
  status: OpsRunStatus;
  script: string | null;
  script_error: string | null;
  blocked_reason: string | null;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  hosts: OpsRunHost[];
}

export const listOpsTargetRunners = async () => (await api.get<{ id: number; name: string }[]>("/api/ops/runners")).data;
export const listOpsRuns = async () => (await api.get<OpsRun[]>("/api/ops")).data;
export const getOpsRun = async (id: number) => (await api.get<OpsRun>(`/api/ops/${id}`)).data;
export const createOpsRun = async (body: { name: string; goal: string; llm_profile_id: number; llm_model?: string | null; runner_ids: number[] }) =>
  (await api.post<OpsRun>("/api/ops", body)).data;
export const cancelOpsRun = async (id: number) => (await api.post<OpsRun>(`/api/ops/${id}/cancel`)).data;

// ── Агент ────────────────────────────────────────────────────────────────
//
// A chat with Codex CLI that runs in the isolated agent container (see backend/app/api/agent.py). Each message is one "turn"; what the agent
// does comes back as an ordered history of events plus snapshots of what it is writing right now, live over a WebSocket.

export type AgentSessionStatus = "idle" | "running";
export type ApprovalMode = "auto" | "ask";
export type WebSearchMode = "auto" | "on" | "off";
/** A goal makes the agent work by itself, turn after turn, until it is reached or its token budget is spent. */
export interface AgentGoal {
  objective: string;
  status: "active" | "paused" | "blocked" | "usageLimited" | "budgetLimited" | "complete" | string;
  token_budget: number | null;
  tokens_used: number;
  seconds_used: number;
}
export const REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"] as const;

export interface AgentSession {
  id: number;
  title: string;
  llm_profile_id: number | null;
  llm_profile_name: string | null;
  /** The model the session talks to: the one chosen, else the profile's default. */
  llm_model: string | null;
  instructions: string | null;
  reasoning_effort: string | null;
  approval_mode: ApprovalMode;
  web_search: WebSearchMode;
  goal: AgentGoal | null;
  /** Tokens in the model's context after the last turn, and the window Codex assumes for the model (a guess when `context_window_known` is false). */
  context_used: number | null;
  context_window: number | null;
  context_window_known: boolean;
  /** Spent by the session in total. */
  tokens_in: number;
  tokens_out: number;
  status: AgentSessionStatus;
  turn_count: number;
  last_error: string | null;
  cancel_requested: boolean;
  event_count: number;
  created_at: string;
  updated_at: string;
}

/** One stored event: `kind` is "user", "action", "turn.started", "item.completed", "turn.diff", "turn.plan", "approval.requested/resolved",
 * "turn.completed", "thread.reverted", "turn.error"/"turn.stopped" (the panel's own notes). */
export interface AgentEvent {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
}

export interface AgentSessionInput {
  title: string;
  llm_profile_id: number;
  llm_model?: string | null;
  instructions?: string | null;
  reasoning_effort?: string | null;
  approval_mode?: ApprovalMode;
}
export interface AgentSettingsInput {
  title?: string;
  instructions?: string | null;
  reasoning_effort?: string | null;
  approval_mode?: ApprovalMode;
  web_search?: WebSearchMode;
}
export type ReviewTarget = { type: "uncommittedChanges" } | { type: "baseBranch"; branch: string } | { type: "commit"; sha: string } | { type: "custom"; instructions: string };
export type AgentActionInput =
  | { action: "compact" }
  | { action: "revert" }
  | { action: "goal"; objective?: string; token_budget?: number }
  | { action: "goal_clear" }
  | { action: "shell"; command: string }
  | { action: "review"; target?: ReviewTarget };
export type ApprovalDecision = "accept" | "accept_session" | "decline" | "cancel" | "answer";

export interface AgentFileEntry {
  name: string;
  dir: boolean;
  size: number | null;
  modified: number;
}
export interface AgentFileListing {
  path: string;
  entries: AgentFileEntry[];
}
export interface AgentFilePreview {
  binary: boolean;
  size: number;
  text: string | null;
  truncated: boolean;
}

const A = "/api/agent/sessions";
export const getAgentHealth = async () => (await api.get<{ ok: boolean; detail: string }>("/api/agent/health")).data;
export const listAgentSessions = async () => (await api.get<AgentSession[]>(A)).data;
export const createAgentSession = async (body: AgentSessionInput) => (await api.post<AgentSession>(A, body)).data;
export const getAgentSession = async (id: number) => (await api.get<AgentSession>(`${A}/${id}`)).data;
export const updateAgentSession = async (id: number, body: AgentSettingsInput) => (await api.patch<AgentSession>(`${A}/${id}`, body)).data;
export const deleteAgentSession = async (id: number) => {
  await api.delete(`${A}/${id}`);
};
export const sendAgentMessage = async (id: number, text: string) => (await api.post<AgentSession>(`${A}/${id}/messages`, { text })).data;
/** Add an instruction to the turn that is running now. */
export const steerAgentTurn = async (id: number, text: string) => (await api.post<AgentSession>(`${A}/${id}/steer`, { text })).data;
export const stopAgentTurn = async (id: number) => (await api.post<AgentSession>(`${A}/${id}/stop`)).data;
export const runAgentAction = async (id: number, body: AgentActionInput) => (await api.post<AgentSession>(`${A}/${id}/actions`, body)).data;
export const forkAgentSession = async (id: number) => (await api.post<AgentSession>(`${A}/${id}/fork`, undefined, { timeout: 300_000 })).data;
export const answerAgentApproval = async (id: number, requestId: string, decision: ApprovalDecision, answers?: Record<string, string | string[]>) => {
  await api.post(`${A}/${id}/approvals/${encodeURIComponent(requestId)}`, { decision, answers: answers ?? null });
};

export const listAgentFiles = async (id: number, path = "") => (await api.get<AgentFileListing>(`${A}/${id}/files`, { params: { path } })).data;
export const readAgentFile = async (id: number, path: string) => (await api.get<AgentFilePreview>(`${A}/${id}/files/content`, { params: { path } })).data;
export const uploadAgentFile = async (id: number, path: string, file: File) =>
  (await api.put<{ path: string; size: number }>(`${A}/${id}/files`, file, { params: { path }, headers: { "Content-Type": "application/octet-stream" }, timeout: 300_000 })).data;
export const cloneIntoAgent = async (id: number, url: string) => (await api.post<{ path: string }>(`${A}/${id}/files/clone`, { url }, { timeout: 330_000 })).data;

/** Save a file the panel serves behind the login: fetched with the token, then handed to the browser as a download. */
async function saveBlob(url: string, params: Record<string, string>, filename: string): Promise<void> {
  const response = await api.get<Blob>(url, { params, responseType: "blob", timeout: 600_000 });
  const href = URL.createObjectURL(response.data);
  const link = document.createElement("a");
  link.href = href;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10_000);
}
export const downloadAgentFile = (id: number, path: string) => saveBlob(`${A}/${id}/files/download`, { path }, path.split("/").pop() || "file");
export const downloadAgentArchive = (id: number, title: string) => saveBlob(`${A}/${id}/files/archive`, {}, `${title.replace(/[^\p{L}\p{N}._-]+/gu, "_") || "session"}.zip`);

export interface AgentFromScan {
  session: AgentSession;
  /** The task proposed for the first message: the user reads it and sends it. */
  draft: string;
  files: number;
}
export const createAgentFromScan = async (scanId: number, body: { llm_profile_id: number; llm_model?: string | null }) =>
  (await api.post<AgentFromScan>(`/api/agent/from-scan/${scanId}`, body, { timeout: 120_000 })).data;

// ── Настройки ────────────────────────────────────────────────────────────
//
// The operating parameters that used to live only in `.env`. `value` applies right now; `default` is what `.env` / the built-in
// default says (what "сбросить" returns to); `restart` is not empty for a parameter that needs a restart of that part to apply.

export type SettingKind = "int" | "float" | "bool" | "choice" | "text";
export type SettingValue = number | boolean | string;

export interface AppSettingItem {
  key: string;
  env: string;
  label: string;
  hint: string;
  kind: SettingKind;
  minimum: number | null;
  maximum: number | null;
  choices: string[];
  unit: string;
  restart: string;
  value: SettingValue;
  default: SettingValue;
  changed: boolean;
  updated_at: string | null;
}
export interface AppSettingsGroup {
  name: string;
  items: AppSettingItem[];
}
export const getAppSettings = async () => (await api.get<{ groups: AppSettingsGroup[] }>("/api/settings")).data.groups;
/** key -> new value; `null` resets that parameter to its default. All or nothing. */
export const updateAppSettings = async (values: Record<string, SettingValue | null>) =>
  (await api.put<{ groups: AppSettingsGroup[] }>("/api/settings", { values })).data.groups;

// ── Огоньки в меню ───────────────────────────────────────────────────────
//
// What is new since the user last opened «Сканы» / «Агент» (see backend/app/api/attention.py).

export interface Attention {
  /** The server's clock: stored as "seen" when a section is opened. */
  now: string;
  scans: { finished: number; failed: number; findings: number };
  agent: { finished: number; waiting: number };
}
export const getAttention = async (params: { scans_since?: string; agent_since?: string }) => (await api.get<Attention>("/api/attention", { params })).data;
