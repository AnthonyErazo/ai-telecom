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

  it("bloquea el asistente hasta obtener un token LOA2", async () => {
    renderApp();

    expect(screen.getByRole("button", { name: "Asistente" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Ingresar" }));

    expect(await screen.findByText("Cuenta autenticada")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Asistente" })).toBeEnabled();
    expect(api.token).toHaveBeenCalledWith("C-DEMO-01");
    expect(api.facts).toHaveBeenCalledWith("jwt-loa2");
  });

  it("cierra la sesión y vuelve a bloquear el asistente", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Ingresar" }));
    await screen.findByText("Cuenta autenticada");

    fireEvent.click(screen.getByRole("button", { name: "Login" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cerrar sesión" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Asistente" })).toBeDisabled());
    expect(screen.getByRole("heading", { name: "Ingrese a su cuenta" })).toBeInTheDocument();
  });
});
