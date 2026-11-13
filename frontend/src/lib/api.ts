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
}

export interface Health {
  status: "ok" | "degraded";
  version: string;
  env: string;
  services: { database: boolean; redis: boolean };
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
  (await api.post<Runner>(`/api/runners/${id}/check`, null, { timeout: 30000 })).data;
