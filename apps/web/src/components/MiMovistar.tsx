/**
 * MiMovistar — App Mi Movistar completa
 *
 * Flujo de 7 pantallas:
 *   splash → selector → loginDNI/registroDNI → productos? → dashboard → recibo → chat
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
  AlertCircle, ArrowLeft, Bot, CreditCard, Gift, Home as HomeIcon,
  Key, Loader2, MessageSquare, Mic, MicOff, Send, ShieldCheck,
  ShoppingBag, Smartphone, Star, ThumbsDown, ThumbsUp, TrendingDown, TrendingUp,
  User as UserIcon, Phone, Wifi,
} from "lucide-react";
import MovistarLogo from "./MovistarLogo";
import { ReceiptDetailCard } from "./ReceiptDetailCard";
import { api, ApiError } from "../api/client";
import type { Block, Explanation, FactSet } from "../api/types";
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

const narrativa = (bloques: Block[]) =>
  bloques
    .filter((b) => b.tipo === "texto" || b.tipo === "aviso")
    .map((b) => (b as { texto: string }).texto.trim())
    .filter(Boolean)
    // Un espacio, no un salto de párrafo. Cada causa viaja en su propio bloque
    // —para poder señalarla en el recibo guiado— y con el salto el chat pintaba
    // cuatro párrafos donde hay una idea: el mismo texto con el triple de alto y
    // con pinta de informe. Se lee de un vistazo.
    .join(" ");

const liveLabel = (s: LiveStatus): string =>
  ({
    idle:       "Toca para hablar con BillSense",
    connecting: "Conectando con BillSense…",
    listening:  "BillSense te escucha…",
    consulting: "Consultando tu recibo…",
    speaking:   "BillSense está respondiendo…",
    error:      "Error de conexión de voz",
  })[s];

const liveLabelShort = (s: LiveStatus): string =>
  ({ idle:"Voz", connecting:"Conectando…", listening:"Escuchando…", consulting:"Consultando…", speaking:"Respondiendo…", error:"Error" })[s];

// ── Types ─────────────────────────────────────────────────────────────
type TipoDoc  = "DNI" | "CE" | "Pasaporte";
type Pantalla = "splash"|"selector"|"loginDNI"|"registroDNI"|"productos"|"dashboard"|"recibo"|"chat";
type ChatModo = "chat" | "voz";
/** Un mensaje del historial del chat. esRecibo muestra la tarjeta de factura inline. */
type Mensaje  = { rol: "cliente"|"asistente"; texto: string; esVoz?: boolean; esRecibo?: boolean };
interface Producto { id: string; tipo: "movil"|"hogar"; etiqueta: string; numero: string }

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
  const [modoSoloMovil, setModoSoloMovil] = useState(false);
  const [aceptaTC,      setAceptaTC]      = useState(false);
  const [esRegistro,    setEsRegistro]    = useState(false);
  const [bottomTab,     setBottomTab]     = useState("inicio");
  const [chatModo,      setChatModo]      = useState<ChatModo>("chat");

  // ── Auth ────────────────────────────────────────────────────────
  const [tipoDoc,   setTipoDoc]   = useState<TipoDoc>("DNI");
  const [documento, setDocumento] = useState("");
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

  // ── Productos ───────────────────────────────────────────────────
  const [productosSim, setProductosSim] = useState<Producto[]>([]);

  // ── BillSense Voz ───────────────────────────────────────────────
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("idle");
  const [liveMsgs,   setLiveMsgs]   = useState<{role:"user"|"agent"; text:string}[]>([]);
  const liveRef = useRef<GeminiLiveClient | null>(null);
  const [detail]  = useState("CORTO");
  const micIssue  = microphoneSupportError();

  // ── Scroll ──────────────────────────────────────────────────────
  const fin      = useRef<HTMLDivElement | null>(null);
  const vozScroll= useRef<HTMLDivElement | null>(null);

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
  const cuentaElegida = (documento || sugerida).trim();
  const liveActivo    = Boolean(liveRef.current);
  const liveIsVisible = liveActivo || ["connecting","listening","consulting","speaking"].includes(liveStatus);
  const oferta        = ultima?.acciones.find((a) => a.id === "VER_ALTERNATIVAS");
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
  const alDia              = deudaPendienteCent <= 0;
  const vencimiento        = hechos?.fecha_vencimiento ? new Date(hechos.fecha_vencimiento) : null;
  const diasParaVencer     = vencimiento ? Math.ceil((vencimiento.getTime() - Date.now()) / 86_400_000) : null;
  const pctDelta           = hechos && hechos.total_previo_cent > 0
    ? Math.round(Math.abs(hechos.delta_total_cent) / hechos.total_previo_cent * 100)
    : null;

  // ── Mutations ───────────────────────────────────────────────────
  const entrar = useMutation({
    mutationFn: async (id: string) => {
      const emitido = await api.token(id);
      const factset = await api.facts(emitido.access_token);
      return { id, token: emitido.access_token, factset };
    },
    onSuccess: ({ id, token: tk, factset }) => {
      setCuenta(id); setToken(tk); setHechos(factset); setChatError(""); onSesion(id, tk);
      const todas = cuentas.data?.demo ?? [];
      if (todas.length > 1 && pantalla === "registroDNI") {
        setProductosSim(todas.slice(0, 3).map((c, i) => ({
          id: c,
          tipo: (i % 2 === 0 ? "movil" : "hogar") as "movil"|"hogar",
          etiqueta: i % 2 === 0 ? `Línea móvil ${i+1}` : `Movistar Hogar ${i+1}`,
          numero:   i % 2 === 0 ? `+51 9${c.slice(-8)}`  : `Jr. Los Olivos ${100+i}, Lima`,
        })));
        setPantalla("productos");
      } else {
        setPantalla("dashboard");
      }
    },
    onError: (e) => setChatError(err(e)),
  });

  const explicar = useMutation({
    mutationFn: (texto: string) =>
      api.explain(token, { conversation_id: conversacion, cuenta_id: cuenta, verbosidad: detail, utterance: texto }),
    onSuccess: (res) => {
      setConversacion(res.conversation_id);
      setUltima(res); setOpinion(null);
      setMensajes((p) => [...p, { rol: "asistente", texto: narrativa(res.bloques) }]);
      onExplicacion(res);
    },
    onError: (e) => setChatError(err(e)),
  });

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

  const pad = (key: string) => {
    if (key === "⌫") setDocumento((p) => p.slice(0, -1));
    else if (key) setDocumento((p) => p + key);
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
              return [...conPregunta, { rol:"asistente" as const, texto:narrativa(res.bloques), esVoz:true }];
            });
            return [];
          });
          onExplicacion(res);
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
  const generarPDF = () => {
    if (!hechos) return;
    const base = Math.round(hechos.total_actual_cent / 1.18);
    const igv  = hechos.total_actual_cent - base;
    const saldoAnt  = (hechos.deuda_anterior_cent as number|undefined) ?? 0;
    const prorrateo = (hechos.prorrateo_cent      as number|undefined) ?? 0;
    const reconexion= (hechos.reconexion_cent     as number|undefined) ?? 0;
    const html = `<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<title>Factura Movistar ${hechos.periodo_actual}</title>
<style>
  body{font-family:Arial,sans-serif;max-width:580px;margin:0 auto;padding:24px;color:#172033}
  .top{background:#019DF4;color:#fff;padding:20px 24px;border-radius:12px;margin-bottom:16px}
  .top h1{margin:0;font-size:22px} .top p{margin:4px 0 0;font-size:13px;opacity:.8}
  .hero{background:#e4f7fd;border:1.5px solid #b3e0f7;border-radius:12px;padding:20px;text-align:center;margin-bottom:16px}
  .hero .amt{font-size:36px;font-weight:900;color:#019DF4;margin:8px 0}
  .hero .due{color:#cc4c3d;font-size:12px;font-weight:700}
  table{width:100%;border-collapse:collapse;margin-bottom:16px}
  td{padding:10px 8px;border-bottom:1px solid #edf0f4;font-size:13px}
  td:last-child{text-align:right;font-weight:600}
  .tax td{background:#f8fafc;color:#68768a}
  .total td{background:#e4f7fd;font-weight:800;font-size:15px}
  .footer{font-size:11px;color:#9fb1c4;text-align:center;margin-top:20px}
  @media print{.no-print{display:none}}
</style></head><body>
<div class="top"><h1>App Mi Movistar</h1><p>Detalle de Facturación — ${hechos.periodo_actual}</p></div>
<div class="hero">
  <p style="margin:0;font-size:12px;color:#68768a;text-transform:uppercase;letter-spacing:.1em">Total a Pagar</p>
  <p class="amt">${soles(hechos.total_actual_cent)}</p>
  ${hechos.fecha_vencimiento ? `<p class="due">⚠ Vence: ${String(hechos.fecha_vencimiento)}</p>` : ""}
</div>
<table>
  <tr><td>Saldo mes anterior</td><td>${soles(saldoAnt)}</td></tr>
  <tr><td>Servicios del mes actual (base imponible)</td><td>${soles(base)}</td></tr>
  <tr class="tax"><td>IGV 18%</td><td>${soles(igv)}</td></tr>
  ${prorrateo ? `<tr><td>Prorrateo</td><td>${soles(prorrateo)}</td></tr>` : ""}
  ${reconexion ? `<tr><td>Cargo por reconexión</td><td style="color:#cc4c3d">${soles(reconexion)}</td></tr>` : ""}
  <tr class="total"><td>Total a pagar servicio</td><td>${soles(hechos.total_actual_cent)}</td></tr>
</table>
<table>
  <tr><td>Mes anterior</td><td>${soles(hechos.total_previo_cent)}</td></tr>
  <tr><td>Modalidad</td><td>${hechos.modalidad_renta}</td></tr>
</table>
<p class="footer">Movistar Perú · Cuenta ${cuenta} · Generado: ${new Date().toLocaleDateString("es-PE")}</p>
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
          <button className="mm-btn-primary" onClick={() => { setEsRegistro(false); setPantalla("selector"); }}>Iniciar sesión</button>
          <button className="mm-btn-outline" onClick={() => { setEsRegistro(true); setAceptaTC(false); setPantalla("selector"); }}>
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
  // SELECTOR — Vista 1 (dos tarjetas)
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "selector") return (
    <div className={F}>
      {hdr("Iniciar sesión", () => setPantalla("splash"))}
      <div className="mm-selector-body">
        <div className="mm-selector-hero">
          <div className="mm-selector-hero-icon">
            <div className="mm-selector-phone-shadow" />
            <div className="mm-selector-phone-body"><Smartphone size={40} className="text-white" /></div>
          </div>
        </div>
        <h2 className="mm-selector-titulo">Elige tu forma de ingreso</h2>
        <p className="mm-selector-sub">Selecciona cómo deseas gestionar tus servicios Movistar</p>
        <div className="mm-selector-cards">
          <button id="btn-todos-productos" className="mm-selector-card"
            onClick={() => { setModoSoloMovil(false); setDocumento(""); setChatError(""); setPantalla(esRegistro ? "registroDNI" : "loginDNI"); }}>
            <div className="mm-sc-icon" style={{ background:"#e4f7fd" }}><Key size={24} className="text-[#019DF4]" /></div>
            <div className="mm-sc-body">
              <strong>{esRegistro ? "Soy titular" : "Todos mis productos"}</strong>
              <span>Gestiona todos tus productos y beneficios como titular</span>
            </div>
            <span className="mm-sc-arrow">›</span>
          </button>
          <button id="btn-solo-movil" className="mm-selector-card"
            onClick={() => { setModoSoloMovil(true); setDocumento(""); setChatError(""); setPantalla(esRegistro ? "registroDNI" : "loginDNI"); }}>
            <div className="mm-sc-icon" style={{ background:"#edfae1" }}><Smartphone size={24} className="text-[#5BC500]" /></div>
            <div className="mm-sc-body">
              <strong>Solo con mi móvil</strong>
              <span>Podrás ver y gestionar solo una línea móvil</span>
            </div>
            <span className="mm-sc-arrow">›</span>
          </button>
        </div>
      </div>
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // LOGIN DNI (Vista 2) / REGISTRO DNI (Vista 4)
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "loginDNI" || pantalla === "registroDNI") {
    const esReg = pantalla === "registroDNI";
    const puede = cuentaElegida.length > 0 && (!esReg || aceptaTC);
    return (
      <div className={F}>
        {hdr(esReg ? "Registrarme" : "Iniciar sesión",
             () => setPantalla(esReg ? "splash" : "selector"))}
        <div className="mm-dni-body">
          <div className="mm-dni-illustration">
            <div className="mm-dni-ilu-circle"><UserIcon size={38} className="text-[#019DF4]" /></div>
            {esReg && <div className="mm-dni-ilu-badge"><ShieldCheck size={14} className="text-white" /></div>}
          </div>
          <h2 className="mm-dni-titulo">¡Genial!</h2>
          <p className="mm-dni-sub">Solo ingresa tu documento de identidad</p>
          <div className="mm-dni-form">
            <select className="mm-dni-select" value={tipoDoc} onChange={(e) => setTipoDoc(e.target.value as TipoDoc)}>
              <option value="DNI">DNI</option>
              <option value="CE">Carnet Ext.</option>
              <option value="Pasaporte">Pasaporte</option>
            </select>
            <div className="mm-dni-display" aria-label="Número de documento">
              {documento
                ? <span className="mm-dni-number">{documento}</span>
                : <span className="mm-dni-placeholder">{sugerida || "N° de documento"}</span>}
            </div>
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
  // PRODUCTOS
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "productos") return (
    <div className={F}>
      {hdr("Mis Productos", () => setPantalla("registroDNI"))}
      <div className="mm-productos-body">
        <p className="mm-productos-intro">
          Tienes <strong>{productosSim.length} productos</strong> a tu nombre. Selecciona el que deseas gestionar:
        </p>
        <div className="mm-productos-lista">
          {productosSim.map((p) => (
            <button key={p.id} className="mm-producto-item"
              onClick={() => p.id !== cuenta ? entrar.mutate(p.id) : setPantalla("dashboard")}>
              <div className={`mm-producto-icon ${p.tipo}`}>
                {p.tipo === "movil" ? <Smartphone size={22} /> : <HomeIcon size={22} />}
              </div>
              <div className="mm-producto-info"><strong>{p.etiqueta}</strong><span>{p.numero}</span></div>
              <div className="mm-producto-arrow">›</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );

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
          <span>{modoSoloMovil ? "Solo móvil" : hechos?.modalidad_renta ?? "Mi Plan"}</span>
        </button>
      </div>
      <div className="mm-dash-scroll">
        {/* Mi situación — con datos reales del recibo, no genéricos */}
        {hechos && (
          <div className="mm-situacion-card">
            <div className="mm-situacion-row">
              <span className={`mm-situacion-badge ${alDia ? "ok" : "alerta"}`}>
                {alDia ? <ShieldCheck size={13}/> : <AlertCircle size={13}/>}
                {alDia ? "Cuenta al día" : `Deuda anterior: ${soles(deudaPendienteCent)}`}
              </span>
              {diasParaVencer !== null && (
                <span className={`mm-situacion-venc${diasParaVencer <= 3 ? " urgente" : ""}`}>
                  {diasParaVencer < 0
                    ? "Recibo vencido"
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
        {/* Banner */}
        <div className="mm-dash-banner">
          <div>
            <p className="mm-dash-banner-kicker">🎉 Oferta exclusiva</p>
            <p className="mm-dash-banner-text">¡Disfruta de <strong>35 GB</strong> + velocidad máxima!</p>
          </div>
          <button className="mm-dash-banner-btn">Ver</button>
        </div>
        {/* Mejorar plan */}
        <div className="mm-dash-card">
          <div className="mm-dash-card-icon" style={{ background:"#f3eeff", color:"#7c3aed" }}><Star size={22} /></div>
          <div className="mm-dash-card-info">
            <strong>Mejorar mi plan</strong><span>Encuentra el plan perfecto para ti</span>
          </div>
          <button className="mm-dash-card-cta"
            onClick={() => abrirChat("¿Cuáles son los planes disponibles para mejorar mi servicio?")}>
            Ver opciones
          </button>
        </div>
        {/* MI RECIBO */}
        <div className="mm-recibo-card">
          <div className="mm-recibo-card-top">
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
        {/* Consumos */}
        <p className="mm-dash-section-title">Mis consumos</p>
        <div className="mm-consumos">
          {[
            { icon:<Wifi size={18}/>,  label:"Bono datos", val:"35 GB", pct:42,  color:"#019DF4", bg:"#e4f7fd" },
            { icon:<Phone size={18}/>, label:"Minutos",    val:"Ilim.", pct:100, color:"#5BC500", bg:"#edfae1" },
            { icon:<Bot size={18}/>,   label:"BillSense",  val:"Activo",pct:100, color:"#d99b1c", bg:"#fff8e8" },
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
    </div>
  );

  // ════════════════════════════════════════════════════════════════
  // RECIBO — Detalle completo
  // ════════════════════════════════════════════════════════════════
  if (pantalla === "recibo") return (
    <div className={F}>
      {hdr("Mi Recibo", () => setPantalla("dashboard"))}
      <div className="mm-recibo-body">
        <div className="mm-recibo-hero">
          <p className="mm-recibo-period">Período {hechos?.periodo_actual}</p>
          <p className="mm-recibo-amount">{soles(hechos?.total_actual_cent ?? 0)}</p>
          <p className={`mm-recibo-delta ${sube?"up":"down"}`}>
            {sube?"▲":"▼"} {soles(Math.abs(hechos?.delta_total_cent ?? 0))} respecto del mes anterior
          </p>
        </div>
        <div className="mm-recibo-detail">
          {[
            ["Mes anterior", soles(hechos?.total_previo_cent ?? 0)],
            ["Este mes",     soles(hechos?.total_actual_cent ?? 0), true],
            ["Modalidad",    hechos?.modalidad_renta ?? "—"],
            hechos?.prorrateo_cent  ? ["Prorrateo",      soles(Number(hechos.prorrateo_cent))]  : null,
            hechos?.reconexion_cent ? ["Reconexión",     soles(Number(hechos.reconexion_cent))]  : null,
            hechos?.deuda_anterior_cent ? ["⚠️ Deuda ant.", soles(hechos.deuda_anterior_cent as number), false, true] : null,
            hechos?.fecha_vencimiento   ? ["Vencimiento",  String(hechos.fecha_vencimiento)]     : null,
          ].filter((f): f is (string | boolean)[] => Boolean(f)).map(([k,v,bold,danger],i) => (
            <div key={i} className={`mm-recibo-row${danger?" danger":""}`}>
              <span>{k as string}</span>
              <span style={{ fontWeight:bold?700:undefined, color:danger?"#cc4c3d":undefined }}>{v as string}</span>
            </div>
          ))}
        </div>
        {hechos && (
          <div style={{ marginBottom:12 }}>
            <ReceiptDetailCard hechos={hechos} onDownload={generarPDF} />
          </div>
        )}
        <button className="mm-btn-primary" onClick={() => abrirChat("Explícame mi recibo detalladamente")}>
          <Bot size={18} /> Explícame este recibo con BillSense
        </button>
        <button className="mm-btn-outline" style={{ marginTop:12 }} onClick={() => setPantalla("dashboard")}>
          Volver al inicio
        </button>
      </div>
    </div>
  );

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
        <div style={{ width:32, display:"flex", justifyContent:"flex-end" }}>
          <div className={`mm-live-badge${liveIsVisible?" active":""}`}>
            {liveIsVisible ? <><Mic size={11}/> {liveLabelShort(liveStatus)}</> : <><Bot size={11}/> IA</>}
          </div>
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

      {/* ════════════════════════════════════════════
          MODO CHAT — texto sin audio
          ════════════════════════════════════════════ */}
      {chatModo === "chat" && (
        <>
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
                    <ReceiptDetailCard hechos={hechos} onDownload={generarPDF} />
                  </div>
                );
              }
              const esAsi = m.rol === "asistente";
              return (
                <div key={i} className={`mm-chat-turn ${esAsi?"assistant":"client"}`}>
                  {esAsi && (
                    <div className={`mm-chat-avatar-bot${m.esVoz?" voz":""}`}>
                      {m.esVoz ? <Mic size={14}/> : <img src={billsenseLogo} alt="" />}
                    </div>
                  )}
                  <div className={`mm-chat-bubble ${esAsi?"assistant":"client"}`}>{m.texto}</div>
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
                  <span className="mm-voz-bubble-text">{m.texto}</span>
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
