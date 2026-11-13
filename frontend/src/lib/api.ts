import axios from "axios";

const baseURL = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export const api = axios.create({ baseURL, timeout: 8000 });

export interface Health {
  status: "ok" | "degraded";
  version: string;
  env: string;
  services: { database: boolean; redis: boolean };
}

export async function getHealth(): Promise<Health> {
  const { data } = await api.get<Health>("/api/health");
  return data;
}
