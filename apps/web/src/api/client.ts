import type { DemoAccounts, Explanation, FactSet } from "./types";

const configuredBase = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "");
const API_BASE = configuredBase ?? "";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public data?: unknown) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, body?.codigo ?? `HTTP_${response.status}`, body?.detalle ?? "Error de API", body?.datos);
  }
  return body as T;
}

const authorized = (token: string, init: RequestInit = {}): RequestInit => ({
  ...init,
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...init.headers }
});

export const api = {
  health: () => request<Record<string, unknown>>("/salud"),
  accounts: () => request<DemoAccounts>("/dev/cuentas"),
  token: (accountId: string) => request<{ access_token: string }>("/dev/token", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cuenta_id: accountId, nivel: "LOA2", canal: "APP" })
  }),
  facts: (token: string) => request<FactSet>("/v1/hechos", authorized(token)),
  explain: (token: string, input: { conversation_id?: string; cuenta_id: string; verbosidad: string; utterance: string }) =>
    request<Explanation>("/v1/explicar", authorized(token, { method: "POST", body: JSON.stringify({ ...input, canal: "APP" }) })),
  audit: (token: string, traceId: string) => request<Record<string, unknown>>(`/v1/auditoria?trace_id=${encodeURIComponent(traceId)}&incluir_eventos=true`, authorized(token)),
  hallucinate: (token: string, accountId: string) => request<Record<string, unknown>>("/dev/alucinar", authorized(token, {
    method: "POST", body: JSON.stringify({ activar: true, delta_cent: 731, turnos: 1, cuenta_id: accountId })
  }))
};
