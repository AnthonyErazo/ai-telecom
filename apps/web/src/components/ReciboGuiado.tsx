import React, { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronRight, Loader2, Pause, Play, Send, Sparkles, Volume2, VolumeX } from "lucide-react";
import MovistarLogo from "./MovistarLogo";
import { api, ApiError } from "../api/client";
import type { Block, Explanation, FactSet, LineaFactSet } from "../api/types";

/**
 * Recibo guiado: el recibo a la vista, señalado mientras se explica.
 *
 * La idea
 * -------
 * En Mi Movistar la explicación y el recibo son dos pantallas: el cliente lee «le subió
 * S/ 12,30 por la reactivación», vuelve al recibo y tiene que **buscar** de qué línea le
 * hablaban. Ese salto es donde se pierde la explicación. Aquí no hay salto: el recibo se
 * queda delante y cada frase enciende la línea de la que habla.
 *
 * Por qué se puede hacer sin inventar nada
 * ----------------------------------------
 * Porque la respuesta ya venía anclada. Cada bloque narrativo lleva sus `fact_ids`, y
 * desde el motor cada causa viaja en su propio bloque con la referencia de su línea
 * (`linea:<concepto_id>.delta_cent`). Esta pantalla **no interpreta el texto**: no busca
 * palabras ni adivina a qué se refiere una frase. Lee el ancla que el motor ya puso.
 *
 * Eso importa más de lo que parece: señalar la línea equivocada sería una mentira nueva,
 * de las que el verificador numérico no caza porque no hay ninguna cifra inventada. Al
 * atarlo al ancla, la pantalla no puede señalar algo que la explicación no dijo.
 *
 * Lo que se ve
 * ------------
 * Los pasos avanzan solos —se lee como una animación, no como un formulario— y se pueden
 * pausar o saltar. La línea señalada se resalta y las demás bajan de opacidad: el ojo va
 * donde va la frase. Con la voz activada, además se lee en alto y el paso avanza al
 * terminar la locución, no por reloj.
 *
 * El recibo sigue la estructura del recibo real de Movistar Perú —información general con
 * el total a pagar y el último día de pago, datos del ciclo, resumen del recibo y la deuda
 * pasada como línea propia—, para que quien lo tenga en la app reconozca lo que ve.
 */

