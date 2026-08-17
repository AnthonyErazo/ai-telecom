import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { ElementoCola, PaqueteAsesor } from "../api/types";

type FiltroCanal = "TODOS" | "APP" | "WHATSAPP";

const etiquetaCanal = (canal?: string | null) =>
  canal === "WHATSAPP" ? "WhatsApp" : canal === "APP" ? "App Mi Movistar" : canal || "Canal digital";

const etiquetaMotivo = (motivo?: string | null) => ({
  PETICION_HUMANO: "Solicita hablar con una persona",
  INTENCION_REGULATORIA: "Requiere atención especializada",
  INVARIANTE_ROTO: "Revisión de facturación",
  VERIFICACION_FALLIDA: "Consulta por confirmar",
}[motivo ?? ""] ?? "Consulta de facturación");

const textoPendiente = (codigo?: string | null) => ({
  SIN_HECHOS: "No hay información suficiente del recibo para confirmar la consulta todavía.",
  INVARIANTE_ROTO: "El detalle del recibo necesita revisión antes de confirmar importes.",
  LINEA_SIN_ATRIBUIR: "Hay cambios en el recibo cuya causa todavía no está identificada.",
  CAUSA_POCO_FIABLE: "La causa del cambio debe confirmarse antes de comunicarla como definitiva.",
  CIFRA_NO_ANCLADA: "Hay importes que requieren validación antes de compartirlos con el cliente.",
  SIN_EXPLICACION_ENTREGADA: "El cliente aún no recibió una explicación de su caso.",
  CADENA_ROTA: "El caso requiere una revisión interna antes de confirmar información.",
}[codigo ?? ""] ?? "Hay información pendiente de confirmar antes de dar una respuesta definitiva.");

/** Consola de atención para asesores del 104. */
export function Asesor() {
  const [asesorNombre, setAsesorNombre] = useState("");
  const [token, setToken] = useState("");
  const [caso, setCaso] = useState<ElementoCola | null>(null);
  const [paquete, setPaquete] = useState<PaqueteAsesor | null>(null);
  const [borrador, setBorrador] = useState("");
  const [error, setError] = useState("");
  const [filtroCanal, setFiltroCanal] = useState<FiltroCanal>("TODOS");
  const [llamadaSolicitada, setLlamadaSolicitada] = useState(false);
  const clienteConsultas = useQueryClient();
  const cuentas = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const cuentaInicial = cuentas.data?.demo?.[0] ?? "";

  const entrar = useMutation({
    mutationFn: () => api.tokenAsesor(asesorNombre.trim(), cuentaInicial),
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
      if (!elemento.cuenta_id) throw new Error("No se pudo abrir este caso.");
      // La cuenta se resuelve desde el caso elegido; el asesor nunca tiene que conocerla
      // ni escribirla para atender otro canal.
      const sesionCaso = await api.tokenAsesor(asesorNombre.trim(), elemento.cuenta_id);
      const datos = await api.paquete(sesionCaso.access_token, elemento.context_ref);
      return { elemento, datos, token: sesionCaso.access_token };
    },
    onSuccess: ({ elemento, datos, token: tokenCaso }) => {
      setToken(tokenCaso); setCaso(elemento); setPaquete(datos); setLlamadaSolicitada(false); setError("");
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
        <p>Vea los casos de la App Mi Movistar y WhatsApp, conozca el resumen y únase a la atención en vivo.</p>
      </div>
      <form className="login-form" onSubmit={(e) => { e.preventDefault(); entrar.mutate(); }}>
        <label htmlFor="asesor-nombre">Nombre del asesor</label>
        <input id="asesor-nombre" value={asesorNombre} placeholder="Ej. Ana Torres"
               onChange={(e) => setAsesorNombre(e.target.value)} autoComplete="name" />
        <button type="submit" disabled={!asesorNombre.trim() || !cuentaInicial || entrar.isPending}>
          {entrar.isPending ? "Ingresando…" : cuentas.isLoading ? "Cargando…" : "Entrar a la consola"}
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
        <span>{etiquetaMotivo(aviso.motivo_codigo)} · {etiquetaCanal(aviso.canal)}</span>
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
            <strong>{etiquetaMotivo(elemento.motivo_codigo)}</strong>
          </button>
        </li>)}
      </ul>
      {error && <p className="asesor-error">{error}</p>}
    </section>

    <section className="panel asesor-expediente">
      {!paquete && <div className="welcome"><span>📄</span><h2>Elija un caso</h2>
        <p>Seleccione un caso para ver el resumen y unirse a la atención.</p></div>}

      {paquete && <>
        <div className="asesor-cabecera">
          <div>
            <p className="eyebrow">Resumen del caso</p>
            <h2>Atención por {etiquetaCanal(paquete.canal)}</h2>
          </div>
        </div>

        <div className="asesor-resumen">
          <strong>Resumen de la atención</strong>
          <p>
            El cliente solicitó atención humana por {etiquetaMotivo(paquete.motivo_codigo).toLowerCase()}.
          </p>
        </div>
        <div className="asesor-contexto">
          <section>
            <h3>El cliente consulta</h3>
            <p>“{paquete.consulta_cliente || "No se registró una consulta inicial."}”</p>
          </section>
          <section>
            <h3>Ya se le respondió</h3>
            <p>{paquete.ya_explicado.hubo_explicacion
              ? paquete.ya_explicado.texto || "Se entregó una explicación sobre su recibo."
              : "Aún no recibió una explicación. Inicie la atención sin asumir una respuesta previa."}</p>
          </section>
          <section>
            <h3>Antes de confirmar</h3>
            {paquete.incertidumbres.length
              ? <ul>{paquete.incertidumbres.map((pendiente, indice) =>
                  <li key={indice}>{textoPendiente(pendiente.codigo)}</li>)}</ul>
              : <p>La información revisada está lista para continuar con el cliente.</p>}
          </section>
          <section>
            <h3>Siguiente paso sugerido</h3>
            <p>{paquete.accion_pendiente || "Escuche al cliente y continúe la atención desde esta conversación."}</p>
          </section>
        </div>
      </>}
    </section>

    <section className="panel asesor-sala">
      <div className="asesor-cabecera">
        <div><p className="eyebrow">Atención en vivo</p><h2>{sala.data?.asesor ? "Conversación activa" : "Lista para unirse"}</h2></div>
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
              <small>{turno.rol === "asesor" ? `Tú · ${turno.autor}` : turno.rol === "cliente" ? "Cliente" : "BillSense"}</small>
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
            Coordinaremos una llamada con el cliente.
          </small>
        </div>
      </>}
    </section>
  </div>;
}

function mensajeDeError(causa: unknown) {
  return causa instanceof ApiError ? `${causa.code}: ${causa.message}`
    : causa instanceof Error ? causa.message : "Ocurrió un error inesperado";
}
