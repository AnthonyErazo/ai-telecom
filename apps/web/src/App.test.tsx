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
  it("no muestra el asistente hasta obtener un token LOA2", async () => {
    renderApp();

    expect(screen.getByRole("heading", { name: "Ingrese a su cuenta" })).toBeInTheDocument();
    expect(screen.queryByText("Cuenta autenticada")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Ingresar" }));

    expect(await screen.findByText("Cuenta autenticada")).toBeInTheDocument();
    expect(api.token).toHaveBeenCalledWith("C-DEMO-01");
    expect(api.facts).toHaveBeenCalledWith("jwt-loa2");
  });

  it("cierra la sesión y vuelve a pedir acceso", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Ingresar" }));
    await screen.findByText("Cuenta autenticada");

    fireEvent.click(await screen.findByRole("button", { name: "Cerrar sesión" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Ingrese a su cuenta" })).toBeInTheDocument()
    );
    expect(screen.queryByText("Cuenta autenticada")).not.toBeInTheDocument();
  });
});
