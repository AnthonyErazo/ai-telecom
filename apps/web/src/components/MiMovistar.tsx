import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft, Bot, CreditCard, Loader2, Send, ShieldCheck,
  ShoppingBag, Smartphone, ThumbsDown, ThumbsUp, User as UserIcon,
} from "lucide-react";
import MovistarLogo from "./MovistarLogo";
import { api, ApiError } from "../api/client";
import type { Block, Explanation, FactSet } from "../api/types";

/**
 * App Mi Movistar: la pantalla que ve el cliente.
 *
 * De dónde sale esta interfaz
 * ---------------------------
 * El diseño viene del prototipo `brainy-bill` —marco de móvil, marca Movistar, acceso
 * por documento, chat con preguntas sugeridas y feedback— porque es el que se parece a la
 * aplicación real. Lo que **no** se trajo es su origen de datos: allí los importes salían
 * de `mock_data.py`, inventados. Aquí cada cifra viene de `POST /v1/explicar`, calculada
 * por el motor y comprobada por el verificador numérico antes de pintarse.
 *
 * Esa es toda la diferencia, y es la del proyecto entero: la misma pantalla, sostenida.
 *
 * Las tres decisiones de producto que se conservan del prototipo
 * -------------------------------------------------------------
 * 1. **Feedback 👍/👎 antes de ofrecer nada.** El cross-selling no aparece por tiempo ni
 *    por scroll: aparece si el cliente dice que entendió. Y el 👎 no abre una encuesta:
 *    vuelve a preguntar, más simple. Lo que el prototipo abría ahí era el paso a un
 *    asesor, y eso se quitó —quien no entiende algo quiere entenderlo, no que lo pasen
 *    con otra persona; si prefiere hablar con alguien, lo escribe y el motor lo detecta.
 * 2. **La oferta no lleva importes.** Es una acción (`VER_ALTERNATIVAS`) que el backend
 *    autoriza con doble condición; si trajera cifras tendrían que pasar el verificador y
 *    no están en el FactSet.
 * 3. **El hand-off no se anuncia.** La derivación ocurre por debajo, con el contexto ya
 *    cargado, y el asesor recibe el expediente; al cliente no se le pone delante. Un
 *    último recurso que aparece en todas las respuestas deja de ser el último.
 */

