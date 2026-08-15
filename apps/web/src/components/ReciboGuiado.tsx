import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, Loader2, Pause, Play, Send, Sparkles } from "lucide-react";
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
 * donde va la frase.
 */

const soles = (centimos: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(centimos / 100);

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

export function ReciboGuiado({ onSesion }: { onSesion?: (cuenta: string, token: string) => void }) {
  const [pantalla, setPantalla] = useState<Pantalla>("acceso");
  const [documento, setDocumento] = useState("");
  const [cuenta, setCuenta] = useState("");
  const [token, setToken] = useState("");
  const [hechos, setHechos] = useState<FactSet | null>(null);
  const [pasos, setPasos] = useState<Paso[]>([]);
  const [indice, setIndice] = useState(0);
  const [reproduciendo, setReproduciendo] = useState(true);
  const [borrador, setBorrador] = useState("");
  const [conversacion, setConversacion] = useState<string | undefined>();
  const [error, setError] = useState("");
  const finPasos = useRef<HTMLDivElement | null>(null);

  const cuentas = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const sugerida = cuentas.data?.demo?.[0] ?? "";
  useEffect(() => { if (!documento && sugerida) setDocumento(sugerida); }, [sugerida, documento]);
  const cuentaElegida = (documento || sugerida).trim();

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

  // Avance automático. Se detiene solo en el último paso: la explicación termina y la
  // última línea señalada se queda encendida, que es donde el cliente quiere mirar.
  useEffect(() => {
    if (!reproduciendo || indice >= pasos.length - 1) return;
    const reloj = window.setTimeout(() => setIndice((previo) => previo + 1), MS_POR_PASO);
    return () => window.clearTimeout(reloj);
  }, [reproduciendo, indice, pasos.length]);

  useEffect(() => { finPasos.current?.scrollIntoView?.({ behavior: "smooth", block: "end" }); }, [indice]);

  const preguntar = (texto: string) => {
    const limpio = texto.trim();
    if (!limpio || explicar.isPending) return;
    setPasos([]); setIndice(0); setBorrador("");
    explicar.mutate(limpio);
  };

  const paso = pasos[indice];
  // Los conceptos encendidos ahora mismo. Se calculan aquí y no dentro del map de líneas
  // para que la decisión de qué se señala esté en un solo sitio y sea legible.
  const encendidos = useMemo(() => new Set(paso?.conceptos ?? []), [paso]);
  const lineas: LineaFactSet[] = (hechos?.lineas ?? []).filter((l) => l.delta_cent !== 0);

  // ------------------------------------------------------------------ acceso
  if (pantalla === "acceso") {
    return (
      <div className="min-h-full flex items-center justify-center bg-gray-50 p-6">
        <form
          className="w-full max-w-sm bg-white rounded-3xl shadow-card border border-gray-100 p-8 space-y-6"
          onSubmit={(e) => { e.preventDefault(); if (cuentaElegida) entrar.mutate(cuentaElegida); }}
        >
          <div className="text-center space-y-3">
            <div className="bg-sky-50 w-20 h-20 rounded-full flex items-center justify-center mx-auto">
              <MovistarLogo className="h-10 w-auto" />
            </div>
            <h1 className="text-xl font-bold text-gray-800 m-0">Recibo guiado</h1>
            <p className="text-sm text-gray-500">Le señalamos en el recibo lo que le vamos contando.</p>
          </div>
          <div className="space-y-2">
            <label htmlFor="cuenta-guiada" className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
              Número de cuenta
            </label>
            <input
              id="cuenta-guiada" list="cuentas-guiadas" value={documento} autoFocus
              onChange={(e) => setDocumento(e.target.value)}
              className="w-full border-2 border-gray-200 rounded-xl px-4 py-3 focus:border-[#019DF4] outline-none"
            />
            <datalist id="cuentas-guiadas">
              {(cuentas.data?.demo ?? []).map((id) => <option key={id} value={id} />)}
            </datalist>
          </div>
          {error && <p className="text-sm text-rose-600">{error}</p>}
          <button
            type="submit" disabled={!cuentaElegida || entrar.isPending}
            className="w-full bg-[#019DF4] hover:bg-[#0089d8] disabled:opacity-50 text-white font-semibold py-3 rounded-xl flex items-center justify-center gap-2"
          >
            {entrar.isPending ? <Loader2 size={18} className="animate-spin" /> : null} Entrar
          </button>
        </form>
      </div>
    );
  }

  // ------------------------------------------------------------------ recibo guiado
  const sube = (hechos?.delta_total_cent ?? 0) >= 0;
  return (
    <div className="min-h-full bg-gray-50 flex flex-col">
      <div className="bg-white px-5 py-4 flex items-center gap-3 border-b border-gray-100">
        <button onClick={() => setPantalla("acceso")} className="p-2 -ml-2 rounded-full hover:bg-gray-100 text-gray-600" aria-label="Volver">
          <ArrowLeft size={20} />
        </button>
        <MovistarLogo className="h-6 w-auto" />
        <h1 className="font-bold text-lg text-gray-800 m-0">Recibo guiado</h1>
        <span className="ml-auto text-xs text-gray-400">{cuenta}</span>
      </div>

      <div className="flex-1 grid lg:grid-cols-2 gap-5 p-5 items-start">
        {/* ------------------------------------------------------ el recibo */}
        <section className="bg-white rounded-2xl shadow-card border border-gray-100 overflow-hidden">
          <div className="p-5 border-b border-gray-100">
            <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Periodo {hechos?.periodo_actual}
            </p>
            <p
              className={`text-4xl font-bold mt-1 rounded-xl px-2 -mx-2 transition-all duration-500 ${
                paso?.total ? "bg-amber-100 text-amber-900 ring-2 ring-amber-400" : "text-gray-800"
              }`}
            >
              {soles(hechos?.total_actual_cent ?? 0)}
            </p>
            <p className={`text-sm font-semibold mt-2 ${sube ? "text-rose-600" : "text-emerald-600"}`}>
              {sube ? "▲" : "▼"} {soles(Math.abs(hechos?.delta_total_cent ?? 0))} respecto del mes anterior
            </p>
          </div>
          <ul className="divide-y divide-gray-100 m-0 p-0 list-none">
            {lineas.map((linea) => {
              const encendida = encendidos.has(linea.concepto_id);
              // Cuando hay algo señalado, lo demás baja de opacidad. Sin nada señalado
              // el recibo se ve entero y normal: no se castiga la lectura libre.
              const apagada = encendidos.size > 0 && !encendida;
              return (
                <li
                  key={linea.concepto_id}
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
            {!lineas.length && (
              <li className="px-5 py-6 text-sm text-gray-400">Este recibo no tuvo variaciones.</li>
            )}
          </ul>
        </section>

        {/* ------------------------------------------------- la explicación */}
        <section className="bg-white rounded-2xl shadow-card border border-gray-100 flex flex-col min-h-[420px]">
          <div className="flex-1 p-5 space-y-3 overflow-y-auto chat-scroll">
            {!pasos.length && !explicar.isPending && (
              <button
                onClick={() => preguntar("¿Por qué me vino más caro este mes?")}
                className="w-full bg-[#019DF4] hover:bg-[#0089d8] text-white font-semibold py-4 rounded-2xl flex items-center justify-center gap-2"
              >
                <Sparkles size={18} /> Explícame este recibo
              </button>
            )}
            {explicar.isPending && (
              <p className="text-sm text-gray-400 flex items-center gap-2">
                <Loader2 size={16} className="animate-spin" /> Revisando su recibo…
              </p>
            )}
            {pasos.slice(0, indice + 1).map((item, posicion) => (
              <p
                key={posicion}
                onClick={() => { setIndice(posicion); setReproduciendo(false); }}
                className={`text-sm leading-relaxed rounded-xl px-4 py-3 cursor-pointer transition-all duration-300 ${
                  posicion === indice
                    ? "bg-sky-50 text-gray-800 ring-1 ring-sky-200"
                    : "text-gray-400 hover:text-gray-600"
                }`}
              >
                {item.texto}
              </p>
            ))}
            <div ref={finPasos} />
          </div>

          {pasos.length > 1 && (
            <div className="px-5 py-3 border-t border-gray-100 flex items-center gap-3">
              <button
                onClick={() => setReproduciendo((previo) => !previo)}
                className="p-2 rounded-full hover:bg-gray-100 text-gray-600"
                aria-label={reproduciendo ? "Pausar" : "Continuar"}
              >
                {reproduciendo ? <Pause size={16} /> : <Play size={16} />}
              </button>
              <div className="flex-1 flex gap-1">
                {pasos.map((_, posicion) => (
                  <button
                    key={posicion}
                    onClick={() => { setIndice(posicion); setReproduciendo(false); }}
                    aria-label={`Paso ${posicion + 1}`}
                    className={`h-1.5 flex-1 rounded-full transition-colors ${
                      posicion <= indice ? "bg-[#019DF4]" : "bg-gray-200"
                    }`}
                  />
                ))}
              </div>
              <span className="text-xs text-gray-400 tabular-nums">{indice + 1}/{pasos.length}</span>
            </div>
          )}

          <form
            className="p-4 border-t border-gray-100 flex gap-2"
            onSubmit={(evento: FormEvent) => { evento.preventDefault(); preguntar(borrador); }}
          >
            <input
              value={borrador} onChange={(e) => setBorrador(e.target.value)}
              placeholder="Pregunte otra cosa sobre su recibo"
              className="flex-1 border-2 border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:border-[#019DF4] outline-none"
            />
            <button
              type="submit" disabled={!borrador.trim() || explicar.isPending}
              className="bg-[#019DF4] hover:bg-[#0089d8] disabled:opacity-40 text-white px-4 rounded-xl"
              aria-label="Enviar"
            >
              <Send size={18} />
            </button>
          </form>
          {error && <p className="px-5 pb-4 text-sm text-rose-600 m-0">{error}</p>}
        </section>
      </div>
    </div>
  );
}

function mensajeDeError(causa: unknown): string {
  if (causa instanceof ApiError) return causa.message;
  return causa instanceof Error ? causa.message : "No se pudo completar la operación.";
}

export default ReciboGuiado;
