import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { money } from "./Blocks";
import type { ElementoCola, PaqueteAsesor } from "../api/types";

type FiltroCanal = "TODOS" | "APP" | "WHATSAPP";

const etiquetaCanal = (canal?: string | null) =>
  canal === "WHATSAPP" ? "WhatsApp" : canal === "APP" ? "App Mi Movistar" : canal || "Canal digital";

/**
 * Consola del asesor del 104 (canal ASESOR).
 *
 * Las tres cosas que esta pantalla existe para demostrar
 * -----------------------------------------------------
 * 1. **El asesor no empieza de cero.** El expediente llega reconstruido desde la bitácora
 *    encadenada, no desde la memoria del proceso: trae el delta, las líneas que lo
 *    componen y **el texto que ya se le dijo al cliente**, para que no se lo repita.
 *
 * 2. **Lo que NO se pudo confirmar va primero.** Un asesor que recibe cifras sin saber
 *    cuáles son hipótesis las confirma al cliente, y entonces el error deja de ser del
 *    motor para pasar a ser de la operadora. Por eso las incertidumbres se pintan arriba
 *    y en rojo, no escondidas al final.
 *
 * 3. **El verificador numérico no se aplica a lo que escribe la persona.** El asesor
 *    responde de sus palabras; la máquina, de las suyas. Lo que sí queda es constancia
 *    nominal de cada turno en la bitácora.
 *
 * El token exige `acting_on_behalf_of`: un asesor siempre actúa a nombre de una cuenta, y
 * eso viaja en cada evento auditado.
 */
