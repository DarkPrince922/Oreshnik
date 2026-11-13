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
  /** The pinned host key (every connection is checked against it) and a key still waiting for confirmation. */
  ssh_host_key_fingerprint: string | null;
  ssh_host_key_pending_fingerprint: string | null;
  ssh_host_key_algorithm: string | null;
  has_provision_log: boolean;
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
export const createProfile = async (body: {
  name: string;
  provider: Provider;
  model: string;
  base_url: string | null;
  api_key: string;
}) => (await api.post<LLMProfile>("/api/llm-profiles", body)).data;
export const updateProfile = async (
  id: number,
  body: { name: string; provider: Provider; model: string; base_url: string | null; api_key?: string }
) => (await api.patch<LLMProfile>(`/api/llm-profiles/${id}`, body)).data;
export const deleteProfile = (id: number) => api.delete(`/api/llm-profiles/${id}`);

export interface RunnerInput {
  name: string;
  kind?: RunnerKind;
  ssh_host?: string | null;
  ssh_port?: number | null;
  ssh_user?: string | null;
  ssh_private_key?: string | null;
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
