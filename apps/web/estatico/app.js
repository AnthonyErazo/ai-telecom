/**
 * Consola de demostración de recibo-claro.
 *
 * Dos columnas a la vez, porque el activo de la demo es verlas juntas: a la izquierda lo
 * que ve el cliente; a la derecha la gobernanza que lo respalda. Esta capa **no calcula
 * dinero, no interpreta cifras y no rellena huecos**: pide, mapea y pinta. Cada importe
 * que aparece en pantalla viene de `GET /v1/hechos` o de un bloque de `POST /v1/explicar`.
 *
 * Cómo se sirve: `apps/api/main.py` monta este directorio en `/ui`, así que la página y
 * la API comparten origen. Para apuntar a otra instancia: `/ui/?api=http://host:puerto`.
 */

import * as api from "./api.js";
import { ErrorHttp, ErrorRed } from "./api.js";
import {
  formatearSoles,
  formatearSignado,
  periodoLegible,
  fechaLegible,
  hashCorto,
} from "./formato.js";
import { indiceAserciones, renderizarAcciones, renderizarBloques, renderizarBrief } from "./bloques.js";
import { animarPuente } from "./puente.js";
import { indiceEvidencia, resolverFacto, simularAlucinacion } from "./hechos.js";
import {
  agregarLineas,
  agregarTurno,
  explicarCambioDeModo,
  pintarCadena,
  pintarContador,
  pintarEstadoServicio,
  pintarIndicadores,
  pintarModoLlm,
} from "./gobernanza.js";

/* ------------------------------------------------------------------------- */
/* Constantes                                                                 */
/* ------------------------------------------------------------------------- */

/**
 * Respaldo si `GET /dev/cuentas` no está disponible. Son los tres clientes de guion del
 * dataset sintético (`data/sintetico/bills/`), con la descripción que publica la propia
 * API en el campo `guion`.
 */
const CLIENTES_RESPALDO = {
  "C-DEMO-01": "cambio de plan a mitad de ciclo · renta ADELANTADA",
  "C-DEMO-02": "corte y reconexión por morosidad · renta VENCIDA",
  "C-DEMO-03": "fin de descuento prorrateado + deuda anterior arrastrada",
};

const PREGUNTA_POR_DEFECTO = "¿por qué me vino más caro este mes?";
const DELTA_ALUCINACION_CENT = 731;

/* ------------------------------------------------------------------------- */
/* Estado de la sesión                                                        */
/* ------------------------------------------------------------------------- */

const estado = {
  salud: null,
  preparacion: null,
  clientes: [],
  cuentaId: null,
  verbosidad: "CORTO",
  token: null,
  conversationId: null,
  factset: null,
  errorHechos: null,
  evidencia: new Map(),
  ultimaPregunta: "",
  ultimaRespuesta: null,
  ocupado: false,
  adversarioArmado: false,
};

const nodos = {};

/* ------------------------------------------------------------------------- */
/* Utilidades                                                                 */
/* ------------------------------------------------------------------------- */