const soles = (centimos: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(centimos / 100);

/** «2026-04-17» se lee como una clave de sistema; «17 de abril de 2026», como una fecha. */
const fecha = (iso: string) =>
  new Intl.DateTimeFormat("es-PE", { day: "numeric", month: "long", year: "numeric" })
    .format(new Date(`${iso}T00:00:00`));

/** Un paso de la explicación: la frase y qué hay que señalar mientras se lee. */
type Paso = {
  texto: string;
  /** Conceptos del recibo que enciende este paso. Vacío = habla del total. */
  conceptos: string[];
  /** El paso habla del total del recibo, no de una línea concreta. */
  total: boolean;
};

/** Milisegundos que se queda cada paso antes de pasar al siguiente. */
const MS_POR_PASO = 3_200;

/**
 * Convierte los bloques de la respuesta en pasos señalables.
 *
 * Solo mira `fact_ids`, nunca el texto. Un ancla `linea:FRTOCH_003.delta_cent` señala esa
 * línea; una `factset:total_actual_cent` o `factset:delta_total_cent` señala el total.
 * Un bloque sin anclas se lee igual, sin señalar nada: es preferible a señalar a bulto.
 */
export function pasosDeExplicacion(bloques: Block[]): Paso[] {
  const pasos: Paso[] = [];
  for (const bloque of bloques) {
    if (bloque.tipo !== "texto" && bloque.tipo !== "aviso") continue;
    const texto = bloque.texto?.trim();
    if (!texto) continue;
    const anclas = bloque.fact_ids ?? [];
    const conceptos = anclas
      .map((ancla) => /^linea:(.+?)\./.exec(ancla)?.[1])
      .filter((concepto): concepto is string => Boolean(concepto));
    pasos.push({
      texto,
      conceptos,
      total: conceptos.length === 0 && anclas.some((a) => a.startsWith("factset:total")),
    });
  }
  return pasos;
}

type Pantalla = "acceso" | "recibo";

/** Envuelve la pantalla en el marco del teléfono. Ver el tamaño real es parte del diseño. */
function Telefono({ children }: { children: React.ReactNode }) {
  return (
    <div className="telefono">
      <div className="telefono-pantalla">
        <div className="telefono-muesca" />
        {children}
      </div>
    </div>
  );
}

export function ReciboGuiado({ onSesion }: { onSesion?: (cuenta: string, token: string) => void }) {
  const [pantalla, setPantalla] = useState<Pantalla>("acceso");
  const [documento, setDocumento] = useState("");
  const [cuenta, setCuenta] = useState("");
  const [token, setToken] = useState("");
  const [hechos, setHechos] = useState<FactSet | null>(null);
  const [pasos, setPasos] = useState<Paso[]>([]);
  const [indice, setIndice] = useState(0);
  const [reproduciendo, setReproduciendo] = useState(true);
  const [voz, setVoz] = useState(false);
  const [borrador, setBorrador] = useState("");
  const [conversacion, setConversacion] = useState<string | undefined>();
  const [error, setError] = useState("");
  const finPasos = useRef<HTMLDivElement | null>(null);
  const lineaEncendida = useRef<HTMLLIElement | null>(null);

  const cuentas = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const sugerida = cuentas.data?.demo?.[0] ?? "";
  useEffect(() => { if (!documento && sugerida) setDocumento(sugerida); }, [sugerida, documento]);
  const cuentaElegida = (documento || sugerida).trim();
  // `guion` mapea cuenta -> caso del reto que ejemplifica. Sale de qué grupos de cargo
  // tiene la cuenta en su último ciclo, así que no es una etiqueta puesta a mano.
  const casos = Object.entries(cuentas.data?.guion ?? {});
  const casoActual = cuentas.data?.guion?.[cuenta] ?? "";

  const entrar = useMutation({
    mutationFn: async (id: string) => {
      const emitido = await api.token(id);
      const factset = await api.facts(emitido.access_token);
      return { id, token: emitido.access_token, factset };
    },
    onSuccess: ({ id, token: emitido, factset }) => {
      setCuenta(id); setToken(emitido); setHechos(factset); setError("");
      setPantalla("recibo");
      onSesion?.(id, emitido);
    },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const explicar = useMutation({
    mutationFn: (texto: string) => api.explain(token, {
      conversation_id: conversacion, cuenta_id: cuenta, verbosidad: "CORTO", utterance: texto,
    }),
    onSuccess: (respuesta: Explanation) => {
      setConversacion(respuesta.conversation_id);
      setPasos(pasosDeExplicacion(respuesta.bloques));
      setIndice(0);
      setReproduciendo(true);
    },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const paso = pasos[indice];
  // Los conceptos encendidos ahora mismo. Se calculan aquí y no dentro del map de líneas
  // para que la decisión de qué se señala esté en un solo sitio y sea legible.
  const encendidos = useMemo(() => new Set(paso?.conceptos ?? []), [paso]);
  const lineas: LineaFactSet[] = (hechos?.lineas ?? []).filter((l) => l.delta_cent !== 0);

  // Avance automático por reloj. No corre cuando habla la voz: ahí manda el final de la
  // locución, que es una señal de verdad y no una estimación de cuánto se tarda en leer.
  useEffect(() => {
    if (!reproduciendo || voz || indice >= pasos.length - 1) return;
    const reloj = window.setTimeout(() => setIndice((previo) => previo + 1), MS_POR_PASO);
    return () => window.clearTimeout(reloj);
  }, [reproduciendo, voz, indice, pasos.length]);

  // Voz.
  //
  // Se lee con `speechSynthesis`, del propio navegador, y NO pasando el texto por un
  // modelo de voz. La razón no es el coste: es que esta pantalla existe para señalar en el
  // recibo la frase exacta que el motor verificó y ancló. Un modelo que reformula al
  // hablar rompe las dos cosas a la vez —diría algo que no pasó por el verificador, y lo
  // diría mientras se señala una línea que ya no le corresponde—. Aquí se pronuncia
  // carácter por carácter lo mismo que está escrito.
  //
  // De regalo, la sincronía sale gratis y es mejor: el paso avanza cuando **termina** la
  // locución, no cuando se cumple un temporizador. Una frase larga se queda encendida más
  // rato porque tarda más en leerse, que es justo lo que uno esperaría.
  useEffect(() => {
    const sintesis = window.speechSynthesis;
    if (!voz || !reproduciendo || !paso || !sintesis) return;
    sintesis.cancel();
    const avanzar = () =>
      setIndice((previo) => (previo < pasos.length - 1 ? previo + 1 : previo));
    const locucion = new SpeechSynthesisUtterance(paso.texto);
    locucion.lang = "es-PE";
    locucion.onend = avanzar;
    // Sin esto, un equipo sin voces en español deja la explicación congelada en el primer
    // paso: `speak` no falla de forma visible, simplemente `onend` no llega nunca y el
    // reloj está desactivado porque «hay voz». Un fallo de audio no puede llevarse por
    // delante la explicación escrita, que es la que de verdad importa.
    locucion.onerror = avanzar;
    // Y el cinturón: si el motor de voz ni siquiera dispara `onerror` —pasa en algunos
    // navegadores cuando no hay ninguna voz instalada—, se avanza igual por tiempo.
    const rescate = window.setTimeout(avanzar, MS_POR_PASO + paso.texto.length * 60);
    sintesis.speak(locucion);
    return () => { window.clearTimeout(rescate); sintesis.cancel(); };
  }, [voz, reproduciendo, paso, pasos.length]);

  // Al salir de la pantalla se calla. Sin esto la voz sigue leyendo un recibo que ya no
  // está delante, que es de las cosas más desconcertantes que puede hacer una interfaz.
  useEffect(() => () => window.speechSynthesis?.cancel(), []);

  // En un móvil el recibo no cabe entero, así que resaltar una línea que está fuera de la
  // pantalla es no resaltar nada: se la trae a la vista antes de que se lea la frase.
  useEffect(() => {
    lineaEncendida.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, [indice]);

  const preguntar = (texto: string) => {
    const limpio = texto.trim();
    if (!limpio || explicar.isPending) return;
    window.speechSynthesis?.cancel();
    setPasos([]); setIndice(0); setBorrador("");
    explicar.mutate(limpio);
  };

  // ------------------------------------------------------------------ acceso
  //
  // No es un formulario de login, es un elector de casos. Un número de cuenta no le dice
  // nada a nadie —«604511549» no anuncia que ahí hay una reconexión—, y el reto pide
  // enseñar cinco situaciones distintas. La API ya devuelve qué caso ejemplifica cada
  // cuenta, así que elegir cuenta es elegir qué se quiere ver explicado.
  if (pantalla === "acceso") {
    return (
      <Telefono>
        <div className="flex-1 overflow-y-auto chat-scroll px-5 pt-10 pb-6 space-y-5">
          <div className="text-center space-y-3">
            <div className="bg-sky-50 w-20 h-20 rounded-full flex items-center justify-center mx-auto">
              <MovistarLogo className="h-10 w-auto" />
            </div>
            <h1 className="text-xl font-bold text-gray-800 m-0">Recibo guiado</h1>
            <p className="text-sm text-gray-500 m-0">
              Le señalamos en el recibo lo que le vamos contando.
            </p>
          </div>

          {casos.length > 0 && (
            <ul className="space-y-2 m-0 p-0 list-none">
              {casos.map(([id, caso]) => (
                <li key={id}>
                  <button
                    onClick={() => entrar.mutate(id)}
                    disabled={entrar.isPending}
                    className="w-full text-left bg-white border border-gray-200 rounded-2xl px-4 py-3.5 hover:border-[#019DF4] disabled:opacity-50 flex items-center gap-3"
                  >
                    <span className="flex-1 flex flex-col min-w-0">
                      <span className="text-sm font-semibold text-gray-800">{caso}</span>
                      <span className="text-xs text-gray-400 mt-0.5">Cuenta {id}</span>
                    </span>
                    {entrar.isPending && entrar.variables === id
                      ? <Loader2 size={16} className="animate-spin text-[#019DF4]" />
                      : <ChevronRight size={18} className="text-gray-300" />}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <details className="bg-white border border-gray-200 rounded-2xl px-4 py-3">
            <summary className="text-sm text-gray-600 cursor-pointer">Usar otra cuenta</summary>
            <form
              className="flex gap-2 mt-3"
              onSubmit={(e) => { e.preventDefault(); if (cuentaElegida) entrar.mutate(cuentaElegida); }}
            >
              <input
                id="cuenta-guiada" list="cuentas-guiadas" value={documento}
                onChange={(e) => setDocumento(e.target.value)}
                className="flex-1 border-2 border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:border-[#019DF4] outline-none min-w-0"
              />
              <datalist id="cuentas-guiadas">
                {(cuentas.data?.demo ?? []).map((id) => <option key={id} value={id} />)}
              </datalist>
              <button
                type="submit" disabled={!cuentaElegida || entrar.isPending}
                className="bg-[#019DF4] disabled:opacity-50 text-white font-semibold px-5 rounded-xl text-sm"
              >
                Entrar
              </button>
            </form>
          </details>
          {error && <p className="text-sm text-rose-600 text-center">{error}</p>}
        </div>
      </Telefono>
    );
  }

  // ------------------------------------------------------------------ recibo guiado
  const sube = (hechos?.delta_total_cent ?? 0) >= 0;
  // El recibo real cobra el mes y arrastra lo que quedó debiendo, y lo enseña sumado: si
  // se pinta solo el importe del mes, el cliente compara con lo que le cobran de verdad y
  // no le cuadra. La deuda anterior va además como línea propia, abajo.
  const deuda = hechos?.deuda_anterior_cent ?? 0;
  const totalAPagar = (hechos?.total_actual_cent ?? 0) + deuda;
  return (
    // Una sola columna, siempre, y alta fija: el recibo se desplaza por dentro y el
    // narrador se queda pegado abajo. Las dos columnas de antes obligaban a partir la
    // atención entre dos sitios, y en un móvil directamente no caben: la explicación
    // acababa debajo del recibo, fuera de pantalla, justo cuando señala una línea.
    <Telefono>
      <div className="px-4 py-3 flex items-center gap-2 border-b border-gray-100 shrink-0">
        <button onClick={() => { window.speechSynthesis?.cancel(); setPantalla("acceso"); }}
                className="p-2 -ml-2 rounded-full hover:bg-gray-100 text-gray-600" aria-label="Volver a los casos">
          <ArrowLeft size={20} />
        </button>
        <MovistarLogo className="h-5 w-auto shrink-0" />
        <span className="min-w-0 flex flex-col">
          <span className="font-bold text-sm text-gray-800 truncate">
            {casoActual || "Recibo guiado"}
          </span>
          <span className="text-[11px] text-gray-400">Cuenta {cuenta}</span>
        </span>
      </div>

      {/* Lo único que se desplaza. El narrador de abajo no se mueve nunca, así que la
          frase que se está leyendo siempre está a la vista aunque el recibo sea largo. */}
      <div className="flex-1 overflow-y-auto chat-scroll">
        <section>
          {/* «Información general del recibo», la primera sección del recibo real: total
              a pagar, último día de pago y la cuenta. Es lo que el cliente busca primero,
              así que va arriba y en grande, como en la app. */}
          <div className="p-5 border-b border-gray-100">
            <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Total a pagar</p>
            <p
              className={`text-4xl font-bold mt-1 rounded-xl px-2 -mx-2 transition-all duration-500 ${
                paso?.total ? "bg-amber-100 text-amber-900 ring-2 ring-amber-400" : "text-gray-800"
              }`}
            >
              {soles(totalAPagar)}
            </p>
            <p className={`text-sm font-semibold mt-2 ${sube ? "text-rose-600" : "text-emerald-600"}`}>
              {sube ? "▲" : "▼"} {soles(Math.abs(hechos?.delta_total_cent ?? 0))} respecto del mes anterior
            </p>
            {hechos?.fecha_vencimiento && (
              <p className="text-xs text-gray-500 mt-3 m-0">
                Último día de pago: <strong className="text-gray-700">{fecha(hechos.fecha_vencimiento)}</strong>
              </p>
            )}
          </div>

          {/* «Información del ciclo de facturación». La modalidad de renta se enseña
              porque no es un tecnicismo interno: es la que decide si el mes que está
              pagando es el que ya usó o el que va a usar. */}
          <div className="px-5 py-3 bg-gray-50 border-b border-gray-100 flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500">
            <span>Periodo <strong className="text-gray-700">{hechos?.periodo_actual}</strong></span>
            <span>Ciclo de <strong className="text-gray-700">{hechos?.dias_ciclo ?? 30} días</strong></span>
            <span>Renta <strong className="text-gray-700">{(hechos?.modalidad_renta ?? "").toLowerCase()}</strong></span>
          </div>

          <p className="px-5 pt-4 pb-1 text-xs font-semibold text-gray-400 uppercase tracking-wider m-0">
            Resumen del recibo
          </p>
          <ul className="divide-y divide-gray-100 m-0 p-0 list-none">
            {lineas.map((linea) => {
              const encendida = encendidos.has(linea.concepto_id);
              // Cuando hay algo señalado, lo demás baja de opacidad. Sin nada señalado
              // el recibo se ve entero y normal: no se castiga la lectura libre.
              const apagada = encendidos.size > 0 && !encendida;
              return (
                <li
                  key={linea.concepto_id}
                  ref={encendida ? lineaEncendida : undefined}
                  className={`flex items-center justify-between gap-3 px-5 py-3 transition-all duration-500 ${
                    encendida ? "bg-amber-50 ring-2 ring-inset ring-amber-400" : ""
                  } ${apagada ? "opacity-35" : "opacity-100"}`}
                >
                  <span className="text-sm text-gray-700">{linea.nombre_comercial}</span>
                  <span className="flex flex-col items-end shrink-0">
                    {/* Un concepto que desapareció vale S/ 0,00 este mes, y escribirlo así
                        es correcto y se lee fatal: parece que le cobraron cero. Lo que el
                        cliente necesita saber es que ya no se le cobra y cuánto era. */}
                    <span className="text-sm font-semibold text-gray-800">
                      {linea.monto_actual_cent === 0 && linea.monto_previo_cent !== 0
                        ? "Ya no se cobra"
                        : soles(linea.monto_actual_cent)}
                    </span>
                    <span className={`text-xs ${linea.delta_cent > 0 ? "text-rose-600" : "text-emerald-600"}`}>
                      {linea.delta_cent > 0 ? "+" : "−"}{soles(Math.abs(linea.delta_cent))}
                    </span>
                  </span>
                </li>
              );
            })}
            {deuda > 0 && (
              <li className="flex items-center justify-between gap-3 px-5 py-3 bg-amber-50/50">
                <span className="text-sm text-gray-700">Deuda de recibos anteriores</span>
                <span className="text-sm font-semibold text-gray-800">{soles(deuda)}</span>
              </li>
            )}
            {!lineas.length && (
              <li className="px-5 py-6 text-sm text-gray-400">Este recibo no tuvo variaciones.</li>
            )}
          </ul>
        </section>

      </div>

      {/* ---------------------------------------------------- el narrador ---
          Fijo abajo y con UNA sola frase: la que se está leyendo. El historial de la
          explicación se quitó a propósito. En una pantalla de móvil, apilar las frases
          anteriores empuja hacia arriba la que importa y obliga a leer un muro para
          encontrarla; y la que importa es siempre la última, porque es la que señala. Los
          puntos de progreso dejan volver atrás para quien quiera repasar. */}
      <div className="shrink-0 border-t border-gray-100 bg-white">
        {!pasos.length && !explicar.isPending && (
          <div className="p-4">
            <button
              onClick={() => preguntar("¿Por qué cambió mi recibo este mes?")}
              className="w-full bg-[#019DF4] hover:bg-[#0089d8] text-white font-semibold py-3.5 rounded-2xl flex items-center justify-center gap-2"
            >
              <Sparkles size={18} /> Explícame este recibo
            </button>
          </div>
        )}

        {explicar.isPending && (
          <p className="px-5 py-5 text-sm text-gray-400 flex items-center gap-2 m-0">
            <Loader2 size={16} className="animate-spin" /> Revisando su recibo…
          </p>
        )}

        {paso && !explicar.isPending && (
          <>
            <p className="px-5 pt-4 pb-3 text-[15px] leading-relaxed text-gray-800 m-0 min-h-[76px]">
              {paso.texto}
            </p>
            <div className="px-4 pb-3 flex items-center gap-2">
              <button
                onClick={() => setReproduciendo((previo) => !previo)}
                className="p-2 rounded-full hover:bg-gray-100 text-gray-600"
                aria-label={reproduciendo ? "Pausar" : "Continuar"}
              >
                {reproduciendo ? <Pause size={16} /> : <Play size={16} />}
              </button>
              <button
                onClick={() => { window.speechSynthesis?.cancel(); setVoz((previo) => !previo); }}
                className={`p-2 rounded-full hover:bg-gray-100 ${voz ? "text-[#019DF4]" : "text-gray-600"}`}
                aria-label={voz ? "Silenciar la explicación" : "Escuchar la explicación"}
                aria-pressed={voz}
              >
                {voz ? <Volume2 size={16} /> : <VolumeX size={16} />}
              </button>
              <div className="flex-1 flex gap-1">
                {pasos.map((_, posicion) => (
                  <button
                    key={posicion}
                    onClick={() => { window.speechSynthesis?.cancel(); setIndice(posicion); }}
                    aria-label={`Paso ${posicion + 1}`}
                    aria-current={posicion === indice}
                    className={`h-1.5 flex-1 rounded-full transition-colors ${
                      posicion <= indice ? "bg-[#019DF4]" : "bg-gray-200"
                    }`}
                  />
                ))}
              </div>
              <span className="text-xs text-gray-400 tabular-nums">{indice + 1}/{pasos.length}</span>
            </div>
          </>
        )}

        <form
          className="px-4 pb-4 pt-1 flex gap-2"
          onSubmit={(evento: FormEvent) => { evento.preventDefault(); preguntar(borrador); }}
        >
          <input
            value={borrador} onChange={(e) => setBorrador(e.target.value)}
            placeholder="Pregunte otra cosa sobre su recibo"
            className="flex-1 border-2 border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:border-[#019DF4] outline-none min-w-0"
          />
          <button
            type="submit" disabled={!borrador.trim() || explicar.isPending}
            className="bg-[#019DF4] hover:bg-[#0089d8] disabled:opacity-40 text-white px-4 rounded-xl shrink-0"
            aria-label="Enviar"
          >
            <Send size={18} />
          </button>
        </form>
        {error && <p className="px-5 pb-4 text-sm text-rose-600 m-0">{error}</p>}
      </div>
      <div ref={finPasos} />
    </Telefono>
  );
}

function mensajeDeError(causa: unknown): string {
  if (causa instanceof ApiError) return causa.message;
  return causa instanceof Error ? causa.message : "No se pudo completar la operación.";
}

export default ReciboGuiado;
