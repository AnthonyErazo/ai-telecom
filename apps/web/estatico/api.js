/**
 * Cliente HTTP de la API de recibo-claro.
 *
 * La página se sirve desde la propia API (`/ui`), así que el origen por defecto es el
 * mismo y no hace falta CORS. Para abrirla contra otra instancia se pasa `?api=` en la
 * URL, por ejemplo `http://127.0.0.1:8000/ui/?api=http://otra-maquina:8000`; la API ya
 * expone `X-Trace-Id` y `X-Degradado` en `expose_headers`, así que las cabeceras siguen
 * leyéndose desde otro origen.
 *
 * Todo error de negocio de la API viaja con el mismo cuerpo
 * (`{codigo, detalle, trace_id, nivel_requerido, datos}`), y los canales enrutan por
 * `codigo`, nunca por el texto: `ErrorHttp` lo conserva tal cual.
 */

const PARAMETROS = new URLSearchParams(location.search);

/** Base de la API. Vacía = mismo origen que la página. */
export const BASE = (PARAMETROS.get("api") || "").replace(/\/+$/, "");

/** Error de negocio o de contrato devuelto por la API (4xx/5xx con cuerpo conocido). */
export class ErrorHttp extends Error {
  constructor(estado, cuerpo, ruta) {
    const codigo = (cuerpo && cuerpo.codigo) || `HTTP_${estado}`;
    const detalle = (cuerpo && cuerpo.detalle) || `la API respondió ${estado}`;
    super(detalle);
    this.name = "ErrorHttp";
    this.estado = estado;
    this.codigo = codigo;
    this.detalle = detalle;
    this.datos = (cuerpo && cuerpo.datos) || {};
    // Cuerpo íntegro: `GET /salud/preparacion` responde 503 con el detalle de readiness,
    // que no es un `RespuestaError` y sigue siendo la información que hay que pintar.
    this.cuerpo = cuerpo;
    this.traceId = (cuerpo && cuerpo.trace_id) || null;
    this.nivelRequerido = (cuerpo && cuerpo.nivel_requerido) || null;
    this.ruta = ruta;
  }
}

/** La petición no llegó a completarse: servidor apagado, DNS, cable, CORS. */
export class ErrorRed extends Error {
  constructor(ruta, causa) {
    super(`no se pudo contactar con la API en ${ruta}`);
    this.name = "ErrorRed";
    this.ruta = ruta;
    this.causa = causa;
  }
}

async function pedir(ruta, { metodo = "GET", cuerpo = null, token = null } = {}) {
  const cabeceras = { Accept: "application/json" };
  if (cuerpo !== null) cabeceras["Content-Type"] = "application/json";
  if (token) cabeceras.Authorization = `Bearer ${token}`;

  let respuesta;
  try {
    respuesta = await fetch(`${BASE}${ruta}`, {
      method: metodo,
      headers: cabeceras,
      body: cuerpo === null ? undefined : JSON.stringify(cuerpo),
    });
  } catch (causa) {
    throw new ErrorRed(ruta, causa);
  }

  const texto = await respuesta.text();
  let datos = null;
  if (texto) {
    try {
      datos = JSON.parse(texto);
    } catch {
      datos = { codigo: "RESPUESTA_NO_JSON", detalle: texto.slice(0, 300) };
    }
  }
  if (!respuesta.ok) throw new ErrorHttp(respuesta.status, datos, ruta);

  return {
    datos,
    estado: respuesta.status,
    traceId: respuesta.headers.get("X-Trace-Id"),
    degradado: respuesta.headers.get("X-Degradado"),
  };
}

/* ------------------------------------------------------------------------- */
/* Salud y arranque                                                           */
/* ------------------------------------------------------------------------- */

/** `GET /salud` — liveness: entorno, `rules_version`, `llm_mode`, verificador. */
export const salud = () => pedir("/salud").then((r) => r.datos);

/** `GET /salud/preparacion` — readiness: RAG, proveedor generativo y bitácora. */
export const preparacion = () => pedir("/salud/preparacion").then((r) => r.datos);

/** `GET /dev/cuentas` — cuentas de guion del dataset. Solo con `ENTORNO=dev`. */
export const cuentasDemo = () => pedir("/dev/cuentas").then((r) => r.datos);

/* ------------------------------------------------------------------------- */
/* Identidad                                                                  */
/* ------------------------------------------------------------------------- */

/**
 * `POST /dev/token` — emite un JWT de prueba. Solo existe con `ENTORNO=dev`; en
 * producción los tokens los emite el IdP de Movistar y esta consola no los pide.
 */
export function emitirToken(cuentaId, { nivel = "LOA2", canal = "APP" } = {}) {
  return pedir("/dev/token", {
    metodo: "POST",
    cuerpo: { cuenta_id: cuentaId, nivel, canal },
  }).then((r) => r.datos);
}

/* ------------------------------------------------------------------------- */
/* Núcleo                                                                     */
/* ------------------------------------------------------------------------- */

/** `GET /v1/hechos` — FactSet conciliado. Exige LOA2. Puede responder 409. */
export function hechos(token, { periodo = null } = {}) {
  const consulta = periodo ? `?periodo=${encodeURIComponent(periodo)}` : "";
  return pedir(`/v1/hechos${consulta}`, { token }).then((r) => r.datos);
}

/** `POST /v1/explicar` — explicación verificada. Devuelve también las cabeceras. */
export function explicar(token, peticion) {
  return pedir("/v1/explicar", { metodo: "POST", cuerpo: peticion, token });
}

/** `GET /v1/evidencia/{explicacion_id}` — el `explicacion_id` ES el `trace_id`. */
export function evidencia(token, explicacionId, { solo = null } = {}) {
  const consulta = solo ? `?solo=${encodeURIComponent(solo)}` : "";
  return pedir(`/v1/evidencia/${encodeURIComponent(explicacionId)}${consulta}`, { token })
    .then((r) => r.datos);
}

/** `GET /v1/auditoria` — eventos del turno, resumen y validez de la cadena. */
export function auditoria(token, traceId, { incluirEventos = true } = {}) {
  const consulta = `?trace_id=${encodeURIComponent(traceId)}&incluir_eventos=${incluirEventos}`;
  return pedir(`/v1/auditoria${consulta}`, { token }).then((r) => r.datos);
}

/** `POST /v1/derivacion` — hand-off explícito con el contexto cargado. */
export function derivar(token, peticion) {
  return pedir("/v1/derivacion", { metodo: "POST", cuerpo: peticion, token }).then((r) => r.datos);
}

/**
 * `POST /dev/alucinar` — modo adversario. Exige LOA2 y `ENTORNO=dev`.
 *
 * Con `cuenta_id` el backend ejecuta la demo en el acto (genera, envenena y vuelve a
 * verificar) **y** deja armado el siguiente turno de `/v1/explicar`.
 */
export function alucinar(token, { activar = true, deltaCent = 731, turnos = 1, cuentaId = null, periodo = null } = {}) {
  const cuerpo = { activar, delta_cent: deltaCent, turnos };
  if (cuentaId) cuerpo.cuenta_id = cuentaId;
  if (periodo) cuerpo.periodo = periodo;
  return pedir("/dev/alucinar", { metodo: "POST", cuerpo, token }).then((r) => r.datos);
}