export function Asesor({ cuentaSugerida }: { cuentaSugerida: string }) {
  const [asesorId, setAsesorId] = useState("ASESOR-01");
  const [cuenta, setCuenta] = useState(cuentaSugerida);
  const [token, setToken] = useState("");
  const [caso, setCaso] = useState<ElementoCola | null>(null);
  const [paquete, setPaquete] = useState<PaqueteAsesor | null>(null);
  const [borrador, setBorrador] = useState("");
  const [error, setError] = useState("");
  const [filtroCanal, setFiltroCanal] = useState<FiltroCanal>("TODOS");
  const [llamadaSolicitada, setLlamadaSolicitada] = useState(false);
  const clienteConsultas = useQueryClient();

  useEffect(() => { setCuenta(cuentaSugerida); }, [cuentaSugerida]);

  const entrar = useMutation({
    mutationFn: () => api.tokenAsesor(asesorId.trim(), cuenta.trim()),
    onSuccess: (t) => { setToken(t.access_token); setError(""); },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const cola = useQuery({
    queryKey: ["cola", token],
    queryFn: () => api.cola(token),
    enabled: Boolean(token),
    refetchInterval: 5000,
  });
  const casosVisibles = (cola.data ?? []).filter((elemento) =>
    filtroCanal === "TODOS" || elemento.canal === filtroCanal,
  );

  // Aviso de caso nuevo. Un asesor no está mirando la cola: está atendiendo a alguien, o
  // mirando otra pantalla. Si el sistema deriva y nadie se entera, el hand-off «con
  // contexto» se convierte en una cola donde el cliente espera igual que antes.
  const vistos = useRef<Set<string> | null>(null);
  const [aviso, setAviso] = useState<ElementoCola | null>(null);
  useEffect(() => {
    const actual = cola.data;
    if (!actual) return;
    const referencias = new Set(actual.map((elemento) => elemento.context_ref));
    if (vistos.current === null) {
      // La primera carga no avisa: lo que ya estaba en la cola al abrir la consola no es
      // una novedad, y avisar de todo de golpe enseña a ignorar el aviso.
      vistos.current = referencias;
      return;
    }
    const nuevo = actual.find((elemento) => !vistos.current!.has(elemento.context_ref));
    vistos.current = referencias;
    if (nuevo) setAviso(nuevo);
  }, [cola.data]);

  const sala = useQuery({
    queryKey: ["sala", token, caso?.conversation_id],
    queryFn: () => api.sala(token, caso!.conversation_id),
    enabled: Boolean(token && caso),
    // Sondeo y no un canal permanente: para la demostración el resultado visible es
    // idéntico y hay la mitad de piezas que pueden fallar.
    refetchInterval: 3000,
  });

  const abrir = useMutation({
    mutationFn: async (elemento: ElementoCola) => {
      const datos = await api.paquete(token, elemento.context_ref);
      return { elemento, datos };
    },
    onSuccess: ({ elemento, datos }) => {
      setCaso(elemento); setPaquete(datos); setLlamadaSolicitada(false); setError("");
    },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const unirse = useMutation({
    mutationFn: () => api.unirse(token, caso!.conversation_id),
    onSuccess: () => { void clienteConsultas.invalidateQueries({ queryKey: ["sala"] }); setError(""); },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const salir = useMutation({
    mutationFn: () => api.salir(token, caso!.conversation_id),
    onSuccess: () => void clienteConsultas.invalidateQueries({ queryKey: ["sala"] }),
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const solicitarLlamada = useMutation({
    mutationFn: () => api.solicitarLlamada(token, caso!.conversation_id),
    onSuccess: () => { setLlamadaSolicitada(true); setError(""); },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const escribir = useMutation({
    mutationFn: (texto: string) => api.mensajeAsesor(token, caso!.conversation_id, texto),
    onSuccess: () => { setBorrador(""); void clienteConsultas.invalidateQueries({ queryKey: ["sala"] }); },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  const enviar = (evento: FormEvent) => {
    evento.preventDefault();
    const texto = borrador.trim();
    if (texto && !escribir.isPending) escribir.mutate(texto);
  };

  if (!token) {
    return <section className="login-panel" aria-labelledby="asesor-title">
      <div className="login-copy">
        <p className="eyebrow">Call center 104 · Atención digital</p>
        <h1 id="asesor-title">Dashboard del asesor</h1>
        <p>
          Reciba los casos de la <b>App Mi Movistar y WhatsApp</b> con un resumen verificado,
          entre al mismo chat del cliente o solicite una llamada. Cada acción queda auditada.
        </p>
      </div>
      <form className="login-form" onSubmit={(e) => { e.preventDefault(); entrar.mutate(); }}>
        <label htmlFor="asesor-id">Identificador del asesor</label>
        <input id="asesor-id" value={asesorId} onChange={(e) => setAsesorId(e.target.value)} />
        <label htmlFor="asesor-cuenta">Cuenta que va a atender</label>
        <input id="asesor-cuenta" value={cuenta} onChange={(e) => setCuenta(e.target.value)} />
        <button type="submit" disabled={!asesorId.trim() || !cuenta.trim() || entrar.isPending}>
          {entrar.isPending ? "Validando…" : "Entrar a la consola"}
        </button>
        {error && <small className="asesor-error">{error}</small>}
      </form>
    </section>;
  }

  return <div className="workspace asesor-dashboard">
    {/* El aviso se queda hasta que el asesor actúa: un mensaje que se desvanece solo es
        un mensaje que no llegó si en ese momento no estabas mirando. */}
    {aviso && <div className="asesor-aviso" role="alert">
      <div>
        <strong>Nuevo caso en la cola</strong>
        <span>Cuenta {aviso.cuenta_id ?? "sin cuenta"} · {aviso.motivo_codigo ?? "sin motivo"}</span>
      </div>
      <div className="asesor-aviso-acciones">
        <button onClick={() => { abrir.mutate(aviso); setAviso(null); }}>Atender ahora</button>
        <button className="secundario" onClick={() => setAviso(null)}>Después</button>
      </div>
    </div>}

    <section className="panel asesor-cola">
      <div className="asesor-cabecera">
        <div><p className="eyebrow">Cola omnicanal</p><h2>Casos sin atender</h2></div>
        <span className="verdict">{cola.data?.length ?? 0}</span>
      </div>
      <div className="asesor-filtros" role="group" aria-label="Filtrar casos por canal">
        {(["TODOS", "APP", "WHATSAPP"] as const).map((filtro) => (
          <button
            key={filtro}
            type="button"
            className={filtroCanal === filtro ? "activo" : ""}
            onClick={() => setFiltroCanal(filtro)}
          >
            {filtro === "TODOS" ? "Todos" : etiquetaCanal(filtro)}
          </button>
        ))}
      </div>
      {cola.isLoading && <p className="asesor-vacio">Cargando…</p>}
      {cola.data?.length === 0 && <p className="asesor-vacio">
        No hay casos derivados. Pida un asesor desde la App o desde WhatsApp y aparecerá aquí.
      </p>}
      {cola.data && casosVisibles.length === 0 && <p className="asesor-vacio">
        No hay casos de {etiquetaCanal(filtroCanal)} en este momento.
      </p>}
      <ul className="asesor-lista">
        {casosVisibles.map((elemento) => <li key={elemento.context_ref}>
          <button className={caso?.context_ref === elemento.context_ref ? "activo" : ""}
                  onClick={() => abrir.mutate(elemento)} disabled={abrir.isPending}>
            <span className="asesor-canal-badge">{etiquetaCanal(elemento.canal)}</span>
            <strong>{elemento.cuenta_id ?? "sin cuenta"}</strong>
            <span>{elemento.motivo_codigo ?? "sin motivo"}</span>
            <small>{elemento.context_ref}</small>
          </button>
        </li>)}
      </ul>
      {error && <p className="asesor-error">{error}</p>}
    </section>

    <section className="panel asesor-expediente">
      {!paquete && <div className="welcome"><span>📄</span><h2>Elija un caso</h2>
        <p>El expediente se reconstruye desde la bitácora encadenada, no desde la memoria del proceso.</p></div>}

      {paquete && <>
        <div className="asesor-cabecera">
          <div>
            <p className="eyebrow">{paquete.motivo_codigo ?? "consulta"}</p>
            <h2>Cuenta {paquete.cuenta_id} · {etiquetaCanal(paquete.canal)}</h2>
          </div>
          <span className={`verdict ${paquete.evidencia.cadena_valida ? "pass" : ""}`}>
            {paquete.evidencia.cadena_valida ? "CADENA ÍNTEGRA" : "CADENA ROTA"}
          </span>
        </div>

        {/* Lo primero que necesita quien se sienta: QUÉ HA PASADO. Antes de las cifras y
            antes del desglose. El asesor va a hablar con una persona que ya ha contado su
            problema una vez; empezar por «¿en qué puedo ayudarle?» es hacérselo repetir. */}
        <div className="asesor-resumen">
          <strong>Resumen para atender</strong>
          <p>
            El cliente escribió «{paquete.consulta_cliente || "sin consulta registrada"}» por
            {" "}{etiquetaCanal(paquete.canal)}.
            {paquete.ya_explicado.hubo_explicacion
              ? ` Ya se le explicó el recibo (${paquete.ya_explicado.cifras.length} cifras entregadas, verificación ${paquete.ya_explicado.veredicto ?? "n/d"}), así que no hay que repetírselo.`
              : " Todavía no se le ha entregado ninguna explicación: usted empieza la conversación, no la retoma."}
            {/* Solo la primera letra: `toLowerCase()` sobre toda la frase convertía los
                códigos de concepto (`FRTOCH_003`) en algo que no se puede buscar en el
                catálogo, que es justo lo que el asesor va a tener que hacer con ellos. */}
            {paquete.motivo_detalle
              ? ` Llega a usted porque ${paquete.motivo_detalle[0].toLowerCase()}${paquete.motivo_detalle.slice(1)}.`
              : ""}
            {paquete.incertidumbres.length > 0
              ? ` Quedan ${paquete.incertidumbres.length} punto(s) sin confirmar; están abajo.`
              : " Todas las cifras están respaldadas."}
          </p>
        </div>

        {/* Lo que no se pudo confirmar va PRIMERO: es lo que evita que el asesor
            confirme al cliente una hipótesis del motor como si fuera un hecho. */}
        {paquete.incertidumbres.length > 0 && <div className="asesor-dudas">
          <strong>No confirmado ({paquete.incertidumbres.length})</strong>
          <ul>{paquete.incertidumbres.map((duda, i) => <li key={i}>{duda.detalle}</li>)}</ul>
        </div>}

        {paquete.delta_total_cent !== null && paquete.delta_total_cent !== undefined && <div className="summary">
          <div><small>Anterior</small><strong>{money(paquete.total_previo_cent ?? 0)}</strong></div>
          <div><small>Actual</small><strong>{money(paquete.total_actual_cent ?? 0)}</strong></div>
          <div className={paquete.delta_total_cent >= 0 ? "up" : "down"}>
            <small>Variación</small><strong>{money(paquete.delta_total_cent)}</strong>
          </div>
          <div><small>Periodo</small><strong>{paquete.periodo_actual ?? "n/d"}</strong></div>
        </div>}

        {paquete.lineas.length > 0 && <table className="asesor-lineas">
          <thead><tr><th>Concepto</th><th>Variación</th><th>Causa</th></tr></thead>
          <tbody>{paquete.lineas.map((linea) => <tr key={linea.concepto_id}>
            <td>{linea.nombre_comercial || linea.concepto_id}</td>
            <td className={linea.delta_cent >= 0 ? "up" : "down"}>{money(linea.delta_cent)}</td>
            <td>{linea.atribuida ? linea.causa : <em>sin confirmar</em>}</td>
          </tr>)}</tbody>
        </table>}

        {paquete.ya_explicado.hubo_explicacion && <details className="asesor-dicho">
          <summary>Ya se le dijo al cliente ({paquete.ya_explicado.cifras.length} cifras)</summary>
          <p>{paquete.ya_explicado.texto}</p>
        </details>}

        <div className="asesor-pendiente"><strong>Pendiente:</strong> {paquete.accion_pendiente}</div>

        {paquete.brief && <details className="asesor-brief">
          <summary>Ficha verificada · {paquete.verificacion_brief?.veredicto ?? "?"}</summary>
          <pre>{paquete.brief}</pre>
        </details>}

        <small className="asesor-evidencia">{paquete.evidencia.consulta_auditoria} · {paquete.evidencia.eventos} eventos</small>
      </>}
    </section>

    <section className="panel asesor-sala">
      <div className="asesor-cabecera">
        <div><p className="eyebrow">Atención en vivo</p><h2>{sala.data?.modo ?? "—"}</h2></div>
        {sala.data?.asesor
          ? <button className="danger" onClick={() => salir.mutate()} disabled={salir.isPending}>Salir</button>
          : <button onClick={() => unirse.mutate()} disabled={!caso || unirse.isPending}>
              {paquete?.canal === "WHATSAPP" ? "Atender WhatsApp" : "Unirse al chat"}
            </button>}
      </div>
      {!caso && <p className="asesor-vacio">Elija un caso para ver la conversación.</p>}
      {caso && <>
        <div className="asesor-turnos">
          {sala.data?.turnos.map((turno, i) =>
            <div className={`turn ${turno.rol === "cliente" ? "client" : "agent"}`} key={i}>
              <p>{turno.texto}</p>
              <small>{turno.rol}{turno.autor ? ` · ${turno.autor}` : ""}</small>
            </div>)}
          {sala.data?.turnos.length === 0 && <p className="asesor-vacio">Sin turnos todavía.</p>}
        </div>
        <form className="customer-form asesor-form" onSubmit={enviar}>
          <input aria-label="Mensaje al cliente" value={borrador} maxLength={2000}
                 placeholder={sala.data?.asesor ? "Escriba al cliente…" : "Únase a la sala primero"}
                 disabled={!sala.data?.asesor}
                 onChange={(e) => setBorrador(e.target.value)} />
          <button disabled={!sala.data?.asesor || escribir.isPending}>Enviar</button>
        </form>
        <div className="asesor-acciones">
          <button
            type="button"
            className="secundario"
            disabled={!sala.data?.asesor || solicitarLlamada.isPending || llamadaSolicitada}
            onClick={() => solicitarLlamada.mutate()}
          >
            {llamadaSolicitada ? "Llamada solicitada" : solicitarLlamada.isPending ? "Solicitando…" : "Solicitar llamada"}
          </button>
          <small>
            La solicitud queda en la bitácora para el conector de telefonía; el número no se
            expone en este dashboard.
          </small>
        </div>
        <small className="asesor-evidencia">
          Lo que escribe una persona <b>no</b> pasa por el verificador numérico: usted responde de
          sus palabras. Cada turno queda en la bitácora con su identificador.
        </small>
      </>}
    </section>
  </div>;
}

function mensajeDeError(causa: unknown) {
  return causa instanceof ApiError ? `${causa.code}: ${causa.message}`
    : causa instanceof Error ? causa.message : "Ocurrió un error inesperado";
}
