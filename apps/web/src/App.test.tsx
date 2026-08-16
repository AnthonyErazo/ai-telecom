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
  // Mi Movistar usa un único acceso por cuenta cliente. La cuenta sugerida viene del
  // backend de demo y solo se intercambia por un token LOA2 al pulsar Continuar.
  it("no pide hechos ni emite token hasta que el cliente se identifica", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));

    // `findBy`, no `getBy`: cambiar de pestaña pasa por `stopLive()`, que es async.
    expect(await screen.findByRole("heading", { name: "App Mi Movistar" })).toBeInTheDocument();
    expect(api.token).not.toHaveBeenCalled();
    expect(api.facts).not.toHaveBeenCalled();
  });

  it("muestra directamente el acceso unificado por cuenta cliente", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });

    fireEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));

    expect(await screen.findByRole("heading", { name: "Ingresa a tu cuenta" })).toBeInTheDocument();
    expect(screen.getByLabelText("Cuenta cliente")).toHaveTextContent("C-DEMO-01");
    expect(screen.queryByText("Todos mis productos")).toBeNull();
    expect(screen.queryByText("Solo con mi móvil")).toBeNull();
    await waitFor(() => expect(api.token).not.toHaveBeenCalled());
    expect(api.facts).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Cerrar sesión" })).toBeNull();
  });

  it("valida la cuenta cliente y carga sus hechos facturados", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });
    fireEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));
    await screen.findByRole("heading", { name: "Ingresa a tu cuenta" });

    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));

    await waitFor(() => expect(api.token).toHaveBeenCalledWith("C-DEMO-01"));
    expect(api.facts).toHaveBeenCalledWith("jwt-loa2");
    expect((await screen.findAllByText("ADELANTADA")).length).toBeGreaterThan(0);
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
