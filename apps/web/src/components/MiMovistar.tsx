/**
 * MiMovistar — App Mi Movistar completa
 *
 * Flujo de 7 pantallas:
 *   splash → loginCuenta/registroCuenta → dashboard → recibo → chat
 *
 * La pantalla "chat" tiene DOS modos seleccionables:
 *   ① Chat  — texto puro, sin audio. Incluye ReceiptDetailCard dentro
 *              del flujo de mensajes.
 *   ② BillSense Voz — interfaz dedicada al audio. Botón grande de
 *              micrófono, transcriptos de la IA como burbujas de texto.
 *              No hay campo de escritura.
 *
 * BillSense = marca de la IA (antes "Gemini Live"). El motor interno no
 * cambia; solo la presentación al usuario.
 */
import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertCircle, ArrowLeft, Bot, Clock, CreditCard, Gift, Home as HomeIcon,
  History, Layers, Loader2, MessageSquare, Mic, MicOff, Plus, Send, ShieldCheck,
  ShoppingBag, Smartphone, Star, ThumbsDown, ThumbsUp, TrendingDown, TrendingUp,
  User as UserIcon, Wallet, Wifi,
} from "lucide-react";
import MovistarLogo from "./MovistarLogo";
import { PagoModal } from "./PagoModal";
import { ReceiptDetailCard } from "./ReceiptDetailCard";
import { RentaExplicativa } from "./RentaExplicativa";
import { RichMessage } from "./RichMessage";
import { api, ApiError } from "../api/client";
import type { Block, Explanation, FactSet } from "../api/types";
import { agruparLineas } from "../lib/recibo";
import { GeminiLiveClient, type LiveStatus } from "../live/client";
import { microphoneSupportError } from "../live/audio";

// ── Helpers ───────────────────────────────────────────────────────────
// La app se sirve bajo `base: "/ui/"` (vite.config.ts): una ruta absoluta como
// "/billsense-logo.png" apunta a la raíz del dominio, no a donde vite publica
// `public/`, y el navegador la resuelve como imagen rota. `BASE_URL` la coloca
// donde de verdad vive el archivo, en dev y en build.
const billsenseLogo = `${import.meta.env.BASE_URL}billsense-logo.png`;

