/**
 * La terminal de gobernanza: la columna derecha.
 *
 * Es lo que ningún otro equipo enseña, así que se pinta con el mismo vocabulario que el
 * backend imprime en la consola del servidor. Nada de esto se calcula aquí: el contador
 * de afirmaciones sale de `gobernanza`, las seis líneas de pipeline salen tal cual de
 * `GET /v1/auditoria` (campo `terminal`, que es la misma función
 * `formatear_para_terminal` que corre en el servidor) y la validez de la cadena de
 * hashes sale de `cadena_valida` / `indice_roto`.
 */

import { hashCorto, horaLegible, porcentajeFraccion } from "./formato.js";

/* ------------------------------------------------------------------------- */
/* 7 · contador grande                                                        */
/* ------------------------------------------------------------------------- */

/**
 * Cabecera `AFIRMACIONES NUMÉRICAS n · ANCLADAS n · NO ANCLADAS n`.
 * Verde si no quedó ninguna sin anclar, rojo si quedó alguna.
 */
export function pintarContador(contenedor, gobernanza, { pie = "" } = {}) {
  const linea = contenedor.querySelector("#contador-linea");
  const nota = contenedor.querySelector("#contador-pie");
  if (!gobernanza) {
    contenedor.className = "contador contador--neutro";
    linea.textContent = "AFIRMACIONES NUMÉRICAS — · ANCLADAS — · NO ANCLADAS —";
    nota.textContent = pie || "Aún no se ha generado ninguna explicación en esta sesión.";
    return;
  }
  const totales = gobernanza.aserciones_totales ?? 0;
  const ancladas = gobernanza.aserciones_ancladas ?? 0;
  const sinAnclar = gobernanza.aserciones_no_ancladas ?? 0;
  const limpio = sinAnclar === 0;

  contenedor.className = `contador ${limpio ? "contador--ok" : "contador--fallo"}`;
  linea.textContent = `AFIRMACIONES NUMÉRICAS ${totales} · ANCLADAS ${ancladas} · NO ANCLADAS ${sinAnclar}`;
  nota.textContent =
    pie ||
    (limpio
      ? "Cada cifra entregada está anclada a un campo del FactSet."
      : "Hay cifras sin respaldo: la respuesta se bloquea y el turno se deriva.");
}

/* ------------------------------------------------------------------------- */
/* 9 · indicadores del turno                                                  */
/* ------------------------------------------------------------------------- */

function tarjeta(rotulo, valor, tono = "", ancho = false) {
  const nodo = document.createElement("div");
  nodo.className = `indicador${tono ? ` indicador--${tono}` : ""}${ancho ? " indicador--ancho" : ""}`;
  const titulo = document.createElement("p");
  titulo.className = "indicador__rotulo";
  titulo.textContent = rotulo;
  const cuerpo = document.createElement("p");
  cuerpo.className = "indicador__valor";
  cuerpo.textContent = valor;
  nodo.append(titulo, cuerpo);
  return nodo;
}

function tarjetaCopiable(rotulo, valor, alCopiar) {
  const nodo = tarjeta(rotulo, "", "", true);
  const cuerpo = nodo.querySelector(".indicador__valor");
  cuerpo.textContent = "";
  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "indicador__copiar";
  boton.textContent = valor;
  boton.title = "Pulse para copiar";
  boton.addEventListener("click", async () => {
    const previo = boton.textContent;
    try {
      await navigator.clipboard.writeText(valor);
      boton.textContent = "copiado ✓";
    } catch {
      // Sin permiso de portapapeles (o sin HTTPS): se selecciona para copiar a mano.
      const rango = document.createRange();
      rango.selectNodeContents(boton);
      const seleccion = window.getSelection();
      seleccion.removeAllRanges();
      seleccion.addRange(rango);
      boton.textContent = `${previo} · seleccione y copie`;
    }
    setTimeout(() => { boton.textContent = previo; }, 1600);
    if (typeof alCopiar === "function") alCopiar(valor);
  });
  cuerpo.append(boton);
  return nodo;
}

const TONO_VEREDICTO = { PASS: "ok", FAIL: "error", NO_APLICA: "alerta" };

/**
 * Rejilla de indicadores: veredicto, modo de generación, confianza, residual del
 * invariante, latencia, `trace_id` copiable y sello del FactSet.
 */
