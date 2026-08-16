import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const api = vi.hoisted(() => ({
  health: vi.fn(),
  accounts: vi.fn(),
  token: vi.fn(),
  facts: vi.fn(),
  explain: vi.fn(),
  liveToken: vi.fn(),
  audit: vi.fn(),
  hallucinate: vi.fn(),
}));

vi.mock("./api/client", () => ({
  api,
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) { super(message); }
  },
}));

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
}

describe("login de cuenta", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    api.health.mockResolvedValue({ estado: "ok" });
    api.accounts.mockResolvedValue({ demo: ["C-DEMO-01"], guion: {} });
    api.token.mockResolvedValue({ access_token: "jwt-loa2" });
    api.facts.mockResolvedValue({
      periodo_actual: "2026-07",
      total_previo_cent: 8000,
      total_actual_cent: 8290,
      delta_total_cent: 290,
      modalidad_renta: "ADELANTADA",
      sha256: "prueba",
    });
  });

  // La navegación pasó a tener una pestaña por CANAL —«Mi Movistar», «WhatsApp»,
  // «Asesor 104»— y el login dejó de ser un destino propio: es un paso dentro de Mi
  // Movistar. Lo que estas pruebas custodian no ha cambiado: sin token no se ve el
  // asistente, y cerrar sesión devuelve a la pantalla de acceso.
  // El acceso de Mi Movistar cambió al integrar `BenFran13`: ya no es un campo con la
  // cuenta precargada, sino un flujo de varias pantallas (splash → selector → documento
  // con teclado numérico). Estas pruebas dejan de conducir ESA pantalla y comprueban la
  // garantía, que es la que no puede romperse y no depende del diseño: sin token LOA2 no
  // se emite ninguno ni se piden hechos, y al cerrar sesión se vuelve al principio.
  it("no pide hechos ni emite token hasta que el cliente se identifica", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));

    // `findBy`, no `getBy`: cambiar de pestaña pasa por `stopLive()`, que es async.
    expect(await screen.findByRole("heading", { name: "App Mi Movistar" })).toBeInTheDocument();
    expect(api.token).not.toHaveBeenCalled();
    expect(api.facts).not.toHaveBeenCalled();
  });

  it("avanzar en el acceso no emite token por el camino", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });

    // El botón «Cerrar sesión» de la barra ni siquiera existe todavía, porque no hay
    // sesión: esa es justamente la garantía. Y avanzar por el flujo de acceso no emite
    // token ni pide hechos hasta que el cliente termina de identificarse.
    fireEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));

    await waitFor(() => expect(api.token).not.toHaveBeenCalled());
    expect(api.facts).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Cerrar sesión" })).toBeNull();
  });

  it("no abre BillSense con un prompt automático ni con un saludo preescrito", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });

    expect(screen.queryByText("¿En qué te puedo ayudar hoy?")).toBeNull();
    expect(screen.queryByText("Tu conversación está lista.")).toBeNull();
    expect(screen.queryByText("¡Hola! Soy BillSense 👋")).toBeNull();
  });
});