const soles = (c: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(c / 100);

const liveLabel = (s: LiveStatus): string =>
  ({
    idle:       "Toca para hablar con BillSense",
    connecting: "Conectando con BillSense…",
    listening:  "BillSense te escucha…",
    consulting: "Consultando tu recibo…",
    speaking:   "BillSense está respondiendo…",
    error:      "Error de conexión de voz",
  })[s];

// ── Types ─────────────────────────────────────────────────────────────
type Pantalla = "splash"|"loginCuenta"|"registroCuenta"|"dashboard"|"recibo"|"mejorarPlan"|"comoFunciona"|"historial"|"chat";
type ChatModo = "chat" | "voz";
/** Un mensaje del historial del chat. Los del asistente guardan los `bloques`
 * estructurados que ya trajo el backend (no un texto aplanado) para pintarlos con
 * formato rico; esRecibo muestra la tarjeta de factura inline en su lugar.
 * `beneficiosCierre` solo se llena cuando el turno fue una despedida y la cuenta
 * tiene beneficios vigentes: dispara la tarjeta resaltada del "efecto efervescente". */
type Mensaje  = {
  rol: "cliente"|"asistente"|"asesor"; texto?: string; bloques?: Block[]; esVoz?: boolean; esRecibo?: boolean;
  beneficiosCierre?: string[];
};

// ── Numpad grid ───────────────────────────────────────────────────────
const PAD: string[][] = [
  ["1","2","3"],
  ["4","5","6"],
  ["7","8","9"],
  ["","0","⌫"],
];

// ═════════════════════════════════════════════════════════════════════
export function MiMovistar({
  onSesion,
  onExplicacion,
}: {
  onSesion: (cuenta: string, token: string) => void;
  onExplicacion: (e: Explanation) => void;
}) {
  // ── Pantalla / nav ──────────────────────────────────────────────
  const [pantalla,      setPantalla]      = useState<Pantalla>("splash");
  const [aceptaTC,      setAceptaTC]      = useState(false);
  const [bottomTab,     setBottomTab]     = useState("inicio");
  const [chatModo,      setChatModo]      = useState<ChatModo>("chat");
  const [mostrarPago,   setMostrarPago]   = useState(false);

  // ── Historial ───────────────────────────────────────────────────
  // `periodoActivo` es el periodo sobre el que BillSense está contestando en el
  // chat: `null` significa "el periodo actual", tal como ya funcionaba. Se activa
  // solo al entrar a un recibo del historial y se limpia al volver.
  const [periodoActivo,  setPeriodoActivo]  = useState<string | null>(null);
  const [hechosPeriodo,  setHechosPeriodo]  = useState<FactSet | null>(null);
  const [cargandoPeriodo,setCargandoPeriodo]= useState(false);
  const [errorPeriodo,   setErrorPeriodo]   = useState("");

  // ── Auth ────────────────────────────────────────────────────────
  const [cuentaEntrada, setCuentaEntrada] = useState("");
  const [cuenta,    setCuenta]    = useState("");
  const [token,     setToken]     = useState("");
  const [hechos,    setHechos]    = useState<FactSet | null>(null);

  // ── Chat ────────────────────────────────────────────────────────
  const [mensajes,     setMensajes]     = useState<Mensaje[]>([]);
  const [borrador,     setBorrador]     = useState("");
  const [conversacion, setConversacion] = useState<string | undefined>();
  const [ultima,       setUltima]       = useState<Explanation | null>(null);
  const [opinion,      setOpinion]      = useState<"arriba"|"abajo"|null>(null);
  const [chatError,    setChatError]    = useState("");
  const [mostrarHistorialChats, setMostrarHistorialChats] = useState(false);

  // ── BillSense Voz ───────────────────────────────────────────────
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("idle");
  const [liveMsgs,   setLiveMsgs]   = useState<{role:"user"|"agent"; text:string}[]>([]);
  const liveRef = useRef<GeminiLiveClient | null>(null);
  const [detail]  = useState("CORTO");
  const micIssue  = microphoneSupportError();

  // ── Scroll ──────────────────────────────────────────────────────
  const fin      = useRef<HTMLDivElement | null>(null);
  const vozScroll= useRef<HTMLDivElement | null>(null);
  const mensajesAsesorVistos = useRef(new Set<string>());

  // ── Demo accounts ───────────────────────────────────────────────
  const cuentas  = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const sugerida = cuentas.data?.demo?.[0] ?? "";

  // ── Effects ─────────────────────────────────────────────────────
  useEffect(() => () => { void liveRef.current?.close(); }, []);

  useEffect(() => {
    fin.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [mensajes, opinion, liveMsgs]);

  useEffect(() => {
    vozScroll.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [liveMsgs]);

  // ── Derived ─────────────────────────────────────────────────────
  const cuentaElegida = (cuentaEntrada || sugerida).trim();
  const liveActivo    = Boolean(liveRef.current);
  const liveIsVisible = liveActivo || ["connecting","listening","consulting","speaking"].includes(liveStatus);
  const oferta        = ultima?.acciones.find((a) => a.id === "VER_ALTERNATIVAS");
  // Igual que la oferta de arriba: BillSense la sugiere sola (backend, `explicar.py`)
  // cuando queda saldo por pagar; el chat solo pinta el botón si está en la lista.
  const puedePagar    = Boolean(ultima?.acciones.some((a) => a.id === "PAGAR"));
  // Misma idea que `puedePagar`: BillSense ya sugiere "Explíqueme mi recibo" en sus
  // `acciones` (backend, `_acciones_de_intencion`) cuando el cliente no pidió nada
  // concreto todavía. Antes esa acción no hacía nada en el chat de texto; ahora lleva
  // a la pantalla de recibo que ya existe, en vez de duplicarla como otro turno de chat.
  const puedeVerDetalle = Boolean(ultima?.acciones.some((a) => a.id === "VER_DETALLE"));
  const derivada      = ultima?.derivacion.requerida;
  const sube          = (hechos?.delta_total_cent ?? 0) >= 0;
  const nombreCliente = cuenta
    ? cuenta.replace("C-DEMO-","Usuario ").replace("C-","Cliente ")
    : "Cliente";

  // Situación del cliente, para el dashboard — todo sale del FactSet que ya trajo el
  // login (`hechos`), nada se inventa aquí. La fecha de vencimiento es la única cifra
  // que el frontend calcula (días restantes), y es aritmética sobre un dato real, no
  // un importe nuevo.
  const deudaPendienteCent = (hechos?.deuda_anterior_cent as number | undefined) ?? 0;
  const tieneDeudaAnterior = deudaPendienteCent > 0;
  const vencimiento        = hechos?.fecha_vencimiento ? new Date(hechos.fecha_vencimiento) : null;
  const diasParaVencer     = vencimiento ? Math.ceil((vencimiento.getTime() - Date.now()) / 86_400_000) : null;
  const reciboVencido      = diasParaVencer !== null && diasParaVencer < 0;
  // "Cuenta al día" solo si NINGÚN indicador está en rojo. Antes se calculaba mirando
  // únicamente la deuda arrastrada de ciclos previos, así que un cliente sin deuda
  // anterior pero con el recibo del propio mes vencido veía "Cuenta al día" y "Recibo
  // vencido" en la misma tarjeta —dos badges contradictorios sobre el mismo estado.
  const alDia              = !tieneDeudaAnterior && !reciboVencido;
  const pctDelta           = hechos && hechos.total_previo_cent > 0
    ? Math.round(Math.abs(hechos.delta_total_cent) / hechos.total_previo_cent * 100)
    : null;
  const totalLineas        = hechos?.lineas?.length ?? 0;
  const lineasVariaron     = hechos?.lineas?.filter((l) => l.delta_cent !== 0).length ?? 0;
  const pctLineasVariaron  = totalLineas > 0 ? Math.round((lineasVariaron / totalLineas) * 100) : 0;

  /** "Efecto efervescente" del cierre: solo cuando el turno fue una despedida
   * (`Intencion.DESPEDIDA` en el backend, ver `apps/api/routers/explicar.py`) y la
   * cuenta tiene beneficios vigentes. Los beneficios NUNCA salen del texto que redactó
   * el modelo —ese turno no tiene FactSet y cualquier cifra suya sería una alucinación—
   * sino del `FactSet` que ya se cargó al entrar (`hechos.beneficios_vigentes`). Si la
   * cuenta no tiene ninguno, devuelve `undefined` y el cierre queda en el texto simple. */
  const beneficiosDeCierre = (res: Explanation): string[] | undefined => {
    if (String(res.telemetria?.intencion ?? "") !== "DESPEDIDA") return undefined;
    const lista = hechos?.beneficios_vigentes ?? [];
    return lista.length ? lista : undefined;
  };

  // ── Mutations ───────────────────────────────────────────────────
  const entrar = useMutation({
    mutationFn: async (id: string) => {
      const emitido = await api.token(id);
      const factset = await api.facts(emitido.access_token);
      return { id, token: emitido.access_token, factset };
    },
    onSuccess: ({ id, token: tk, factset }) => {
      setCuenta(id); setToken(tk); setHechos(factset); setChatError(""); onSesion(id, tk);
      setPantalla("dashboard");
    },
    onError: (e) => setChatError(err(e)),
  });

  const explicar = useMutation({
    mutationFn: (texto: string) =>
      api.explain(token, {
        conversation_id: conversacion, cuenta_id: cuenta, verbosidad: detail, utterance: texto,
        // Si el cliente está viendo un recibo del historial, BillSense contesta
        // sobre ESE periodo; si no, `periodo` va vacío y el backend usa el actual.
        periodo: periodoActivo ?? undefined,
      }),
    onSuccess: (res) => {
      setConversacion(res.conversation_id);
      setUltima(res); setOpinion(null);
      setMensajes((p) => [...p, { rol: "asistente", bloques: res.bloques, beneficiosCierre: beneficiosDeCierre(res) }]);
      onExplicacion(res);
      void historialChats.refetch();
    },
    onError: (e) => setChatError(err(e)),
  });

  // Hasta 5 recibos anteriores al actual (`GET /v1/historial`), cargados solo
  // cuando el cliente entra a la pantalla de historial.
  const historial = useQuery({
    queryKey: ["historial", cuenta],
    queryFn: () => api.historial(token),
    enabled: pantalla === "historial" && Boolean(token),
  });

  const historialChats = useQuery({
    queryKey: ["conversaciones", cuenta],
    queryFn: () => api.conversaciones(token),
    enabled: mostrarHistorialChats && Boolean(token),
  });

  // El asesor trabaja sobre esta misma conversación. El cliente solo recibe el estado
  // público de la sala y los mensajes humanos; nunca el brief ni las notas internas.
  const salaCliente = useQuery({
    queryKey: ["sala-cliente", token, conversacion],
    queryFn: () => api.estadoCliente(token, conversacion!),
    enabled: Boolean(token && conversacion),
    refetchInterval: 3000,
  });
  useEffect(() => {
    const nuevos = salaCliente.data?.turnos.filter((turno) => {
      const clave = `${turno.ts ?? ""}:${turno.texto}`;
      if (turno.rol !== "asesor" || mensajesAsesorVistos.current.has(clave)) return false;
      mensajesAsesorVistos.current.add(clave);
      return true;
    }) ?? [];
    if (nuevos.length) {
      setMensajes((previos) => [
        ...previos,
        ...nuevos.map((turno) => ({ rol: "asesor" as const, texto: turno.texto })),
      ]);
    }
  }, [salaCliente.data]);

  const nuevoChat = useMutation({
    mutationFn: () => api.nuevaConversacion(token, periodoActivo ?? undefined),
    onSuccess: (chat) => {
      setConversacion(chat.conversation_id);
      setMensajes([]);
      setUltima(null);
      setOpinion(null);
      setChatError("");
      setMostrarHistorialChats(false);
      void historialChats.refetch();
    },
    onError: (e) => setChatError(err(e)),
  });

  const cargarChat = useMutation({
    mutationFn: (conversationId: string) => api.conversacion(token, conversationId),
    onSuccess: (chat) => {
      setConversacion(chat.conversation_id);
      setPeriodoActivo(chat.periodo ?? null);
      setMensajes(chat.mensajes.map((mensaje) => ({
        rol: mensaje.rol === "cliente" ? "cliente" : "asistente",
        texto: mensaje.contenido,
        bloques: mensaje.bloques ?? undefined,
      })));
      setUltima(null);
      setOpinion(null);
      setMostrarHistorialChats(false);
      setChatError("");
    },
    onError: (e) => setChatError(err(e)),
  });

  /** Carga el `FactSet` de un periodo pasado para verlo y, si el cliente quiere,
   * preguntarle a BillSense por él. El periodo más antiguo del historial puede no
   * tener uno anterior con el que compararse (`SIN_RECIBO_PREVIO`, 422) — se
   * muestra como aviso, no como error roto. */
  const verPeriodo = async (periodo: string) => {
    setCargandoPeriodo(true); setErrorPeriodo(""); setHechosPeriodo(null);
    try {
      setHechosPeriodo(await api.facts(token, periodo));
    } catch (e) {
      setErrorPeriodo(
        e instanceof ApiError && e.code === "SIN_RECIBO_PREVIO"
          ? "Este es el recibo más antiguo disponible: no hay uno previo con el que compararlo todavía."
          : err(e)
      );
    } finally {
      setCargandoPeriodo(false);
    }
  };

  const volverAlPeriodoActual = () => { setPeriodoActivo(null); setChatError(""); };

  // ── Handlers ────────────────────────────────────────────────────
  const preguntar = (texto: string) => {
    const t = texto.trim();
    if (!t || explicar.isPending || !token) return;
    setMensajes((p) => [...p, { rol: "cliente", texto: t }]);
    setBorrador("");
    explicar.mutate(t);
  };

  const enviar = (e: FormEvent) => { e.preventDefault(); preguntar(borrador); };

  /** Muestra la tarjeta de desglose de factura como mensaje inline */
  const mostrarDetalleRecibo = () => {
    setMensajes((p) => [...p, { rol: "asistente", texto: "", esRecibo: true }]);
  };

  const abrirChat = (q?: string, modo: ChatModo = "chat") => {
    setPantalla("chat");
    setChatModo(modo);
    setBottomTab("billsense");
    if (q && !mensajes.length && !borrador.trim()) {
      setBorrador(q);
    }
  };

  /** "Ver opciones" en Mejorar mi plan. No hay catálogo de planes con precio/GB en el
   * FactSet —lo confirma el propio backend (VER_ALTERNATIVAS no lleva cifras, ver
   * explicar.py)—, así que esta pantalla no inventa ninguna. Si ya se preguntó algo en
   * la sesión se reusa esa respuesta; si no, hay que pedirla explícitamente. */
  const abrirMejorarPlan = () => setPantalla("mejorarPlan");
  const consultarAlternativas = () =>
    explicar.mutate("¿Qué alternativas de plan tengo disponibles según mi cuenta?");

  const pad = (key: string) => {
    if (key === "⌫") setCuentaEntrada((p) => p.slice(0, -1));
    else if (key) setCuentaEntrada((p) => p + key);
  };

  const stopLive = async () => {
    if (!liveRef.current) return;
    const c = liveRef.current; liveRef.current = null; await c.close();
  };

  const toggleLive = async () => {
    if (liveRef.current) { await stopLive(); setLiveStatus("idle"); return; }
    if (!token || !cuenta) return;
    setLiveMsgs([]); setChatError("");
    const client = new GeminiLiveClient(
      { authToken: token, accountId: cuenta, conversationId: conversacion, detail },
      {
        onStatus: setLiveStatus,
        onInputTranscript: (t) => setLiveMsgs((cur) => {
          const last = cur[cur.length - 1];
          if (last?.role === "user") return [...cur.slice(0,-1), { role:"user" as const, text:`${last.text} ${t}`.trim() }];
          return [...cur, { role:"user" as const, text:t.trim() }];
        }),
        // Se descarta a propósito: es la narración improvisada que Gemini dice en voz
        // alta mientras suena el audio, no la respuesta verificada contra el FactSet.
        // Pintarla junto al bloque verificado duplicaba la respuesta —una hablada, otra
        // con las cifras comprobadas—; el chat de voz enseña solo la verificada, con el
        // estado ("BillSense está respondiendo…") como única señal mientras habla.
        onOutputTranscript: () => {},
        onExplanation: (res) => {
          setConversacion(res.conversation_id);
          setUltima(res); setOpinion(null);
          // `liveMsgs` guarda lo dicho por voz mientras dura la sesión, pero este
          // callback quedó cerrado sobre el valor que tenía al conectar —no el que
          // tiene ahora—, así que leerlo directo aquí perdía la pregunta hablada justo
          // al contestarla. La forma funcional de `setLiveMsgs` sí ve el valor actual:
          // se usa para rescatar la pregunta antes de vaciar la transcripción en vivo.
          setLiveMsgs((current) => {
            const pregunta = current.filter((m) => m.role === "user").map((m) => m.text).join(" ").trim();
            setMensajes((p) => {
              const conPregunta = pregunta ? [...p, { rol:"cliente" as const, texto:pregunta, esVoz:true }] : p;
              return [...conPregunta, { rol:"asistente" as const, bloques:res.bloques, esVoz:true, beneficiosCierre: beneficiosDeCierre(res) }];
            });
            return [];
          });
          onExplicacion(res);
          void historialChats.refetch();
        },
        onError: setChatError,
      }
    );
    liveRef.current = client;
    try { await client.connect(); }
    catch (e) { liveRef.current = null; setChatError(err(e)); await client.close(); setLiveStatus("error"); }
  };

  const navTo = (tab: string) => {
    setBottomTab(tab);
    if      (tab === "inicio")     setPantalla("dashboard");
    else if (tab === "recibo")     setPantalla("recibo");
    else if (tab === "billsense")  {
      setPantalla("chat"); setChatModo("chat"); setBottomTab("billsense");
    } else if (tab === "tienda" || tab === "beneficios") {
      setBottomTab(tab);
      return;
    }
  };

  /** Genera un PDF imprimible del recibo en una ventana nueva */
  const generarPDF = (fs: FactSet | null = hechos) => {
    if (!fs) return;
    const deudaAnterior = fs.deuda_anterior_cent ?? 0;
    const totalAPagar = fs.total_a_pagar_cent ?? (fs.total_actual_cent + deudaAnterior);
    const vencimiento = fs.fecha_vencimiento ? String(fs.fecha_vencimiento) : null;
    const vencido = Boolean(vencimiento && vencimiento < new Date().toISOString().slice(0, 10));
    const subio = fs.delta_total_cent >= 0;
    const escapar = (valor: unknown) => String(valor ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

    // El PDF imprime siempre tanto el total de cada categoría como cada cargo
    // facturado que la tarjeta permite desplegar. Todos los importes vienen del
    // mismo FactSet usado por `ReceiptDetailCard`.
    const filas = agruparLineas(fs.lineas).map((grupo) => `
      <tr class="grupo">
        <td>${escapar(grupo.etiqueta)}</td>
        <td class="${grupo.aFavor ? "credito" : ""}">${grupo.aFavor ? "− " : ""}${escapar(soles(Math.abs(grupo.monto_cent)))}</td>
      </tr>
      ${grupo.lineas.map((linea) => `
        <tr class="detalle">
          <td><strong>${escapar(linea.nombre_comercial)}</strong><small>${escapar(linea.concepto_id)}</small></td>
          <td>${escapar(soles(linea.monto_actual_cent))}</td>
        </tr>`).join("")}
    `).join("");
    const html = `<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<title>Factura Movistar ${escapar(fs.periodo_actual)}</title>
<style>
  body{font-family:Arial,sans-serif;max-width:580px;margin:0 auto;padding:24px;color:#172033}
  .top{background:#019DF4;color:#fff;padding:20px 24px;border-radius:12px;margin-bottom:16px}
  .top h1{margin:0;font-size:22px} .top p{margin:4px 0 0;font-size:13px;opacity:.8}
  .hero{background:#e4f7fd;border:1.5px solid #b3e0f7;border-radius:12px;padding:20px;text-align:center;margin-bottom:16px}
  .hero .amt{font-size:36px;font-weight:900;color:#019DF4;margin:8px 0}
  .hero .due{color:#cc4c3d;font-size:12px;font-weight:700}
  h2{font-size:12px;color:#68768a;text-transform:uppercase;letter-spacing:.09em;margin:22px 0 8px}
  table{width:100%;border-collapse:collapse;margin-bottom:16px}
  td{padding:10px 8px;border-bottom:1px solid #edf0f4;font-size:13px}
  td:last-child{text-align:right;font-weight:600}
  .grupo td{font-weight:800;background:#f7f9fb;border-top:1px solid #dce4ec}
  .detalle td{padding-top:7px;padding-bottom:7px;color:#526174}
  .detalle td:first-child{padding-left:22px}.detalle strong{display:block;font-weight:600}
  .detalle small{display:block;color:#9aaabd;font-size:10px;margin-top:2px}
  .credito{color:#3a7a10!important}
  .alert td{background:#fff2f2;color:#cc4c3d}
  .total td{background:#e4f7fd;font-weight:800;font-size:15px}
  .estado-vencido{color:#cc4c3d}.estado-vigente{color:#3a7a10}
  .footer{font-size:11px;color:#9fb1c4;text-align:center;margin-top:20px}
  @media print{.no-print{display:none}}
</style></head><body>
<div class="top">
  <h1>Detalle de Facturación</h1>
  <p>Ciclo: ${escapar(fs.periodo_actual)} · Variación: ${subio ? "+" : "−"}${escapar(soles(Math.abs(fs.delta_total_cent)))}</p>
</div>
<div class="hero">
  <p style="margin:0;font-size:12px;color:#68768a;text-transform:uppercase;letter-spacing:.1em">Total a Pagar</p>
  <p class="amt">${escapar(soles(totalAPagar))}</p>
  ${vencimiento ? `<p class="due">⚠ ${vencido ? "Venció el" : "Vence"}: ${escapar(vencimiento)}</p>` : ""}
</div>
<h2>Detalle de cargos facturados</h2>
<table>
  ${filas}
  ${deudaAnterior > 0 ? `<tr class="alert"><td>⚠ Deuda pasada</td><td>${escapar(soles(deudaAnterior))}</td></tr>` : ""}
  <tr class="total"><td>Total a pagar</td><td>${escapar(soles(totalAPagar))}</td></tr>
</table>
<h2>Resumen de mi cuenta</h2>
<table>
  <tr><td>Estado</td><td class="${vencido ? "estado-vencido" : "estado-vigente"}">${vencido ? "Vencido" : "Vigente"}</td></tr>
  <tr><td>Vencimiento</td><td>${escapar(vencimiento ?? "—")}</td></tr>
  <tr><td>Código de pago</td><td>${escapar(cuenta)}</td></tr>
  <tr><td>Total</td><td>${escapar(soles(totalAPagar))}</td></tr>
  <tr><td>Modalidad</td><td>${escapar(fs.modalidad_renta)}</td></tr>
</table>
<h2>Comparativa con el mes anterior</h2>
<table>
  <tr><td>Mes anterior</td><td>${escapar(soles(fs.total_previo_cent))}</td></tr>
  <tr><td>Este mes</td><td>${escapar(soles(fs.total_actual_cent))}</td></tr>
  <tr><td>Variación</td><td>${subio ? "+" : "−"}${escapar(soles(Math.abs(fs.delta_total_cent)))}</td></tr>
</table>
<p class="footer">Movistar Perú · Cuenta ${escapar(cuenta)} · Generado: ${escapar(new Date().toLocaleDateString("es-PE"))}</p>
<button class="no-print" onclick="window.print()" style="margin-top:16px;width:100%;padding:12px;background:#019DF4;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer">Imprimir / Guardar PDF</button>
</body></html>`;
    const w = window.open("","_blank","width=680,height=860");
    if (w) { w.document.write(html); w.document.close(); }
  };

  // ── Shared header ────────────────────────────────────────────────
  const hdr = (title: ReactNode, back?: () => void, extra?: ReactNode) => (
    <div className="mm-header">
      {back
        ? <button onClick={back} className="mm-back-btn" aria-label="Volver"><ArrowLeft size={20} /></button>
        : <div style={{ width:32 }} />}
      <div className="mm-header-title">
        <MovistarLogo className="h-5 w-auto" />
        <h1 className="mm-title">{title}</h1>
      </div>
      <div style={{ width:32, display:"flex", justifyContent:"flex-end" }}>{extra}</div>
    </div>
  );

  const F = "mm-frame";

  // ════════════════════════════════════════════════════════════════
  // SPLASH
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "splash") return (
    <div className={F}>
      <div className="mm-splash">
        <div className="mm-splash-wave" />
        <div className="mm-splash-content">
          <div className="mm-splash-logo-wrap"><MovistarLogo className="h-16 w-auto" /></div>
          <h1 className="mm-splash-title">App Mi Movistar</h1>
          <p className="mm-splash-sub">Gestiona servicios, consulta recibos y conversa con BillSense, tu asistente IA.</p>
          <div className="mm-splash-chips">
            <span className="mm-chip"><Wifi size={13} /> Consumos</span>
            <span className="mm-chip"><CreditCard size={13} /> Recibos</span>
            <span className="mm-chip"><Bot size={13} /> BillSense</span>
          </div>
        </div>
        <div className="mm-splash-actions">
          <button className="mm-btn-primary" onClick={() => { setCuentaEntrada(""); setChatError(""); setPantalla("loginCuenta"); }}>Iniciar sesión</button>
          <button className="mm-btn-outline" onClick={() => { setAceptaTC(false); setCuentaEntrada(""); setChatError(""); setPantalla("registroCuenta"); }}>
            Registrarme
          </button>
        </div>
        <p className="mm-splash-footer">
          Al ingresar aceptas los <span className="mm-link">Términos y condiciones</span> de Movistar Perú.
        </p>
      </div>
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // ACCESO UNIFICADO POR CUENTA CLIENTE
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "loginCuenta" || pantalla === "registroCuenta") {
    const esReg = pantalla === "registroCuenta";
    const puede = cuentaElegida.length > 0 && (!esReg || aceptaTC);
    return (
      <div className={F}>
        {hdr(esReg ? "Registrarme" : "Iniciar sesión", () => setPantalla("splash"))}
        <div className="mm-dni-body">
          <div className="mm-dni-illustration">
            <div className="mm-dni-ilu-circle"><UserIcon size={38} className="text-[#019DF4]" /></div>
            {esReg && <div className="mm-dni-ilu-badge"><ShieldCheck size={14} className="text-white" /></div>}
          </div>
          <h2 className="mm-dni-titulo">Ingresa a tu cuenta</h2>
          <p className="mm-dni-sub">Escribe tu cuenta cliente asociada a tus recibos</p>
          <div className="mm-dni-form">
            <input
              className="mm-dni-display"
              aria-label="Cuenta cliente"
              type="text"
              inputMode="numeric"
              autoComplete="username"
              spellCheck={false}
              value={cuentaEntrada}
              placeholder={sugerida || "N.º de cuenta cliente"}
              onChange={(e) => setCuentaEntrada(e.target.value.trim())}
              onKeyDown={(e) => {
                if (e.key === "Enter" && puede && !entrar.isPending) {
                  entrar.mutate(cuentaElegida);
                }
              }}
            />
          </div>
          {chatError && <p className="mm-error-msg">{chatError}</p>}
          {esReg && (
            <label className="mm-tc-label">
              <input type="checkbox" checked={aceptaTC} onChange={(e) => setAceptaTC(e.target.checked)} className="mm-tc-check" />
              <span>He leído la <span className="mm-link">política de privacidad</span> y acepto los{" "}
                <span className="mm-link">términos y condiciones</span></span>
            </label>
          )}
          <button id={esReg?"btn-soy-titular":"btn-continuar"} className="mm-btn-primary"
            disabled={!puede || entrar.isPending} onClick={() => entrar.mutate(cuentaElegida)}>
            {entrar.isPending
              ? <><Loader2 size={18} className="animate-spin" /> Verificando…</>
              : esReg ? <><ShieldCheck size={18} /> Registrarme</> : "Continuar"}
          </button>
        </div>
        <div className="mm-numpad">
          {PAD.map((row, ri) => (
            <div key={ri} className="mm-numpad-row">
              {row.map((k, ki) => (
                <button key={ki} className={`mm-numpad-btn${k===""?" empty":""}${k==="⌫"?" del":""}`}
                  onClick={() => pad(k)} disabled={k===""} aria-label={k==="⌫"?"Borrar":k||undefined}>
                  {k}
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════════
  // DASHBOARD — Vista 3 (con bottom nav)
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "dashboard") return (
    <div className={F}>
      <div className="mm-dash-hdr">
        <div>
          <p className="mm-dash-greeting">¡Hola,</p>
          <h1 className="mm-dash-name">{nombreCliente}!</h1>
        </div>
        <button className="mm-dash-line-pill" onClick={() => navTo("recibo")}>
          <Smartphone size={13} />
          <span>{hechos?.modalidad_renta ?? "Cuenta cliente"}</span>
        </button>
      </div>
      <div className="mm-dash-scroll">
        {/* MI RECIBO — primer bloque del inicio */}
        <div className="mm-recibo-card">
          <div
            className="mm-recibo-card-top clicable"
            role="button"
            tabIndex={0}
            aria-label="Ver detalle de mi recibo"
            onClick={() => setPantalla("recibo")}
            onKeyDown={(e) => {
              if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                setPantalla("recibo");
              }
            }}
          >
            <div className="mm-recibo-card-icon"><CreditCard size={22} /></div>
            <div className="mm-recibo-card-info">
              <p className="mm-recibo-card-label">Mi recibo</p>
              <p className="mm-recibo-card-sub">Paga tu plan aquí</p>
            </div>
            <div className="mm-recibo-card-amount">
              <strong>{soles(hechos?.total_actual_cent ?? 0)}</strong>
              <span>{hechos?.periodo_actual ?? "—"}</span>
            </div>
          </div>
          <div className="mm-recibo-card-btns">
            <button id="btn-ver-recibo" className="mm-rc-btn" onClick={() => setPantalla("recibo")}>Ver recibo</button>
            <button id="btn-detalle-factura" className="mm-rc-btn primary"
              onClick={() => abrirChat("Consulta sobre tu recibo", "chat")}>
              Consulta sobre tu recibo
            </button>
          </div>
        </div>
        {/* Mi situación — con datos reales del recibo, no genéricos */}
        {hechos && (
          <div className="mm-situacion-card">
            <div className="mm-situacion-row">
              <span className={`mm-situacion-badge ${alDia ? "ok" : "alerta"}`}>
                {alDia ? <ShieldCheck size={13}/> : <AlertCircle size={13}/>}
                {tieneDeudaAnterior
                  ? `Deuda anterior: ${soles(deudaPendienteCent)}`
                  : reciboVencido
                  ? "Recibo vencido"
                  : "Cuenta al día"}
              </span>
              {/* Detalle de fecha aparte del badge de estado: da el número de días sin
                  repetir la misma frase ("Recibo vencido") que ya dijo el badge de arriba. */}
              {diasParaVencer !== null && (
                <span className={`mm-situacion-venc${diasParaVencer <= 3 ? " urgente" : ""}`}>
                  {diasParaVencer < 0
                    ? `Vencido hace ${Math.abs(diasParaVencer)} día${Math.abs(diasParaVencer) === 1 ? "" : "s"}`
                    : diasParaVencer === 0
                    ? "Vence hoy"
                    : `Vence en ${diasParaVencer} día${diasParaVencer === 1 ? "" : "s"}`}
                </span>
              )}
            </div>
            <div className="mm-situacion-row">
              <span className={`mm-situacion-tendencia ${sube ? "up" : "down"}`}>
                {sube ? <TrendingUp size={14}/> : <TrendingDown size={14}/>}
                Tu recibo {sube ? "subió" : "bajó"} {soles(Math.abs(hechos.delta_total_cent))}
                {pctDelta !== null ? ` (${pctDelta}%)` : ""} respecto al mes anterior
              </span>
            </div>
            <div className="mm-situacion-row">
              <span className="mm-situacion-plan"><Smartphone size={13}/> {hechos.modalidad_renta}</span>
              <button className="mm-situacion-link" onClick={() => setPantalla("comoFunciona")}>
                <Layers size={12}/> ¿Cómo se calcula?
              </button>
            </div>
            {oferta ? (
              <button className="mm-situacion-promo" onClick={() => abrirChat(`Cuéntame más sobre: ${oferta.etiqueta}`)}>
                <Gift size={14}/><span>{oferta.etiqueta}</span><span className="mm-situacion-promo-arrow">›</span>
              </button>
            ) : (
              <button className="mm-situacion-promo ghost" onClick={() => abrirChat("¿Qué promociones u ofertas tengo disponibles según mi cuenta?")}>
                <Gift size={14}/><span>Pregúntale a BillSense por promociones para ti</span><span className="mm-situacion-promo-arrow">›</span>
              </button>
            )}
          </div>
        )}
        {/* Banner — antes era un anuncio fijo de "35 GB" sin ningún respaldo en el
            FactSet. Se reemplaza por algo real: una invitación a que BillSense
            explique los descuentos, que sí sabe explicar con datos verificados. */}
        <button className="mm-dash-banner" onClick={() => abrirChat("Explícame cómo funcionan mis descuentos y cuándo terminan")}>
          <div>
            <p className="mm-dash-banner-kicker">💡 ¿Sabías esto?</p>
            <p className="mm-dash-banner-text">Los descuentos <strong>bajan o terminan</strong> con el tiempo — pregúntale a BillSense cuándo vence el tuyo.</p>
          </div>
          <span className="mm-dash-banner-btn">Ver</span>
        </button>
        {/* Mejorar plan */}
        <div className="mm-dash-card">
          <div className="mm-dash-card-icon" style={{ background:"#f3eeff", color:"#7c3aed" }}><Star size={22} /></div>
          <div className="mm-dash-card-info">
            <strong>Mejorar mi plan</strong><span>Encuentra el plan perfecto para ti</span>
          </div>
          <button className="mm-dash-card-cta" onClick={abrirMejorarPlan}>
            Ver opciones
          </button>
        </div>
        {/* PAGOS — apartado propio en el inicio, con su código de pago y pasos, sin
            derivar a ningún canal externo. */}
        <div className="mm-dash-card">
          <div className="mm-dash-card-icon" style={{ background:"#edfae1", color:"#3a7a10" }}><Wallet size={22} /></div>
          <div className="mm-dash-card-info">
            <strong>Pagar mi recibo</strong><span>Código de pago y pasos para pagar</span>
          </div>
          <button id="btn-pagar-inicio" className="mm-dash-card-cta" onClick={() => setMostrarPago(true)} disabled={!hechos}>
            Pagar
          </button>
        </div>
        {/* Resumen de mi recibo — el dataset del desafío describe facturación
            (líneas, deuda, variación), no consumo de red (GB/minutos usados); esos
            campos no existen en el FactSet, así que mostrarlos sería inventar cifras
            que ningún endpoint respalda. Estos tres indicadores sí salen de `hechos`. */}
        <p className="mm-dash-section-title">Resumen de mi recibo</p>
        <div className="mm-consumos">
          {[
            {
              icon:<Wifi size={18}/>, label:"Conceptos en tu recibo",
              val: `${totalLineas} línea${totalLineas === 1 ? "" : "s"}`,
              pct: pctLineasVariaron, color:"#019DF4", bg:"#e4f7fd",
            },
            {
              icon:<AlertCircle size={18}/>, label:"Deuda anterior",
              val: soles(deudaPendienteCent),
              pct: tieneDeudaAnterior ? 100 : 0,
              color: tieneDeudaAnterior ? "#cc4c3d" : "#5BC500",
              bg:   tieneDeudaAnterior ? "#fff2f2" : "#edfae1",
            },
            {
              icon: sube ? <TrendingUp size={18}/> : <TrendingDown size={18}/>,
              label:"Variación vs. mes anterior",
              val: `${sube ? "+" : "-"}${soles(Math.abs(hechos?.delta_total_cent ?? 0))}`,
              pct: pctDelta ?? 0,
              color: sube ? "#cc4c3d" : "#5BC500",
              bg:   sube ? "#fff2f2" : "#edfae1",
            },
          ].map((c) => (
            <div key={c.label} className="mm-consumo">
              <div className="mm-consumo-icon" style={{ background:c.bg, color:c.color }}>{c.icon}</div>
              <div className="mm-consumo-body">
                <div className="mm-consumo-row"><span>{c.label}</span><strong>{c.val}</strong></div>
                <div className="mm-consumo-track">
                  <div className="mm-consumo-fill" style={{ width:`${c.pct}%`, background:c.color }} />
                </div>
              </div>
            </div>
          ))}
        </div>
        <div style={{ height:80 }} />
      </div>
      {/* Bottom nav */}
      <nav className="mm-bottom-nav" aria-label="Navegación inferior">
        {[
          { tab:"inicio",     icon:<HomeIcon size={22}/>,    label:"Inicio" },
          { tab:"recibo",     icon:<CreditCard size={22}/>,  label:"Recibo" },
          { tab:"billsense",  icon:<Bot size={22}/>,         label:"BillSense" },
          { tab:"tienda",     icon:<ShoppingBag size={22}/>, label:"Tienda" },
          { tab:"beneficios", icon:<Gift size={22}/>,        label:"Beneficios" },
        ].map(({ tab, icon, label }) => (
          <button key={tab} className={`mm-nav-item${bottomTab===tab?" active":""}`}
            onClick={() => navTo(tab)} aria-label={label}>
            {icon}<span>{label}</span>
          </button>
        ))}
      </nav>
      {mostrarPago && hechos && <PagoModal hechos={hechos} cuentaId={cuenta} onClose={() => setMostrarPago(false)} />}
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // RECIBO — Detalle completo
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "recibo") return (
    <div className={F}>
      {hdr(
        "Mi Recibo",
        () => setPantalla("dashboard"),
        <button
          type="button"
          className="mm-back-btn"
          aria-label="Ver historial de recibos"
          title="Historial de recibos"
          onClick={() => {
            setHechosPeriodo(null);
            setErrorPeriodo("");
            setPantalla("historial");
          }}
        >
          <Clock size={19} />
        </button>,
      )}
      <div className="mm-recibo-body">
        <div className="mm-recibo-hero">
          <p className="mm-recibo-period">Período {hechos?.periodo_actual}</p>
          <p className="mm-recibo-amount">{soles(hechos?.total_actual_cent ?? 0)}</p>
          <p className={`mm-recibo-delta ${sube?"up":"down"}`}>
            {sube?"▲":"▼"} {soles(Math.abs(hechos?.delta_total_cent ?? 0))} respecto del mes anterior
          </p>
        </div>
        {hechos && (
          <div style={{ marginBottom:12 }}>
            <ReceiptDetailCard
              hechos={hechos}
              cuentaId={cuenta}
              onDownload={() => generarPDF()}
              mostrarDetalleCargos
            />
          </div>
        )}
        <button className="mm-btn-outline" onClick={() => setPantalla("comoFunciona")}>
          <Layers size={18} /> ¿Cómo se calculó mi recibo?
        </button>
        <button className="mm-btn-primary" style={{ marginTop:12 }} onClick={() => abrirChat("Explícame mi recibo detalladamente")}>
          <Bot size={18} /> Explícame este recibo con BillSense
        </button>
        <button className="mm-btn-outline" style={{ marginTop:12 }} onClick={() => setPantalla("dashboard")}>
          Volver al inicio
        </button>
      </div>
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // HISTORIAL — hasta 5 recibos anteriores (GET /v1/historial)
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "historial") return (
    <div className={F}>
      {hdr("Recibos anteriores", () => setPantalla("dashboard"))}
      <div className="mm-historial-body">
        {!hechosPeriodo && !errorPeriodo && !cargandoPeriodo && (
          <>
            <p className="mm-historial-intro">Hasta 5 recibos antes del actual. Toca uno para verlo o preguntarle a BillSense.</p>
            {historial.isPending && <div className="mm-plan-loading"><Loader2 size={16} className="animate-spin"/> Cargando tu historial…</div>}
            {historial.isError && <p className="mm-error-msg">{err(historial.error)}</p>}
            <div className="mm-historial-lista">
              {historial.data?.map((r) => (
                <button key={r.periodo} className="mm-historial-item" disabled={r.es_actual}
                  onClick={() => void verPeriodo(r.periodo)}>
                  <div className="mm-historial-item-info">
                    <strong>{r.periodo}</strong>
                    <span>{r.modalidad_renta === "ADELANTADA" ? "Renta Adelantada" : "Renta Vencida"}</span>
                  </div>
                  <div className="mm-historial-item-cifras">
                    <strong>{soles(r.total_cent)}</strong>
                    <span>Vence {r.fecha_vencimiento}</span>
                  </div>
                  {r.es_actual ? <span className="mm-historial-badge">Actual</span> : <span className="mm-historial-arrow">›</span>}
                </button>
              ))}
            </div>
          </>
        )}
        {cargandoPeriodo && <div className="mm-plan-loading"><Loader2 size={16} className="animate-spin"/> Cargando ese recibo…</div>}
        {errorPeriodo && (
          <div className="mm-plan-empty-card">
            <p>{errorPeriodo}</p>
            <button className="mm-btn-outline" onClick={() => setErrorPeriodo("")}>‹ Volver a la lista</button>
          </div>
        )}
        {hechosPeriodo && !cargandoPeriodo && (
          <>
            <button className="mm-btn-outline" style={{ marginBottom:12 }} onClick={() => setHechosPeriodo(null)}>‹ Volver a la lista</button>
            <ReceiptDetailCard
              hechos={hechosPeriodo}
              cuentaId={cuenta}
              onDownload={() => generarPDF(hechosPeriodo)}
              mostrarDetalleCargos
            />
            <button className="mm-btn-primary" style={{ marginTop:12 }} onClick={() => {
              setPeriodoActivo(hechosPeriodo.periodo_actual);
              abrirChat(`Explícame mi recibo de ${hechosPeriodo.periodo_actual}`);
            }}>
              <Bot size={18} /> Preguntar a BillSense sobre este periodo
            </button>
          </>
        )}
      </div>
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // CÓMO SE CALCULA TU RECIBO — tipo de renta, ciclo y prorrateo reales
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "comoFunciona") return (
    <div className={F}>
      {hdr("Cómo se calcula tu recibo", () => setPantalla("recibo"))}
      {hechos
        ? <RentaExplicativa hechos={hechos} onPreguntar={(q) => abrirChat(q)} />
        : <div className="mm-plan-empty-card" style={{ margin:20 }}><p>Aún no hemos cargado tu recibo.</p></div>}
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // MEJORAR MI PLAN — plan actual (real) + recomendación de cross-selling
  // (real, cuando una regla explícita la habilita). Sin catálogo de planes
  // inventado: cualquier cifra de una alternativa la confirma BillSense,
  // que sí pasa por el verificador.
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "mejorarPlan") {
    const motivoOferta = typeof oferta?.payload?.motivo === "string" ? oferta.payload.motivo : null;
    return (
      <div className={F}>
        {hdr("Mejorar mi plan", () => setPantalla("dashboard"))}
        <div className="mm-plan-body">
          <div className="mm-plan-actual-card">
            <p className="mm-plan-actual-label">Tu plan actual</p>
            <div className="mm-plan-actual-row">
              <Smartphone size={18}/><strong>{hechos?.modalidad_renta ?? "—"}</strong>
            </div>
            <p className="mm-plan-actual-price">
              {soles(hechos?.total_actual_cent ?? 0)}
              <span>/ {hechos?.periodo_actual ?? "mes"}</span>
            </p>
          </div>

          <p className="mm-dash-section-title">Alternativa recomendada</p>

          {explicar.isPending ? (
            <div className="mm-plan-loading">
              <Loader2 size={16} className="animate-spin"/> Consultando con BillSense…
            </div>
          ) : oferta ? (
            <div className="mm-plan-oferta-card">
              <div className="mm-plan-oferta-header"><Gift size={16}/><span>{oferta.etiqueta}</span></div>
              {motivoOferta && <p className="mm-plan-oferta-motivo">{motivoOferta}</p>}
              <p className="mm-plan-oferta-nota">
                Los montos exactos se confirman en la conversación con BillSense, verificados contra tu recibo.
              </p>
              <button className="mm-btn-primary" onClick={() => abrirChat(`Cuéntame más sobre: ${oferta.etiqueta}`)}>
                <Bot size={16}/> Ver detalle con BillSense
              </button>
            </div>
          ) : ultima ? (
            <div className="mm-plan-empty-card">
              <p>Por ahora no encontramos una alternativa comercial que aplique a tu cuenta.</p>
              <p className="mm-plan-empty-sub">
                El cross-selling en BillSense solo se activa si hay una regla de negocio explícita para tu caso —no se
                fuerza una oferta a todos los clientes.
              </p>
            </div>
          ) : (
            <div className="mm-plan-empty-card">
              <p>Aún no hemos revisado tu cuenta en esta sesión.</p>
              <button className="mm-btn-outline" onClick={consultarAlternativas}>
                Consultar alternativas verificadas
              </button>
            </div>
          )}

          <button className="mm-btn-outline" onClick={() => abrirChat("¿Cuáles son los planes disponibles para mejorar mi servicio?")}>
            <MessageSquare size={16}/> Preguntar directamente a BillSense
          </button>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════════
  // CHAT / BILLSENSE — pantalla principal IA
  // Dos modos: "chat" (texto) y "voz" (audio)
  // ════════════════════════════════════════════════════════════════
  return (
    <div className={F}>
      {/* Header propio de esta pantalla: sin el logo de Movistar —aquí el
          protagonista es el asistente, no la operadora—, con el logo de
          BillSense y un título que dice para qué sirve la pantalla. */}
      <div className="mm-header">
        <button onClick={() => { setPantalla("dashboard"); setBottomTab("inicio"); }} className="mm-back-btn" aria-label="Volver"><ArrowLeft size={20} /></button>
        <div className="mm-header-title">
          <img src={billsenseLogo} alt="BillSense" className="mm-header-billsense-logo" />
          <h1 className="mm-title">Realiza tu consulta</h1>
        </div>
        <div className="mm-chat-header-actions">
          <button
            className="mm-chat-header-btn"
            aria-label="Historial de chats"
            title="Historial de chats"
            onClick={() => setMostrarHistorialChats((visible) => !visible)}
          ><History size={17} /></button>
          <button
            className="mm-chat-header-btn primary"
            aria-label="Nuevo chat"
            title="Nuevo chat"
            disabled={nuevoChat.isPending}
            onClick={() => nuevoChat.mutate()}
          >{nuevoChat.isPending ? <Loader2 size={16} className="animate-spin" /> : <Plus size={18} />}</button>
        </div>
      </div>

      {/* ── Selector de modo: Chat | BillSense Voz ─────────────── */}
      <div className="mm-mode-tabs">
        <button
          id="tab-chat"
          className={`mm-mode-tab${chatModo==="chat"?" active":""}`}
          onClick={() => setChatModo("chat")}
        >
          <MessageSquare size={14} /> Chat
        </button>
        <button
          id="tab-voz"
          className={`mm-mode-tab${chatModo==="voz"?" active":""}`}
          onClick={() => setChatModo("voz")}
        >
          <Mic size={14} /> BillSense Voz
        </button>
      </div>

      {mostrarHistorialChats && (
        <div className="mm-chat-history-panel">
          <div className="mm-chat-history-head">
            <div><strong>Historial de BillSense</strong><small>Conversaciones guardadas por fecha</small></div>
            <button onClick={() => setMostrarHistorialChats(false)} aria-label="Cerrar historial">×</button>
          </div>
          <div className="mm-chat-history-list">
            {historialChats.isPending && <div className="mm-chat-history-state"><Loader2 size={15} className="animate-spin" /> Cargando…</div>}
            {historialChats.isError && <div className="mm-chat-history-state error">{err(historialChats.error)}</div>}
            {historialChats.data?.length === 0 && <div className="mm-chat-history-state">Aún no hay conversaciones guardadas.</div>}
            {historialChats.data?.map((chat) => (
              <button
                key={chat.conversation_id}
                className={`mm-chat-history-item${chat.conversation_id === conversacion ? " active" : ""}`}
                onClick={() => cargarChat.mutate(chat.conversation_id)}
                disabled={cargarChat.isPending}
              >
                <History size={15} />
                <span><strong>{chat.titulo}</strong><small>{new Intl.DateTimeFormat("es-PE", { dateStyle:"medium", timeStyle:"short" }).format(new Date(chat.actualizada_en))}</small></span>
                <em>{chat.mensajes} mensajes{chat.periodo ? ` · ${chat.periodo}` : ""}</em>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ════════════════════════════════════════════
          MODO CHAT — texto sin audio
          ════════════════════════════════════════════ */}
      {chatModo === "chat" && (
        <>
          {/* Aviso de periodo — solo aparece si se entró desde el historial */}
          {periodoActivo && (
            <div className="mm-periodo-aviso">
              <Clock size={13} /><span>Consultando tu recibo de {periodoActivo}</span>
              <button onClick={volverAlPeriodoActual}>Volver al periodo actual</button>
            </div>
          )}
          {/* Strip de métricas */}
          {hechos && (
            <div className="mm-bill-strip">
              <div className="mm-bill-strip-item">
                <span>Recibo actual</span><strong>{soles(hechos.total_actual_cent)}</strong>
              </div>
              <div className="mm-bill-strip-sep" />
              <div className="mm-bill-strip-item">
                <span>Período</span><strong>{hechos.periodo_actual}</strong>
              </div>
              <div className="mm-bill-strip-sep" />
              <div className={`mm-bill-strip-item ${sube?"up":"down"}`}>
                <span>Variación</span>
                <strong style={{ color:sube?"#cc4c3d":"#5BC500" }}>{sube?"+":""}{soles(hechos.delta_total_cent)}</strong>
              </div>
              {hechos.fecha_vencimiento && <>
                <div className="mm-bill-strip-sep"/>
                <div className="mm-bill-strip-item">
                  <span>Vence</span><strong>{String(hechos.fecha_vencimiento)}</strong>
                </div>
              </>}
            </div>
          )}

          {/* Conversación */}
          <div className="mm-chat-scroll">
            {/* Bienvenida */}
            {mensajes.length === 0 && !explicar.isPending && (
              <div className="mm-chat-empty-state">
                <div className="mm-chat-empty-logo"><img src={billsenseLogo} alt="BillSense" /></div>
                <p>Tu conversación está lista. Escribe tu consulta sobre el recibo y BillSense te responderá aquí.</p>
              </div>
            )}

            {/* Mensajes */}
            {mensajes.map((m, i) => {
              if (m.esRecibo && hechos) {
                return (
                  <div key={i} className="mm-chat-turn assistant">
                    <div className="mm-chat-avatar-bot"><img src={billsenseLogo} alt="" /></div>
                    <ReceiptDetailCard hechos={hechos} cuentaId={cuenta} onDownload={() => generarPDF()} />
                  </div>
                );
              }
              const esAsi = m.rol === "asistente";
              const esAsesor = m.rol === "asesor";
              return (
                <div key={i} className={`mm-chat-turn ${esAsi || esAsesor ? "assistant" : "client"} ${esAsesor ? "advisor" : ""}`}>
                  {esAsi && (
                    <div className={`mm-chat-avatar-bot${m.esVoz?" voz":""}`}>
                      {m.esVoz ? <Mic size={14}/> : <img src={billsenseLogo} alt="" />}
                    </div>
                  )}
                  {esAsesor && <div className="mm-chat-avatar-advisor"><UserIcon size={14}/></div>}
                  <div className="mm-chat-bubble-col">
                    <div className={`mm-chat-bubble ${esAsi || esAsesor ? "assistant" : "client"} ${esAsesor ? "advisor" : ""}`}>
                      {esAsesor && <small className="mm-chat-advisor-label">Asesor Movistar</small>}
                      {esAsi && m.bloques ? <RichMessage bloques={m.bloques} /> : m.texto}
                    </div>
                    {esAsi && m.beneficiosCierre && (
                      <div className="mm-chat-efervescente">
                        <span className="mm-chat-efervescente-title"><Gift size={13}/> Recuerde que ya cuenta con:</span>
                        <div className="mm-renta-chips">
                          {m.beneficiosCierre.map((b) => <span className="mm-renta-chip" key={b}>{b}</span>)}
                        </div>
                      </div>
                    )}
                  </div>
                  {!esAsi && <div className="mm-chat-avatar-user"><UserIcon size={16}/></div>}
                </div>
              );
            })}

            {/* Cargando */}
            {explicar.isPending && (
              <div className="mm-chat-turn assistant">
                <div className="mm-chat-avatar-bot"><img src={billsenseLogo} alt="" /></div>
                <div className="mm-chat-typing">
                  <Loader2 size={14} className="animate-spin" style={{ color:"#019DF4" }} />
                  <span>BillSense está verificando cifras…</span>
                </div>
              </div>
            )}

            {/* Sugerencias rápidas (incluye desglose de factura) */}
            {mensajes.filter(m=>!m.esRecibo).length <= 1 && !explicar.isPending && (
              <div className="mm-chat-suggestions">
                <p className="mm-suggest-label">Acciones rápidas:</p>
                <button className="mm-suggest-btn mm-suggest-featured" onClick={mostrarDetalleRecibo}>
                  📄 Ver detalle de factura (PDF)
                </button>
                {[
                  "¿Por qué subió el monto de este mes?",
                  "¿El próximo mes me cobrarán lo mismo?",
                  "¿Qué incluye mi plan actual?",
                ].map((p) => (
                  <button key={p} onClick={() => preguntar(p)} className="mm-suggest-btn">{p}</button>
                ))}
              </div>
            )}

            {/* CTA de pago — BillSense la ofrece igual que ofrecería hablar con un
                asesor: aparece sola cuando el backend la incluyó en `acciones`. */}
            {puedePagar && !explicar.isPending && (
              <div className="mm-chat-turn assistant">
                <div className="mm-chat-avatar-bot"><img src={billsenseLogo} alt="" /></div>
                <button className="mm-pagar-cta" onClick={() => setMostrarPago(true)}>
                  <Wallet size={16}/> Pagar mi recibo
                </button>
              </div>
            )}

            {/* CTA "Explíqueme mi recibo" — misma lógica que la de pago: aparece sola
                cuando el backend la sugirió (`VER_DETALLE` en `acciones`) y lleva a la
                pantalla de recibo ya implementada, no a otro turno de chat. */}
            {puedeVerDetalle && !explicar.isPending && (
              <div className="mm-chat-turn assistant">
                <div className="mm-chat-avatar-bot"><img src={billsenseLogo} alt="" /></div>
                <button className="mm-verdetalle-cta" onClick={() => navTo("recibo")}>
                  <CreditCard size={16}/> Ver el detalle de mi recibo
                </button>
              </div>
            )}

            {/* Feedback */}
            {ultima && !explicar.isPending && !opinion && (
              <div className="mm-chat-feedback">
                <p>¿Te sirvió esta explicación?</p>
                <div className="mm-feedback-btns">
                  <button onClick={() => setOpinion("arriba")} className="mm-feedback-yes">
                    <ThumbsUp size={14}/> Sí, entendí
                  </button>
                  <button onClick={() => { setOpinion("abajo"); preguntar("No entendí, explícamelo más simple"); }} className="mm-feedback-no">
                    <ThumbsDown size={14}/> No comprendo
                  </button>
                </div>
              </div>
            )}

            {/* Oferta */}
            {opinion==="arriba" && oferta && (
              <div className="mm-chat-turn assistant">
                <div className="mm-chat-avatar-bot"><img src={billsenseLogo} alt="" /></div>
                <div className="mm-offer-card">
                  <div className="mm-offer-header"><ShoppingBag size={16}/><span>Oferta exclusiva para ti</span></div>
                  <p className="mm-offer-title">{oferta.etiqueta}</p>
                  <p className="mm-offer-note">Consulta las condiciones antes de contratar.</p>
                </div>
              </div>
            )}

            {/* El aviso de derivación NO se pinta. La derivación ocurre igual y en
                silencio —el caso entra en la cola con su expediente y su context_ref—,
                pero al cliente no se le pone delante: un último recurso que aparece en
                todas las respuestas deja de ser el último. Y el motivo que traía era
                diagnóstico interno («conceptos fuera de catálogo: FRTOCH_003…»), que a
                quien lee su recibo solo le comunica que el sistema no funciona. */}

            {chatError && <p className="mm-chat-error">{chatError}</p>}
            <div ref={fin} />
          </div>

          {/* Input — solo texto, sin micrófono */}
          <form onSubmit={enviar} className="mm-chat-form">
            <input
              aria-label="Consulta a BillSense"
              value={borrador}
              maxLength={2000}
              placeholder="Escribe tu consulta a BillSense…"
              onChange={(e) => setBorrador(e.target.value)}
              className="mm-chat-input"
            />
            <button type="submit" id="btn-enviar-chat"
              disabled={!borrador.trim() || explicar.isPending}
              aria-label="Enviar" className="mm-chat-send">
              <Send size={18}/>
            </button>
          </form>
        </>
      )}

      {/* ════════════════════════════════════════════
          MODO VOZ — interfaz dedicada al audio
          ════════════════════════════════════════════ */}
      {chatModo === "voz" && (
        <div className="mm-voz-container">

          {/* Avatar animado */}
          <div className="mm-voz-avatar-wrap">
            {/* Anillos de onda cuando está activo */}
            {liveIsVisible && <>
              <div className="mm-voz-ring ring1" />
              <div className="mm-voz-ring ring2" />
              <div className="mm-voz-ring ring3" />
            </>}
            <div className={`mm-voz-avatar${liveIsVisible?" active":""}`}>
              <img src={billsenseLogo} alt="BillSense" />
            </div>
          </div>

          {/* Estado de BillSense */}
          <p className={`mm-voz-status${liveIsVisible?" active":""}`}>
            {liveLabel(liveStatus)}
          </p>

          {/* Transcripts de la sesión de voz — una única burbuja verificada por turno,
              no la narración hablada y luego, aparte, la respuesta con cifras. */}
          <div className="mm-voz-transcripts">
            {mensajes.filter(m => m.esVoz).map((m, i) => {
              const esAsi = m.rol === "asistente";
              return esAsi ? (
                <div key={`prev-${i}`} className="mm-voz-bubble agent">
                  <div className="mm-voz-bubble-head">
                    <span className="mm-voz-bubble-who"><Bot size={13}/> BillSense</span>
                    <span className="mm-voz-bubble-tag"><ShieldCheck size={11}/> Verificado</span>
                  </div>
                  <div className="mm-voz-bubble-text">{m.bloques ? <RichMessage bloques={m.bloques} /> : m.texto}</div>
                  {m.beneficiosCierre && (
                    <div className="mm-chat-efervescente">
                      <span className="mm-chat-efervescente-title"><Gift size={13}/> Recuerde que ya cuenta con:</span>
                      <div className="mm-renta-chips">
                        {m.beneficiosCierre.map((b) => <span className="mm-renta-chip" key={b}>{b}</span>)}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div key={`prev-${i}`} className="mm-voz-bubble user">
                  <Mic size={14}/><span>{m.texto}</span>
                </div>
              );
            })}
            {/* Lo que el cliente va diciendo, mientras lo dice */}
            {liveMsgs.map((msg, i) => (
              <div key={`live-${i}`} className="mm-voz-bubble user live">
                <Mic size={14}/><span>{msg.text}</span>
              </div>
            ))}
            <div ref={vozScroll} />
          </div>

          {/* Botón grande de micrófono */}
          <div className="mm-voz-btn-wrap">
            <button
              id="btn-billsense-voz-main"
              className={`mm-voz-btn${liveIsVisible?" active":""}`}
              onClick={() => void toggleLive()}
              disabled={!token || Boolean(micIssue)}
              title={micIssue ?? liveLabel(liveStatus)}
              aria-label={liveIsVisible?"Detener BillSense Voz":"Iniciar BillSense Voz"}
            >
              {liveStatus === "connecting"
                ? <Loader2 size={24} className="animate-spin"/>
                : liveIsVisible
                ? <MicOff size={24}/>
                : <Mic size={24}/>}
            </button>
            <p className="mm-voz-hint">
              {micIssue
                ? <span style={{ color:"#cc4c3d" }}>{micIssue}</span>
                : liveIsVisible
                ? "Toca para detener"
                : "Toca para hablar con BillSense"}
            </p>
          </div>

          {chatError && <p className="mm-chat-error" style={{ margin:"0 20px 16px" }}>{chatError}</p>}
        </div>
      )}
      {mostrarPago && hechos && <PagoModal hechos={hechos} cuentaId={cuenta} onClose={() => setMostrarPago(false)} />}
    </div>
  );
}

// ── Error helper ──────────────────────────────────────────────────────
function err(causa: unknown) {
  return causa instanceof ApiError
    ? `${causa.code}: ${causa.message}`
    : causa instanceof Error
    ? causa.message
    : "Ocurrió un error inesperado";
}