const soles = (centimos: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(centimos / 100);

/** El texto que se lee en el chat: los bloques narrativos, sin el andamiaje de pantalla. */
function narrativa(bloques: Block[]): string {
  return bloques
    .filter((b) => b.tipo === "texto" || b.tipo === "aviso")
    .map((b) => (b as { texto: string }).texto.trim())
    .filter(Boolean)
    // Un espacio, no un salto de párrafo. Desde que cada causa viaja en su propio bloque
    // —para poder señalarla en el recibo— el chat pintaba cuatro párrafos donde antes
    // había uno: el mismo texto, con el triple de alto en pantalla y con pinta de
    // informe. Aquí se lee de un vistazo; la separación por bloques sigue existiendo
    // donde sirve, que es la vista guiada.
    .join(" ");
}

type Pantalla = "acceso" | "recibo" | "chat";
type Mensaje = { rol: "cliente" | "asistente"; texto: string };

export function MiMovistar({
  onSesion,
  onExplicacion,
}: {
  onSesion: (cuenta: string, token: string) => void;
  onExplicacion: (explicacion: Explanation) => void;
}) {
  const [pantalla, setPantalla] = useState<Pantalla>("acceso");
  const [documento, setDocumento] = useState("");
  const [cuenta, setCuenta] = useState("");
  const [token, setToken] = useState("");
  const [hechos, setHechos] = useState<FactSet | null>(null);
  const [mensajes, setMensajes] = useState<Mensaje[]>([]);
  const [borrador, setBorrador] = useState("");
  const [conversacion, setConversacion] = useState<string | undefined>();
  const [ultima, setUltima] = useState<Explanation | null>(null);
  const [opinion, setOpinion] = useState<"arriba" | "abajo" | null>(null);
  const [error, setError] = useState("");
  const fin = useRef<HTMLDivElement | null>(null);

  const cuentas = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const sugerida = cuentas.data?.demo?.[0] ?? "";
  // El documento se precarga con una cuenta que la API declara servible: la demo se hace
  // de un clic y, sobre todo, nunca ofrece una cuenta que el motor no pueda explicar.
  useEffect(() => { if (!documento && sugerida) setDocumento(sugerida); }, [sugerida, documento]);
  // Un SOLO valor para el botón y para el envío. Calcularlos por separado deja el botón
  // deshabilitado —o peor, habilitado y mudo— mientras `/dev/cuentas` está en vuelo.
  const cuentaElegida = (documento || sugerida).trim();
  useEffect(() => { fin.current?.scrollIntoView?.({ behavior: "smooth", block: "end" }); },
    [mensajes, opinion]);

  const entrar = useMutation({
    mutationFn: async (id: string) => {
      const emitido = await api.token(id);
      const factset = await api.facts(emitido.access_token);
      return { id, token: emitido.access_token, factset };
    },
    onSuccess: ({ id, token: emitido, factset }) => {
      setCuenta(id); setToken(emitido); setHechos(factset); setError("");
      setPantalla("recibo");
      onSesion(id, emitido);
    },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const explicar = useMutation({
    mutationFn: (texto: string) => api.explain(token, {
      conversation_id: conversacion, cuenta_id: cuenta, verbosidad: "CORTO", utterance: texto,
    }),
    onSuccess: (respuesta) => {
      setConversacion(respuesta.conversation_id);
      setUltima(respuesta);
      setOpinion(null);
      setMensajes((previos) => [...previos, { rol: "asistente", texto: narrativa(respuesta.bloques) }]);
      onExplicacion(respuesta);
    },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const preguntar = (texto: string) => {
    const limpio = texto.trim();
    if (!limpio || explicar.isPending) return;
    setMensajes((previos) => [...previos, { rol: "cliente", texto: limpio }]);
    setBorrador("");
    explicar.mutate(limpio);
  };

  const enviar = (evento: FormEvent) => { evento.preventDefault(); preguntar(borrador); };

  // `derivacion` sigue llegando en la respuesta y la pantalla NO la lee: la derivación
  // es un asunto interno entre el motor y la cola del 104. Ver el campo y no pintarlo es
  // la decisión, no un olvido.
  // La oferta solo existe si el backend la autorizó: doble condición (consulta resuelta y
  // regla de negocio explícita). La pantalla no decide vender.
  const oferta = ultima?.acciones.find((a) => a.id === "VER_ALTERNATIVAS");

  const marco = "flex flex-col max-w-md mx-auto w-full bg-gray-50 min-h-[720px] shadow-2xl border-x border-gray-100 overflow-hidden";
  const cabecera = (titulo: string, atras?: () => void) => (
    <div className="bg-white px-5 pt-6 pb-4 flex items-center justify-between border-b border-gray-100">
      {atras
        ? <button onClick={atras} className="p-2 -ml-2 rounded-full hover:bg-gray-100 text-gray-600" aria-label="Volver"><ArrowLeft size={20} /></button>
        : <div className="w-8" />}
      <div className="flex items-center gap-2">
        <MovistarLogo className="h-6 w-auto" />
        {/* Encabezado de verdad, no un `span` con aspecto de título: es el nombre de la
            pantalla, y un lector de pantalla tiene que poder saltar a él. */}
        <h1 className="font-bold text-lg text-gray-800 tracking-tight m-0">{titulo}</h1>
      </div>
      <div className="w-8" />
    </div>
  );

  // ------------------------------------------------------------------ acceso
  if (pantalla === "acceso") {
    return <div className={marco}>
      {cabecera("Mi Movistar")}
      <form className="flex-1 flex flex-col px-6 pt-8 pb-10 justify-between" onSubmit={(e) => { e.preventDefault(); if (cuentaElegida) entrar.mutate(cuentaElegida); }}>
        <div>
          <div className="text-center space-y-4">
            <div className="bg-sky-50 w-24 h-24 rounded-full flex items-center justify-center mx-auto shadow-inner">
              <MovistarLogo className="h-12 w-auto" />
            </div>
            <h2 className="text-2xl font-bold text-gray-800">¡Bienvenido a Mi Movistar!</h2>
            <p className="text-sm text-gray-500 max-w-xs mx-auto">
              Consulta tu recibo y resuelve tus dudas al instante.
            </p>
          </div>
          <div className="mt-8 space-y-2">
            <label htmlFor="documento" className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
              Número de cuenta
            </label>
            <input id="documento" list="cuentas-servibles" value={documento} autoFocus
                   onChange={(e) => setDocumento(e.target.value)}
                   className="w-full border border-gray-200 rounded-2xl px-4 py-3.5 text-gray-800 focus:outline-none focus:ring-2 focus:ring-[#019DF4]" />
            <datalist id="cuentas-servibles">
              {(cuentas.data?.demo ?? []).map((id) => <option key={id} value={id} />)}
            </datalist>
            <p className="flex items-center gap-1.5 text-[11px] text-gray-400 pt-1">
              <ShieldCheck size={13} className="text-[#5BC500]" />
              Acceso de demostración con <code>/dev/token</code>. No solicita contraseña.
            </p>
            {error && <p className="text-xs text-rose-600">{error}</p>}
          </div>
        </div>
        <div className="space-y-4 mt-8">
          <button type="submit" disabled={!cuentaElegida || entrar.isPending}
                  className="w-full bg-[#019DF4] hover:bg-[#0089d8] disabled:opacity-60 text-white font-semibold py-4 rounded-2xl shadow-md transition-all active:scale-[0.99]">
            {entrar.isPending ? "Validando…" : "Iniciar sesión"}
          </button>
          <div className="grid grid-cols-3 gap-3 text-center pt-6 border-t border-gray-100">
            <div className="p-3 bg-white rounded-xl shadow-sm border border-gray-100 flex flex-col items-center">
              <CreditCard size={20} className="text-[#019DF4] mb-1" />
              <span className="text-[11px] font-medium text-gray-600">Ver Recibos</span>
            </div>
            <div className="p-3 bg-white rounded-xl shadow-sm border border-gray-100 flex flex-col items-center">
              <Smartphone size={20} className="text-[#019DF4] mb-1" />
              <span className="text-[11px] font-medium text-gray-600">Mi Plan</span>
            </div>
            <div className="p-3 bg-white rounded-xl shadow-sm border border-gray-100 flex flex-col items-center">
              <ShieldCheck size={20} className="text-[#019DF4] mb-1" />
              <span className="text-[11px] font-medium text-gray-600">Soporte</span>
            </div>
          </div>
        </div>
      </form>
    </div>;
  }

  // ------------------------------------------------------------------ recibo
  if (pantalla === "recibo") {
    const sube = (hechos?.delta_total_cent ?? 0) >= 0;
    return <div className={marco}>
      {cabecera("Mi Recibo", () => setPantalla("acceso"))}
      <div className="flex-1 overflow-y-auto p-5 space-y-4 chat-scroll">
        <div className="bg-white rounded-2xl shadow-card border border-gray-100 p-5">
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Periodo {hechos?.periodo_actual}</p>
          <p className="text-4xl font-bold text-gray-800 mt-1">{soles(hechos?.total_actual_cent ?? 0)}</p>
          <p className={`text-sm font-semibold mt-2 ${sube ? "text-rose-600" : "text-emerald-600"}`}>
            {sube ? "▲" : "▼"} {soles(Math.abs(hechos?.delta_total_cent ?? 0))} respecto del mes anterior
          </p>
        </div>
        <div className="bg-white rounded-2xl shadow-card border border-gray-100 p-5 space-y-3">
          <div className="flex justify-between text-sm"><span className="text-gray-500">Mes anterior</span><span className="font-semibold text-gray-800">{soles(hechos?.total_previo_cent ?? 0)}</span></div>
          <div className="flex justify-between text-sm"><span className="text-gray-500">Este mes</span><span className="font-semibold text-gray-800">{soles(hechos?.total_actual_cent ?? 0)}</span></div>
          <div className="flex justify-between text-sm border-t border-gray-100 pt-3"><span className="text-gray-500">Modalidad</span><span className="font-semibold text-gray-800">{hechos?.modalidad_renta}</span></div>
        </div>
        <button onClick={() => { setPantalla("chat"); if (!mensajes.length) preguntar("¿Por qué me vino más caro este mes?"); }}
                className="w-full bg-[#019DF4] hover:bg-[#0089d8] text-white font-semibold py-4 rounded-2xl shadow-md flex items-center justify-center gap-2 active:scale-[0.99]">
          <Bot size={18} /> Explícame este recibo
        </button>
      </div>
    </div>;
  }

  // ------------------------------------------------------------------ chat
  return <div className={marco}>
    {cabecera("Asistente", () => setPantalla("recibo"))}
    <div className="flex-1 overflow-y-auto p-4 space-y-4 chat-scroll">
      {mensajes.map((m, i) => {
        const esAsistente = m.rol === "asistente";
        return <div key={i} className={`flex items-start gap-2.5 ${esAsistente ? "justify-start" : "justify-end"}`}>
          {esAsistente && <div className="w-8 h-8 rounded-full bg-[#019DF4] text-white flex items-center justify-center shrink-0 shadow-sm mt-0.5"><Bot size={18} /></div>}
          <div className={`max-w-[85%] p-4 text-sm leading-relaxed whitespace-pre-wrap ${esAsistente
            ? "bg-white text-gray-800 rounded-2xl rounded-tl-sm shadow-md border border-gray-100"
            : "bg-[#019DF4] text-white font-medium rounded-2xl rounded-tr-sm shadow-md"}`}>{m.texto}</div>
          {!esAsistente && <div className="w-8 h-8 rounded-full bg-gray-200 text-gray-600 flex items-center justify-center shrink-0 shadow-sm mt-0.5"><UserIcon size={18} /></div>}
        </div>;
      })}

      {explicar.isPending && <div className="flex items-center gap-2 text-gray-400 text-xs pl-10">
        <Loader2 size={16} className="animate-spin text-[#019DF4]" />
        <span>Consultando su recibo y verificando cada cifra…</span>
      </div>}

      {mensajes.length === 1 && !explicar.isPending && <div className="pl-10 space-y-2 pt-1">
        <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">Preguntas frecuentes:</p>
        {["¿Por qué subió el monto de este mes?", "¿El próximo mes me cobrarán lo mismo?"].map((p) => (
          <button key={p} onClick={() => preguntar(p)}
                  className="block text-xs font-semibold text-[#019DF4] bg-sky-50 hover:bg-sky-100 px-3.5 py-2 rounded-xl text-left border border-sky-100">{p}</button>
        ))}
      </div>}

      {/* El feedback abre las dos ramas del producto: la oferta o la persona. */}
      {ultima && !explicar.isPending && !opinion && <div className="pl-10 pt-2">
        <div className="bg-white rounded-2xl p-3.5 shadow-md border border-gray-100 space-y-2">
          <p className="text-xs font-semibold text-gray-600">¿Te sirvió esta explicación?</p>
          <div className="flex gap-2">
            <button onClick={() => setOpinion("arriba")} className="flex-1 py-2 px-3 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 font-semibold rounded-xl text-xs flex items-center justify-center gap-1.5 border border-emerald-100"><ThumbsUp size={14} /> Sí, entendí</button>
            <button onClick={() => { setOpinion("abajo"); preguntar("No entendí, explícamelo más simple"); }}
                    className="flex-1 py-2 px-3 bg-rose-50 hover:bg-rose-100 text-rose-700 font-semibold rounded-xl text-xs flex items-center justify-center gap-1.5 border border-rose-100"><ThumbsDown size={14} /> No comprendo</button>
          </div>
        </div>
      </div>}

      {opinion === "arriba" && oferta && <div className="pl-10 pt-2">
        <div className="bg-gradient-to-br from-sky-50 to-blue-50 rounded-2xl p-4 shadow-md border border-sky-200 space-y-2">
          <div className="flex items-center gap-2 text-[#019DF4]"><ShoppingBag size={18} /><span className="text-xs font-bold uppercase tracking-wider">Oferta exclusiva</span></div>
          <h4 className="font-bold text-gray-800 text-sm">{oferta.etiqueta}</h4>
          <p className="text-xs text-gray-600">Consulte las condiciones antes de contratar.</p>
        </div>
      </div>}

      {/* El aviso de derivación NO se pinta. Dos motivos: al cliente no se le ofrece un
          asesor salvo que lo pida —es el último recurso—, y el motivo que traía era
          diagnóstico interno («hay conceptos fuera de catálogo: FRTOCH_003…»), que a
          quien lee su recibo solo le comunica que el sistema no funciona. La derivación
          sigue ocurriendo por debajo y el caso sigue llegando a la cola del 104. */}

      {error && <p className="text-xs text-rose-600 pl-10">{error}</p>}
      <div ref={fin} />
    </div>

    <form onSubmit={enviar} className="border-t border-gray-100 bg-white p-3 flex items-center gap-2">
      <input aria-label="Consulta" value={borrador} maxLength={2000} placeholder="Escribe tu consulta sobre el recibo…"
             onChange={(e) => setBorrador(e.target.value)}
             className="flex-1 bg-gray-50 border border-gray-200 rounded-2xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-[#019DF4]" />
      <button disabled={!borrador.trim() || explicar.isPending} aria-label="Enviar"
              className="w-11 h-11 rounded-full bg-[#019DF4] text-white flex items-center justify-center disabled:opacity-50 shadow-md">
        <Send size={18} />
      </button>
    </form>
  </div>;
}

function mensajeDeError(causa: unknown) {
  return causa instanceof ApiError ? `${causa.code}: ${causa.message}`
    : causa instanceof Error ? causa.message : "Ocurrió un error inesperado";
}
