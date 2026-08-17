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
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

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
      fecha_vencimiento: "2020-01-01",
      deuda_anterior_cent: 0,
      total_a_pagar_cent: 8290,
      lineas: [
        {
          concepto_id: "PLAN-BASE",
          nombre_comercial: "Plan Movistar mensual",
          clase: "CARGO",
          monto_actual_cent: 8990,
          monto_previo_cent: 8000,
          delta_cent: 990,
          familia: "RECURRENTE",
        },
        {
          concepto_id: "NC-FACTURACION",
          nombre_comercial: "Nota de crédito por facturación",
          clase: "ABONO",
          monto_actual_cent: -900,
          monto_previo_cent: 0,
          delta_cent: -900,
          causa: "NOTA_CREDITO",
          familia: "CREDITO",
        },
        {
          concepto_id: "NOTA_DEBITO",
          nombre_comercial: "Nota de débito por regularización",
          clase: "CARGO",
          monto_actual_cent: 200,
          monto_previo_cent: 0,
          delta_cent: 200,
          causa: "NOTA_DEBITO",
          familia: "AJUSTE",
        },
      ],
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

  it("expone el dashboard de asesor como canal independiente", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Asesor 104" }));

    expect(await screen.findByRole("heading", { name: "Dashboard del asesor" })).toBeInTheDocument();
  });

  it("muestra directamente el acceso unificado por cuenta cliente", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });

    fireEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));

    expect(await screen.findByRole("heading", { name: "Ingresa a tu cuenta" })).toBeInTheDocument();
    expect(screen.getByLabelText("Cuenta cliente")).toHaveValue("");
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
    const miRecibo = screen.getByText("Mi recibo");
    const reciboVencido = screen.getByText("Recibo vencido");
    expect(miRecibo.compareDocumentPosition(reciboVencido) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("no abre BillSense con un prompt automático ni con un saludo preescrito", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });

    expect(screen.queryByText("¿En qué te puedo ayudar hoy?")).toBeNull();
    expect(screen.queryByText("Tu conversación está lista.")).toBeNull();
    expect(screen.queryByText("¡Hola! Soy BillSense 👋")).toBeNull();
  });

  it("incluye en el PDF todas las secciones y cargos mostrados en el recibo", async () => {
    const write = vi.fn();
    const close = vi.fn();
    vi.spyOn(window, "open").mockReturnValue({ document: { write, close } } as unknown as Window);

    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Mi Movistar" }));
    await screen.findByRole("heading", { name: "App Mi Movistar" });
    fireEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));
    await screen.findByRole("heading", { name: "Ingresa a tu cuenta" });
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    await screen.findByText("Mi recibo");
    fireEvent.click(screen.getByRole("button", { name: "Ver recibo" }));
    await screen.findByText("Detalle de Facturación");
    expect(screen.getByText("Notas de crédito")).toBeInTheDocument();
    expect(screen.getByText("Notas de débito")).toBeInTheDocument();
    expect(screen.queryByText("Notas de crédito/débito")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Descargar PDF detallado" }));

    const pdf = String(write.mock.calls[0]?.[0] ?? "");
    expect(pdf).toContain("Detalle de cargos facturados");
    expect(pdf).toContain("Plan Movistar mensual");
    expect(pdf).toContain("Nota de crédito por facturación");
    expect(pdf).toContain("Nota de débito por regularización");
    expect(pdf).toContain("Resumen de mi cuenta");
    expect(pdf).toContain("Comparativa con el mes anterior");
    expect(pdf).toContain("Vencido");
    expect(close).toHaveBeenCalled();
  });
});