export function pintarIndicadores(
  contenedor,
  { gobernanza, telemetria, factset, degradado, traceId, derivacion },
) {
  contenedor.replaceChildren();
  if (!gobernanza) return;

  const veredicto = gobernanza.verificacion_numerica || "NO_APLICA";
  contenedor.append(tarjeta("Verificación", veredicto, TONO_VEREDICTO[veredicto] || ""));

  if (derivacion && derivacion.requerida) {
    contenedor.append(tarjeta("Derivación", derivacion.motivo_codigo || "SÍ", "alerta", true));
  }

  const modo = gobernanza.modo || "—";
  contenedor.append(
    tarjeta("Generación", modo, modo === "PLANTILLA" ? "alerta" : "ok"),
  );

  contenedor.append(tarjeta("Confianza", porcentajeFraccion(gobernanza.confianza)));

  const residual = factset && factset.invariante ? factset.invariante.residual_cent : null;
  if (residual !== null && residual !== undefined) {
    contenedor.append(
      tarjeta("Residual", `${residual} c`, Math.abs(residual) > 1 ? "error" : "ok"),
    );
  }

  const latencia = gobernanza.latencia_ms ?? (telemetria && telemetria.latencia_ms);
  if (latencia !== null && latencia !== undefined) {
    contenedor.append(tarjeta("Latencia", `${latencia} ms`));
  }

  if (telemetria && telemetria.score_incomprension !== undefined && telemetria.score_incomprension !== null) {
    const score = Number(telemetria.score_incomprension);
    contenedor.append(tarjeta("Incomprensión", score.toFixed(2), score >= 0.6 ? "alerta" : ""));
  }

  // `trace_id` vive en la raíz de la respuesta, no dentro de `gobernanza`.
  contenedor.append(tarjetaCopiable("Trace id", traceId || "—"));
  contenedor.append(tarjeta("FactSet", hashCorto(gobernanza.factset_sha256), "", true));
  contenedor.append(tarjeta("Reglas", gobernanza.rules_version || "—"));
  contenedor.append(tarjeta("Modelo", gobernanza.model_version || "—", degradado ? "alerta" : "", true));
}

/* ------------------------------------------------------------------------- */
/* 8 · registro tipo terminal                                                 */
/* ------------------------------------------------------------------------- */

/**
 * Clasifica una línea del banner para colorearla.
 *
 * El backend imprime las marcas en Unicode (`✔ ▲ ✖ ·`) o en ASCII (`+ ! x .`) según lo
 * que soporte la consola; se reconocen las dos.
 */
function clasificarLinea(linea) {
  const texto = String(linea);
  if (/^[╭╰+][-─]/.test(texto)) return "marco";
  if (texto.includes("AFIRMACIONES NUMÉRICAS")) {
    return /NO ANCLADAS 0(\s|$|\D)/.test(texto) ? "cabecera-ok" : "cabecera-fallo";
  }
  const marca = /^\s{2}([✔▲✖·+!x.])\s/.exec(texto);
  if (marca) {
    const mapa = { "✔": "ok", "+": "ok", "▲": "alerta", "!": "alerta", "✖": "error", x: "error", "·": "ausente", ".": "ausente" };
    return mapa[marca[1]] || "ok";
  }
  if (/VERIFICACION FAIL|NO ANCLADAS: /.test(texto)) return "adversaria";
  return "ok";
}

function lineasComoNodos(lineas, claseExtra = "") {
  const fragmento = document.createDocumentFragment();
  for (const linea of lineas || []) {
    const nodo = document.createElement("div");
    nodo.className = `turno-log__linea turno-log__linea--${claseExtra || clasificarLinea(linea)}`;
    nodo.textContent = linea;
    fragmento.append(nodo);
  }
  return fragmento;
}

/**
 * Añade el bloque de un turno a la terminal: las pocas líneas de pipeline visibles y,
 * plegado, el detalle completo de los eventos con su payload.
 */
export function agregarTurno(contenedor, auditoria, { adversaria = null } = {}) {
  const vacio = contenedor.querySelector(".terminal__vacio");
  if (vacio) vacio.remove();

  const bloque = document.createElement("div");
  bloque.className = "turno-log";
  bloque.append(lineasComoNodos(auditoria.terminal));

  if (adversaria && adversaria.terminal) {
    bloque.append(lineasComoNodos(adversaria.terminal, "adversaria"));
  }

  const eventos = auditoria.eventos || [];
  if (eventos.length) {
    const detalle = document.createElement("details");
    detalle.className = "turno-log__detalle";
    const resumen = document.createElement("summary");
    resumen.textContent = `${eventos.length} eventos auditados · ver payload`;
    detalle.append(resumen);

    for (const evento of eventos) {
      const caja = document.createElement("div");
      caja.className = "evento";
      const cabecera = document.createElement("div");
      cabecera.className = "evento__cabecera";
      const etapa = document.createElement("span");
      etapa.className = "evento__etapa";
      etapa.textContent = evento.etapa;
      cabecera.append(`[${evento.indice}] `, etapa, ` ${horaLegible(evento.ts)} · ${hashCorto(evento.hash, 8, 4)}`);
      const payload = document.createElement("pre");
      payload.className = "evento__payload";
      payload.textContent = JSON.stringify(evento.payload, null, 1);
      caja.append(cabecera, payload);
      detalle.append(caja);
    }
    bloque.append(detalle);
  }

  contenedor.append(bloque);
  contenedor.scrollTop = contenedor.scrollHeight;
  return bloque;
}