/** UUID v4. `crypto.randomUUID` solo existe en contexto seguro; hay respaldo. */
function nuevoUuid() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (globalThis.crypto && globalThis.crypto.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const elemento = (etiqueta, clase, texto) => {
  const nodo = document.createElement(etiqueta);
  if (clase) nodo.className = clase;
  if (texto !== undefined) nodo.textContent = texto;
  return nodo;
};

function alFinal() {
  nodos.conversacion.scrollTop = nodos.conversacion.scrollHeight;
}

function bloquearEntrada(ocupado) {
  estado.ocupado = ocupado;
  nodos.botonEnviar.disabled = ocupado;
  nodos.entrada.disabled = ocupado;
  nodos.botonAlucinar.disabled = ocupado || !estado.token;
  for (const pastilla of nodos.rapidas.querySelectorAll(".pastilla")) pastilla.disabled = ocupado;
}

/** Franja superior de error. `null` la oculta. */
function mostrarFranja(titulo, detalle) {
  if (!titulo) {
    nodos.franja.hidden = true;
    return;
  }
  nodos.franjaTitulo.textContent = titulo;
  nodos.franjaDetalle.textContent = detalle || "";
  nodos.franja.hidden = false;
}

/** Traduce cualquier error a una línea de terminal y a la franja cuando toca. */
function reportarError(error, contexto) {
  if (error instanceof ErrorRed) {
    mostrarFranja(
      "No se pudo contactar con la API",
      `${contexto}: el servidor no responde. Compruebe que corre \`uvicorn apps.api.main:app --port 8000\`.`,
    );
    agregarLineas(nodos.terminalCuerpo, [`ERROR DE RED   ${contexto} · ${error.ruta}`], "error");
    return;
  }
  if (error instanceof ErrorHttp) {
    agregarLineas(
      nodos.terminalCuerpo,
      [`HTTP ${error.estado}      ${error.codigo} · ${contexto}`, `               ${error.detalle}`],
      "error",
    );
    return;
  }
  agregarLineas(nodos.terminalCuerpo, [`ERROR          ${contexto} · ${error.message}`], "error");
}

/* ------------------------------------------------------------------------- */
/* Burbujas de la conversación                                                */
/* ------------------------------------------------------------------------- */

function burbujaCliente(texto) {
  const fila = elemento("li", "turno turno--cliente");
  const burbuja = elemento("div", "burbuja burbuja--cliente");
  burbuja.append(elemento("p", null, texto));
  fila.append(burbuja);
  nodos.conversacion.append(fila);
  alFinal();
  return fila;
}

function burbujaAsistente(clases = "") {
  const fila = elemento("li", "turno turno--asistente");
  const burbuja = elemento("div", `burbuja burbuja--asistente ${clases}`.trim());
  fila.append(burbuja);
  nodos.conversacion.append(fila);
  alFinal();
  return { fila, burbuja };
}

/** Estado "cargando": lo primero que se ve al enviar, y lo que se sustituye después. */
function burbujaCargando() {
  const { fila, burbuja } = burbujaAsistente();
  const carga = elemento("div", "cargando");
  const puntos = elemento("span", "cargando__puntos");
  puntos.append(elemento("i"), elemento("i"), elemento("i"));
  carga.append(puntos, elemento("span", null, "Construyendo hechos, recuperando contexto y verificando cada cifra…"));
  burbuja.append(carga);
  burbuja.append(elemento("div", "esqueleto"), elemento("div", "esqueleto esqueleto--corto"));
  alFinal();
  return fila;
}

/** Nota de la propia consola (no viene del backend): se distingue a propósito. */
function notaConsola(texto) {
  const { burbuja } = burbujaAsistente();
  const aviso = elemento("div", "bloque bloque--aviso aviso aviso--info");
  const icono = elemento("span", "aviso__icono", "i");
  icono.setAttribute("aria-hidden", "true");
  const cuerpo = elemento("div");
  cuerpo.append(elemento("p", "bloque__titulo", "Consola de demostración"), elemento("p", null, texto));
  aviso.append(icono, cuerpo);
  burbuja.append(aviso);
  alFinal();
}

/* ------------------------------------------------------------------------- */
/* Ficha del recibo (GET /v1/hechos)                                          */
/* ------------------------------------------------------------------------- */

function dato(rotulo, texto, { factId = null, tono = "" } = {}) {
  const caja = elemento("div", "dato");
  caja.append(elemento("span", "dato__rotulo", rotulo));
  if (factId) {
    const boton = elemento("button", `cifra dato__valor${tono ? ` dato__valor--${tono}` : ""}`, texto);
    boton.type = "button";
    boton.title = `Anclado en ${factId}`;
    boton.addEventListener("click", () => abrirEvidencia(factId, rotulo, "ANCLADA"));
    caja.append(boton);
  } else {
    caja.append(elemento("span", `dato__valor${tono ? ` dato__valor--${tono}` : ""}`, texto));
  }
  return caja;
}

function pintarFichaRecibo() {
  nodos.ficha.replaceChildren();

  if (estado.errorHechos) {
    const error = estado.errorHechos;
    const aviso = elemento("div", "ficha-recibo__aviso");
    if (error.codigo === "INVARIANTE_FALLIDO") {
      // 409: el recibo no concilia. No se explica, se deriva. Es un estado de guion.
      aviso.append(
        elemento("strong", null, `409 INVARIANTE_FALLIDO · residual ${error.datos.residual_cent} céntimos`),
        elemento(
          "span",
          null,
          " — la suma de las variaciones por concepto no reproduce la diferencia entre totales " +
          `(tolerancia ±${error.datos.tolerancia_cent} céntimo). El recibo no se explica: se deriva a un asesor.`,
        ),
      );
    } else {
      aviso.append(
        elemento("strong", null, `${error.estado} ${error.codigo}`),
        elemento("span", null, ` — ${error.detalle}`),
      );
    }
    nodos.ficha.append(aviso);
    return;
  }

  const factset = estado.factset;
  if (!factset) return;

  const fila = elemento("div", "ficha-recibo__fila");
  fila.append(dato("Periodo", periodoLegible(factset.periodo_actual), { factId: "factset:periodo_actual" }));
  fila.append(dato("Recibo anterior", formatearSoles(factset.total_previo_cent), { factId: "factset:total_previo_cent" }));
  fila.append(dato("Este recibo", formatearSoles(factset.total_actual_cent), { factId: "factset:total_actual_cent" }));

  const delta = factset.delta_total_cent;
  const tono = delta > 0 ? "sube" : delta < 0 ? "baja" : "igual";
  fila.append(
    dato("Diferencia", delta === 0 ? "Sin variación" : formatearSignado(delta), {
      factId: "factset:delta_total_cent",
      tono,
    }),
  );

  if (factset.deuda_anterior_cent) {
    fila.append(dato("Deuda anterior", formatearSoles(factset.deuda_anterior_cent), { factId: "factset:deuda_anterior_cent" }));
    fila.append(
      dato("Total a pagar", formatearSoles(factset.total_actual_cent + factset.deuda_anterior_cent), {
        factId: "factset:total_a_pagar_cent",
      }),
    );
  }
  if (factset.fecha_vencimiento) {
    fila.append(dato("Vence", fechaLegible(factset.fecha_vencimiento), { factId: "factset:fecha_vencimiento" }));
  }
  fila.append(dato("Renta", factset.modalidad_renta === "ADELANTADA" ? "Adelantada" : "Vencida"));
  nodos.ficha.append(fila);

  if (delta === 0) {
    const nota = elemento("div", "ficha-recibo__aviso");
    nota.style.borderColor = "var(--borde)";
    nota.style.background = "var(--fondo-hundido)";
    nota.style.color = "var(--texto-tenue)";
    nota.textContent =
      "Este recibo no varió respecto del anterior: no hay puente que dibujar, solo el detalle de lo que se cobra.";
    nodos.ficha.append(nota);
  }
}

/* ------------------------------------------------------------------------- */
/* Panel de evidencia                                                         */
/* ------------------------------------------------------------------------- */

function abrirEvidencia(factId, etiqueta, estadoAsercion = "ANCLADA") {
  nodos.evidenciaSub.textContent = factId || "(sin fact_id)";
  nodos.evidenciaCuerpo.replaceChildren();

  if (!factId || estadoAsercion === "NO_ANCLADA") {
    const alarma = elemento("div", "evidencia__destacado");
    alarma.style.borderColor = "var(--error)";
    alarma.style.background = "var(--error-tenue)";
    alarma.append(
      elemento("span", "evidencia__valor", etiqueta || "cifra sin anclar"),
      elemento(
        "span",
        "evidencia__origen",
        "Esta cifra NO existe en el FactSet. El verificador la marcó como NO ANCLADA y por eso la respuesta se bloqueó.",
      ),
    );
    nodos.evidenciaCuerpo.append(alarma);
    nodos.evidencia.hidden = false;
    return;
  }

  const resuelto = resolverFacto(estado.factset, factId);
  const destacado = elemento("div", "evidencia__destacado");
  destacado.append(elemento("span", "evidencia__valor", resuelto.resuelto ? resuelto.texto : etiqueta || "—"));
  const origen = resuelto.descripcion
    || (resuelto.resuelto ? "Campo del FactSet sellado" : "Este identificador no está en el FactSet de esta sesión");
  destacado.append(elemento("span", "evidencia__origen", origen));
  nodos.evidenciaCuerpo.append(destacado);

  const contexto = elemento("p", "evidencia__origen");
  contexto.textContent = estado.factset
    ? `FactSet ${hashCorto(estado.factset.sha256)} · periodo ${estado.factset.periodo_actual} · reglas ${estado.factset.rules_version}`
    : "Sin FactSet cargado en esta sesión.";
  nodos.evidenciaCuerpo.append(contexto);

  const items = estado.evidencia.get(factId) || [];
  const lista = elemento("div", "evidencia__lista");
  lista.style.marginTop = ".6rem";
  if (items.length === 0) {
    lista.append(
      elemento(
        "p",
        "evidencia__nada",
        "GET /v1/evidencia no devolvió items adicionales para este campo; el valor de arriba sale directamente del FactSet.",
      ),
    );
  }
  for (const item of items) {
    const caja = elemento("div", "item-evidencia");
    caja.append(elemento("span", "item-evidencia__tipo", item.tipo));
    const cuerpo = elemento("div", "item-evidencia__cuerpo");
    cuerpo.append(elemento("p", "item-evidencia__ref", item.ref_id));
    cuerpo.append(elemento("p", "item-evidencia__texto", item.snippet));
    caja.append(cuerpo);
    lista.append(caja);
  }
  nodos.evidenciaCuerpo.append(lista);
  nodos.evidencia.hidden = false;
}

function cerrarEvidencia() {
  nodos.evidencia.hidden = true;
}

/* ------------------------------------------------------------------------- */
/* Turno completo                                                             */
/* ------------------------------------------------------------------------- */

function pintarRespuesta(respuesta, { degradado = false } = {}) {
  const derivada = Boolean(respuesta.derivacion && respuesta.derivacion.requerida);
  const bloqueada = respuesta.gobernanza && respuesta.gobernanza.verificacion_numerica === "FAIL";
  const clases = [bloqueada ? "burbuja--bloqueada" : "", derivada && !bloqueada ? "burbuja--derivada" : ""]
    .filter(Boolean)
    .join(" ");
  const { burbuja } = burbujaAsistente(clases);

  const firma = elemento("div", "burbuja__firma");
  firma.append(elemento("span", "punto"));
  const veredicto = respuesta.gobernanza ? respuesta.gobernanza.verificacion_numerica : "NO_APLICA";
  const etiquetaFirma = bloqueada
    ? "Respuesta bloqueada · verificación FAIL"
    : derivada
      ? "Derivado a un asesor"
      : veredicto === "NO_APLICA"
        ? "Sin cifras que verificar"
        : "Verificado contra el recibo";
  firma.append(elemento("span", null, etiquetaFirma));
  if (degradado) firma.append(elemento("span", null, "· plantilla"));
  burbuja.append(firma);

  const contexto = {
    aserciones: indiceAserciones(respuesta.gobernanza),
    alPulsarFacto: abrirEvidencia,
  };
  burbuja.append(renderizarBloques(respuesta.bloques, contexto));

  // Estado "sin variación": no hay puente porque no hay causas que dibujar.
  const hayPuente = (respuesta.bloques || []).some((bloque) => bloque.tipo === "puente");
  if (!hayPuente && estado.factset && estado.factset.delta_total_cent === 0) {
    const nota = elemento("p", "puente__pie", "Sin variación entre los dos recibos: no hay cascada que mostrar.");
    burbuja.append(nota);
  }

  if (derivada && respuesta.derivacion.resumen_asesor) {
    burbuja.append(
      renderizarBrief(respuesta.derivacion.resumen_asesor, { contextRef: respuesta.derivacion.context_ref }),
    );
  }

  const acciones = renderizarAcciones(respuesta.acciones, ejecutarAccion);
  if (acciones) burbuja.append(acciones);

  for (const figura of burbuja.querySelectorAll(".puente")) animarPuente(figura);
  alFinal();
  return burbuja;
}

async function pintarGobernanza(respuesta, { degradado = false, adversaria = null } = {}) {
  pintarContador(nodos.contador, respuesta.gobernanza);
  pintarIndicadores(nodos.indicadores, {
    gobernanza: respuesta.gobernanza,
    telemetria: respuesta.telemetria,
    factset: estado.factset,
    degradado,
    traceId: respuesta.trace_id,
    derivacion: respuesta.derivacion,
  });

  try {
    const bitacora = await api.auditoria(estado.token, respuesta.trace_id);
    agregarTurno(nodos.terminalCuerpo, bitacora, {
      adversaria: adversaria || (respuesta.telemetria && respuesta.telemetria.adversaria) || null,
    });
    pintarCadena(nodos.terminalCadena, bitacora);
  } catch (error) {
    reportarError(error, "GET /v1/auditoria");
  }

  try {
    const evidencia = await api.evidencia(estado.token, respuesta.trace_id);
    estado.evidencia = indiceEvidencia(evidencia.items);
  } catch (error) {
    // La evidencia vive en memoria del proceso; si caducó, el resto del turno sigue útil.
    if (!(error instanceof ErrorHttp) || error.codigo !== "EXPLICACION_NO_ENCONTRADA") {
      reportarError(error, "GET /v1/evidencia");
    }
  }
}

async function enviar(utterance) {
  if (estado.ocupado || !estado.token) return;
  const texto = (utterance || "").trim();
  if (!texto) return;

  estado.ultimaPregunta = texto;
  cerrarEvidencia();
  burbujaCliente(texto);
  nodos.entrada.value = "";
  bloquearEntrada(true);
  const cargando = burbujaCargando();

  try {
    const { datos: respuesta, degradado } = await api.explicar(estado.token, {
      conversation_id: estado.conversationId,
      cuenta_id: estado.cuentaId,
      periodo: null,
      verbosidad: estado.verbosidad,
      utterance: texto,
      canal: "APP",
    });
    cargando.remove();
    estado.ultimaRespuesta = respuesta;
    pintarRespuesta(respuesta, { degradado: Boolean(degradado) });
    await pintarGobernanza(respuesta, { degradado: Boolean(degradado) });
    mostrarFranja(null);
    if (estado.adversarioArmado) desarmarBotonAlucinar();
  } catch (error) {
    cargando.remove();
    reportarError(error, "POST /v1/explicar");
    const { burbuja } = burbujaAsistente("burbuja--bloqueada");
    const aviso = elemento("div", "bloque bloque--aviso aviso aviso--critico");
    const icono = elemento("span", "aviso__icono", "✖");
    icono.setAttribute("aria-hidden", "true");
    const cuerpo = elemento("div");
    cuerpo.append(elemento("p", "bloque__titulo", "No se pudo completar el turno"));
    cuerpo.append(
      elemento(
        "p",
        null,
        error instanceof ErrorHttp
          ? `La API respondió ${error.estado} ${error.codigo}: ${error.detalle}`
          : "El servidor no respondió. Verifique que la API sigue corriendo y vuelva a intentarlo.",
      ),
    );
    aviso.append(icono, cuerpo);
    burbuja.append(aviso);
    alFinal();
  } finally {
    bloquearEntrada(false);
    nodos.entrada.focus();
  }
}

/* ------------------------------------------------------------------------- */
/* Acciones de la respuesta                                                   */
/* ------------------------------------------------------------------------- */

async function ejecutarAccion(accion) {
  if (accion.id === "DERIVAR_ASESOR") {
    await derivarAAsesor();
    return;
  }
  if (accion.id === "VER_DETALLE") {
    fijarVerbosidad("DETALLE");
    await enviar(estado.ultimaPregunta || PREGUNTA_POR_DEFECTO);
    return;
  }
  notaConsola(
    `«${accion.etiqueta}» es una acción ${accion.riesgo.toLowerCase()} del contrato (${accion.id}). ` +
    "Esta consola es una demostración: la registra, pero no ejecuta ningún trámite comercial.",
  );
}

async function derivarAAsesor() {
  if (!estado.token || estado.ocupado) return;
  bloquearEntrada(true);
  try {
    const derivacion = await api.derivar(estado.token, {
      conversation_id: estado.conversationId,
      cuenta_id: estado.cuentaId,
      periodo: null,
      motivo_codigo: "PETICION_HUMANO",
      motivo: null,
      utterance: estado.ultimaPregunta || "quiero hablar con una persona",
    });
    const { burbuja } = burbujaAsistente("burbuja--derivada");
    const firma = elemento("div", "burbuja__firma");
    firma.append(elemento("span", "punto"), elemento("span", null, "Hand-off con contexto cargado"));
    burbuja.append(firma);
    burbuja.append(
      elemento(
        "p",
        null,
        `Listo: dejé su caso con un asesor de la cola ${derivacion.cola}. Le llega el resumen completo de su ` +
        `recibo, así que no tendrá que repetir nada. El contexto queda vigente ${derivacion.vigencia_min} minutos.`,
      ),
    );
    burbuja.append(
      renderizarBrief(derivacion.resumen_asesor, {
        contextRef: derivacion.context_ref,
        cola: derivacion.cola,
      }),
    );
    alFinal();

    agregarLineas(
      nodos.terminalCuerpo,
      [
        `DERIVACIÓN     ${derivacion.context_ref} · cola ${derivacion.cola} · prioridad ${derivacion.prioridad}`,
        `               brief de ${derivacion.lineas_brief} líneas · factset ${hashCorto(derivacion.factset_sha256, 8, 4)}`,
      ],
      "alerta",
    );
  } catch (error) {
    reportarError(error, "POST /v1/derivacion");
  } finally {
    bloquearEntrada(false);
  }
}

/* ------------------------------------------------------------------------- */
/* 10 · modo adversario                                                       */
/* ------------------------------------------------------------------------- */

function armarBotonAlucinar() {
  estado.adversarioArmado = true;
  nodos.botonAlucinar.classList.add("es-armado");
  nodos.notaAlucinar.textContent =
    "Modo adversario ARMADO: el siguiente turno recibirá una cifra inventada. Debe salir FAIL.";
}

function desarmarBotonAlucinar() {
  estado.adversarioArmado = false;
  nodos.botonAlucinar.classList.remove("es-armado");
  nodos.notaAlucinar.textContent =
    "Fuerza una cifra falsa en el siguiente turno. Debe salir FAIL, bloquearse la respuesta y caer a plantilla.";
}

/**
 * Arma el modo adversario y demuestra el corte en el mismo clic.
 *
 * `POST /dev/alucinar` con `cuenta_id` hace dos cosas: ejecuta la comparación
 * limpio/envenenado en el acto (y la devuelve en `demo`) y deja armado el siguiente
 * `POST /v1/explicar`. Por eso, después de pintar la comparación, se reenvía la última
 * pregunta: el turno vuelve bloqueado, con `verificacion_numerica = FAIL`, `modo =
 * PLANTILLA` y `derivacion.motivo_codigo = VERIFICACION_FALLIDA`.
 *
 * Si la API corre con `ENTORNO != dev`, el router `/dev` no existe y responde
 * 404 `FUNCION_NO_DISPONIBLE`. En ese caso se cae a una **simulación local**, rotulada
 * como tal, que replica la estrategia de `verificador.inyectar_alucinacion` contra los
 * importes del FactSet ya descargado. Nunca se presenta como veredicto del backend.
 */
async function inyectarAlucinacion() {
  if (!estado.token || estado.ocupado) return;
  bloquearEntrada(true);
  try {
    const respuesta = await api.alucinar(estado.token, {
      activar: true,
      deltaCent: DELTA_ALUCINACION_CENT,
      turnos: 1,
      cuentaId: estado.cuentaId,
    });
    armarBotonAlucinar();

    const demo = respuesta.demo;
    if (demo) {
      agregarLineas(
        nodos.terminalCuerpo,
        [
          `DEMO ADVERSARIA · ${demo.cuenta_id} · periodo ${demo.periodo} · delta ${respuesta.delta_cent} c`,
          `  texto limpio       ${demo.veredicto_limpio} · ${demo.no_ancladas_limpio} sin anclar`,
          `  texto envenenado   ${demo.veredicto_envenenado} · ${demo.no_ancladas_envenenado} sin anclar`,
          `  infractores        ${demo.infractores.join(", ") || "—"}`,
          `  tokens             ${demo.tokens_infractores.join(", ") || "—"}`,
          ...(demo.terminal || []),
        ],
        "adversaria",
      );
      notaConsola(
        `Modo adversario armado. La misma explicación verifica ${demo.veredicto_limpio} limpia y ` +
        `${demo.veredicto_envenenado} con la cifra inventada ${demo.infractores.join(", ")}. ` +
        "Ahora repito su consulta para que vea el corte en vivo.",
      );
    }
  } catch (error) {
    if (error instanceof ErrorHttp && (error.codigo === "FUNCION_NO_DISPONIBLE" || error.estado === 404)) {
      simularAlucinacionLocal();
    } else {
      reportarError(error, "POST /dev/alucinar");
    }
    bloquearEntrada(false);
    return;
  }

  bloquearEntrada(false);
  await enviar(estado.ultimaPregunta || PREGUNTA_POR_DEFECTO);
}

/** Plan B cuando `/dev/alucinar` no existe: se envenena el texto en el navegador. */
function simularAlucinacionLocal() {
  if (!estado.ultimaRespuesta || !estado.factset) {
    notaConsola(
      "El endpoint POST /dev/alucinar no está disponible (la API no corre con ENTORNO=dev) y todavía no hay " +
      "una respuesta que envenenar. Pida primero una explicación.",
    );
    return;
  }
  const texto = (estado.ultimaRespuesta.bloques || [])
    .map((bloque) => bloque.texto || "")
    .filter(Boolean)
    .join("\n");
  const simulacion = simularAlucinacion(texto, estado.factset, DELTA_ALUCINACION_CENT);

  agregarLineas(
    nodos.terminalCuerpo,
    [
      "DEMO ADVERSARIA · SIMULADA EN EL NAVEGADOR (POST /dev/alucinar no disponible)",
      `  infractores        ${simulacion.infractores.join(", ")}`,
      `  tokens             ${simulacion.tokensInfractores.join(", ")}`,
      "  VERIFICACION FAIL  la cifra no pertenece al conjunto de importes del FactSet",
    ],
    "adversaria",
  );
  notaConsola(
    "La API corre sin ENTORNO=dev, así que el modo adversario del backend no está disponible. Esta comparación " +
    "es una SIMULACIÓN local contra los importes del FactSet: el veredicto de verdad siempre lo da el " +
    "verificador del servidor.",
  );
}

/* ------------------------------------------------------------------------- */
/* Controles                                                                  */
/* ------------------------------------------------------------------------- */

function fijarVerbosidad(valor) {
  estado.verbosidad = valor;
  for (const boton of document.querySelectorAll("[data-verbosidad]")) {
    const activo = boton.dataset.verbosidad === valor;
    boton.classList.toggle("es-activa", activo);
    boton.setAttribute("aria-checked", String(activo));
  }
}

function pintarSelectorClientes() {
  nodos.opcionesCliente.replaceChildren();
  for (const cliente of estado.clientes) {
    const boton = elemento("button", "segmentado__opcion");
    boton.type = "button";
    boton.setAttribute("role", "radio");
    boton.setAttribute("aria-checked", String(cliente.id === estado.cuentaId));
    boton.classList.toggle("es-activa", cliente.id === estado.cuentaId);
    boton.dataset.cuenta = cliente.id;
    boton.append(elemento("span", null, cliente.id));
    if (cliente.descripcion) {
      boton.append(elemento("span", "segmentado__pie", cliente.descripcion));
    }
    boton.title = cliente.descripcion || cliente.id;
    boton.addEventListener("click", () => seleccionarCliente(cliente.id));
    nodos.opcionesCliente.append(boton);
  }
}

/** Cambiar de cliente abre una conversación nueva: token nuevo y memoria limpia. */
async function seleccionarCliente(cuentaId) {
  if (estado.ocupado) return;
  estado.cuentaId = cuentaId;
  estado.conversationId = nuevoUuid();
  estado.factset = null;
  estado.errorHechos = null;
  estado.evidencia = new Map();
  estado.ultimaPregunta = "";
  estado.ultimaRespuesta = null;
  estado.token = null;
  desarmarBotonAlucinar();
  pintarSelectorClientes();
  nodos.conversacion.replaceChildren();
  nodos.ficha.replaceChildren();
  pintarContador(nodos.contador, null, { pie: `Sesión nueva para ${cuentaId}. Aún no hay turno que auditar.` });
  nodos.indicadores.replaceChildren();
  bloquearEntrada(true);

  try {
    const token = await api.emitirToken(cuentaId, { nivel: "LOA2", canal: "APP" });
    estado.token = token.access_token;
    agregarLineas(
      nodos.terminalCuerpo,
      [
        `TOKEN          ${cuentaId} · acr ${token.claims.acr} · canal ${token.claims.canal || "APP"} · vence en ${token.expira_en_s}s`,
      ],
      "ausente",
    );
  } catch (error) {
    if (error instanceof ErrorHttp && error.estado === 404) {
      mostrarFranja(
        "El emisor de tokens de prueba no está montado",
        "POST /dev/token solo existe con ENTORNO=dev. Arranque la API con ENTORNO=dev para usar esta consola.",
      );
    } else {
      reportarError(error, "POST /dev/token");
    }
    bloquearEntrada(false);
    return;
  }

  await cargarHechos();
  bloquearEntrada(false);

  const descripcion = (estado.clientes.find((cliente) => cliente.id === cuentaId) || {}).descripcion;
  const periodo = estado.factset ? periodoLegible(estado.factset.periodo_actual) : "este periodo";
  notaConsola(
    `Sesión abierta para ${cuentaId}${descripcion ? ` (${descripcion})` : ""}. ` +
    `Puedo explicarle el recibo de ${periodo}: pregúnteme o use un botón de abajo.`,
  );
}

/** Descarga el FactSet del periodo por defecto y cubre el 409 y el 422. */
async function cargarHechos() {
  estado.factset = null;
  estado.errorHechos = null;
  try {
    estado.factset = await api.hechos(estado.token);
    mostrarFranja(null);
    agregarLineas(
      nodos.terminalCuerpo,
      [
        `HECHOS         ${estado.factset.cuenta_id} · ${estado.factset.periodo_previo} → ${estado.factset.periodo_actual} · ` +
        `Δ ${formatearSignado(estado.factset.delta_total_cent)} · residual ${estado.factset.invariante.residual_cent} c · ` +
        `factset ${hashCorto(estado.factset.sha256, 8, 4)}`,
      ],
      "ausente",
    );
  } catch (error) {
    if (error instanceof ErrorHttp) {
      estado.errorHechos = error;
      agregarLineas(
        nodos.terminalCuerpo,
        [`HECHOS         ${error.estado} ${error.codigo} · ${error.detalle}`],
        error.codigo === "INVARIANTE_FALLIDO" ? "error" : "alerta",
      );
      if (error.codigo === "INVARIANTE_FALLIDO") {
        // El turno de explicación sigue teniendo sentido: derivará con contexto.
        notaConsola(
          "Este recibo no concilia (409 INVARIANTE_FALLIDO). El sistema no va a explicarlo: si pregunta, " +
          "responderá con un aviso sin cifras y abrirá la derivación a un asesor.",
        );
      }
    } else {
      reportarError(error, "GET /v1/hechos");
    }
  }
  pintarFichaRecibo();
}

/* ------------------------------------------------------------------------- */
/* Arranque                                                                   */
/* ------------------------------------------------------------------------- */

async function cargarEstadoServicio() {
  estado.salud = await api.salud();
  try {
    estado.preparacion = await api.preparacion();
  } catch (error) {
    // `/salud/preparacion` responde 503 cuando la cadena de auditoría no valida, pero el
    // cuerpo trae el diagnóstico completo: se aprovecha en vez de perderlo.
    if (!(error instanceof ErrorHttp)) throw error;
    estado.preparacion = error.cuerpo && error.cuerpo.rag ? error.cuerpo : null;
  }
  pintarEstadoServicio(nodos.estadoServicio, estado.salud, estado.preparacion);
  pintarModoLlm(nodos.selectorLlm, nodos.llmEstado, estado.salud, estado.preparacion);
}

async function cargarClientes() {
  try {
    const cuentas = await api.cuentasDemo();
    const guion = cuentas.guion || {};
    const identificadores = (cuentas.demo || []).length ? cuentas.demo : Object.keys(guion);
    estado.clientes = identificadores.map((id) => ({ id, descripcion: guion[id] || "" }));
  } catch {
    estado.clientes = Object.entries(CLIENTES_RESPALDO).map(([id, descripcion]) => ({ id, descripcion }));
  }
  if (estado.clientes.length === 0) {
    estado.clientes = Object.entries(CLIENTES_RESPALDO).map(([id, descripcion]) => ({ id, descripcion }));
  }
}

async function arrancar() {
  mostrarFranja(null);
  try {
    await cargarEstadoServicio();
  } catch (error) {
    reportarError(error, "GET /salud");
    pintarEstadoServicio(nodos.estadoServicio, null, null);
    return;
  }
  await cargarClientes();
  pintarSelectorClientes();
  await seleccionarCliente(estado.clientes[0].id);
}

function conectarControles() {
  nodos.formulario.addEventListener("submit", (evento) => {
    evento.preventDefault();
    enviar(nodos.entrada.value);
  });

  for (const pastilla of nodos.rapidas.querySelectorAll(".pastilla")) {
    pastilla.addEventListener("click", () => enviar(pastilla.dataset.pregunta));
  }

  for (const boton of document.querySelectorAll("[data-verbosidad]")) {
    boton.addEventListener("click", () => fijarVerbosidad(boton.dataset.verbosidad));
  }

  nodos.botonAlucinar.addEventListener("click", inyectarAlucinacion);
  nodos.cerrarEvidencia.addEventListener("click", cerrarEvidencia);
  nodos.botonReintentar.addEventListener("click", arrancar);

  for (const boton of nodos.selectorLlm.querySelectorAll("[data-llm]")) {
    boton.addEventListener("click", () => {
      const pedido = boton.dataset.llm;
      const actual = (estado.salud && estado.salud.llm_mode) || "";
      if (pedido === actual) return;
      const explicacion = explicarCambioDeModo(pedido);
      agregarLineas(nodos.terminalCuerpo, explicacion.map((linea) => `LLM_MODE       ${linea}`), "alerta");
      nodos.llmEstado.textContent = `${explicacion[0]} ${explicacion[1]}`;
      setTimeout(
        () => pintarModoLlm(nodos.selectorLlm, nodos.llmEstado, estado.salud, estado.preparacion),
        6000,
      );
    });
  }

  document.addEventListener("keydown", (evento) => {
    if (evento.key === "Escape" && !nodos.evidencia.hidden) cerrarEvidencia();
  });
}

function recogerNodos() {
  const id = (nombre) => document.getElementById(nombre);
  Object.assign(nodos, {
    estadoServicio: id("estado-servicio"),
    franja: id("franja-error"),
    franjaTitulo: id("franja-error-titulo"),
    franjaDetalle: id("franja-error-detalle"),
    botonReintentar: id("boton-reintentar"),
    opcionesCliente: id("opciones-cliente"),
    ficha: id("ficha-recibo"),
    conversacion: id("conversacion"),
    rapidas: id("rapidas"),
    formulario: id("formulario"),
    entrada: id("entrada"),
    botonEnviar: id("boton-enviar"),
    contador: id("contador"),
    indicadores: id("indicadores"),
    botonAlucinar: id("boton-alucinar"),
    notaAlucinar: id("nota-alucinar"),
    selectorLlm: id("selector-llm"),
    llmEstado: id("llm-estado"),
    terminalCuerpo: id("terminal-cuerpo"),
    terminalCadena: id("terminal-cadena"),
    evidencia: id("evidencia"),
    evidenciaSub: id("evidencia-sub"),
    evidenciaCuerpo: id("evidencia-cuerpo"),
    cerrarEvidencia: id("cerrar-evidencia"),
  });
}

recogerNodos();
conectarControles();
fijarVerbosidad("CORTO");
arrancar();
