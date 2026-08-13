import type {
  DemoAccounts,
  ElementoCola,
  EstadoSala,
  Explanation,
  FactSet,
  LiveToken,
  PaqueteAsesor,
} from "./types";

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
  // WhatsApp entra con LOA1 a propósito: la identidad se apoya en un número de teléfono,
  // no en una sesión autenticada. Es lo que hace que la respuesta salga SIN importes
  // (§6.4 del README), y por eso el canal se declara aquí y no en la pantalla.
  tokenWhatsapp: (accountId: string) => request<{ access_token: string }>("/dev/token", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cuenta_id: accountId, nivel: "LOA1", canal: "WHATSAPP" })
  }),
  // El asesor declara SIEMPRE a nombre de quién actúa: `acting_on_behalf_of` es
  // obligatorio en LOA_ASESOR y queda en cada evento de la bitácora.
  tokenAsesor: (asesorId: string, cuentaAtendida: string) =>
    request<{ access_token: string }>("/dev/token", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cuenta_id: asesorId, nivel: "LOA_ASESOR",
        acting_on_behalf_of: cuentaAtendida, canal: "ASESOR"
      })
    }),
  facts: (token: string) => request<FactSet>("/v1/hechos", authorized(token)),
  explain: (token: string, input: { conversation_id?: string; cuenta_id: string; periodo?: string; verbosidad: string; utterance: string }) =>
    request<Explanation>("/v1/explicar", authorized(token, { method: "POST", body: JSON.stringify({ ...input, canal: "APP" }) })),
  // El mismo endpoint que usa la App: el canal viaja en el cuerpo y la respuesta es la
  // misma `RespuestaCanalAgnostica`. Lo que cambia es el NIVEL del token, y con él lo que
  // el servidor decide entregar. La pantalla no redacta nada por su cuenta.
  explicarWhatsapp: (token: string, input: { conversation_id?: string; cuenta_id: string; utterance: string }) =>
    request<Explanation>("/v1/explicar", authorized(token, {
      method: "POST",
      body: JSON.stringify({ ...input, canal: "WHATSAPP", verbosidad: "CORTO" })
    })),
  liveToken: (token: string) => request<LiveToken>("/v1/live/token", authorized(token, { method: "POST" })),

  // --- Consola del asesor ------------------------------------------------- //
  cola: (token: string) => request<ElementoCola[]>("/v1/asesor/cola", authorized(token)),
  paquete: (token: string, contextRef: string) =>
    request<PaqueteAsesor>(`/v1/asesor/paquete/${encodeURIComponent(contextRef)}`, authorized(token)),
  sala: (token: string, conversationId: string) =>
    request<EstadoSala>(`/v1/asesor/conversacion/${encodeURIComponent(conversationId)}`, authorized(token)),
  unirse: (token: string, conversationId: string) =>
    request<EstadoSala>(`/v1/asesor/conversacion/${encodeURIComponent(conversationId)}/unirse`,
      authorized(token, { method: "POST" })),
  salir: (token: string, conversationId: string) =>
    request<EstadoSala>(`/v1/asesor/conversacion/${encodeURIComponent(conversationId)}/salir`,
      authorized(token, { method: "POST" })),
  mensajeAsesor: (token: string, conversationId: string, texto: string) =>
    request<EstadoSala>(`/v1/asesor/conversacion/${encodeURIComponent(conversationId)}/mensaje`,
      authorized(token, { method: "POST", body: JSON.stringify({ texto }) })),
  audit: (token: string, traceId: string) => request<Record<string, unknown>>(`/v1/auditoria?trace_id=${encodeURIComponent(traceId)}&incluir_eventos=true`, authorized(token)),
  hallucinate: (token: string, accountId: string) => request<Record<string, unknown>>("/dev/alucinar", authorized(token, {
    method: "POST", body: JSON.stringify({ activar: true, delta_cent: 731, turnos: 1, cuenta_id: accountId })
  }))
};