/** Escribe una línea suelta en la terminal (errores, avisos de la consola). */
export function agregarLineas(contenedor, lineas, clase = "alerta") {
  const vacio = contenedor.querySelector(".terminal__vacio");
  if (vacio) vacio.remove();
  const bloque = document.createElement("div");
  bloque.className = "turno-log";
  bloque.append(lineasComoNodos(lineas, clase));
  contenedor.append(bloque);
  contenedor.scrollTop = contenedor.scrollHeight;
}

/** Estado de la cadena de hashes en la cabecera de la terminal. */
export function pintarCadena(nodo, auditoria) {
  if (!auditoria) {
    nodo.textContent = "";
    nodo.className = "terminal__cadena";
    return;
  }
  if (auditoria.cadena_valida) {
    nodo.textContent = "cadena íntegra";
    nodo.className = "terminal__cadena es-ok";
  } else {
    nodo.textContent = `CADENA ROTA en ${auditoria.indice_roto}`;
    nodo.className = "terminal__cadena es-rota";
  }
}

/* ------------------------------------------------------------------------- */
/* Chips de estado del servicio y 11 · modo del modelo                        */
/* ------------------------------------------------------------------------- */

function chip(texto, tono = "") {
  const nodo = document.createElement("span");
  nodo.className = `chip${tono ? ` chip--${tono}` : ""}`;
  nodo.textContent = texto;
  return nodo;
}

/** Chips de la barra global: entorno, versión de reglas, verificador, RAG y bitácora. */
export function pintarEstadoServicio(contenedor, salud, preparacion) {
  contenedor.replaceChildren();
  if (!salud) {
    contenedor.append(chip("API no disponible", "error"));
    return;
  }
  contenedor.append(chip(`entorno ${salud.entorno}`, salud.entorno === "dev" ? "" : "alerta"));
  contenedor.append(chip(`reglas ${salud.rules_version}`));
  contenedor.append(
    chip(
      salud.verificador_estricto ? "verificador estricto" : "verificador permisivo",
      salud.verificador_estricto ? "ok" : "alerta",
    ),
  );

  if (preparacion) {
    const rag = preparacion.rag || {};
    const vectorial = rag.vectorial || {};
    if (vectorial.respaldo) {
      contenedor.append(
        chip(`RAG ${vectorial.respaldo}`, vectorial.respaldo === "pgvector" ? "ok" : "alerta"),
      );
    }
    const auditoria = preparacion.auditoria || {};
    if (auditoria.cadena_valida !== undefined) {
      contenedor.append(
        chip(auditoria.cadena_valida ? "cadena válida" : "cadena rota",
          auditoria.cadena_valida ? "ok" : "error"),
      );
    }
  }
}

/**
 * Interruptor del modo del modelo.
 *
 * **El modo NO se puede cambiar desde el navegador y esto lo dice en voz alta.** El
 * proveedor generativo se resuelve una sola vez por proceso: `LLM_MODE` lo lee
 * `Ajustes` (cacheado con `lru_cache`) y `obtener_proveedor_llm()` también está
 * cacheado y se construye en el arranque (`calentar()`). Cambiarlo en caliente
 * significaría reconstruir singletons de la API desde una página web, que es
 * exactamente lo que no debe poder hacerse. Así que el interruptor **refleja** el modo
 * real y, si se pulsa el otro, explica cómo cambiarlo de verdad.
 */
export function pintarModoLlm(contenedor, estadoNodo, salud, preparacion) {
  const modo = (salud && salud.llm_mode) || "desconocido";
  for (const boton of contenedor.querySelectorAll(".segmentado__opcion")) {
    const activo = boton.dataset.llm === modo;
    boton.classList.toggle("es-activa", activo);
    boton.setAttribute("aria-checked", String(activo));
  }

  const llm = (preparacion && preparacion.llm) || {};
  const proveedor = llm.proveedor || "—";
  const degradado = Boolean(llm.degradado);
  estadoNodo.replaceChildren();

  const linea = document.createElement("span");
  linea.textContent = degradado
    ? `Sin proveedor generativo: se responde con plantilla determinística (${proveedor}).`
    : `Corriendo en modo ${modo} · proveedor ${proveedor}.`;
  estadoNodo.append(linea);
  return modo;
}

/** Mensaje que se muestra al intentar cambiar el modo desde la consola. */
export function explicarCambioDeModo(modoPedido) {
  return [
    `El modo "${modoPedido}" se activa en el servidor, no desde el navegador.`,
    modoPedido === "gemini"
      ? "Exporte LLM_MODE=gemini y GEMINI_API_KEY=... y reinicie la API."
      : "Exporte LLM_MODE=mock y reinicie la API.",
    "El proveedor se construye una sola vez en el arranque (calentar()), por eso hace falta el reinicio.",
  ];
}
