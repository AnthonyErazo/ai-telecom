/**
 * Renderizador de la respuesta canal-agnóstica.
 *
 * El backend no devuelve HTML: devuelve **bloques tipados** (`texto`, `kv`, `puente`,
 * `tabla`, `aviso`) y este módulo los mapea a DOM. La regla es que el renderizador **no
 * decide nada**: no reordena, no inventa títulos, no calcula importes y no oculta
 * bloques. Si llega un tipo desconocido —porque la API creció— se pinta su texto plano
 * antes que perderlo.
 *
 * Las cifras del texto se vuelven pulsables usando `gobernanza.aserciones`, que ya trae
 * `texto_original` (tal como apareció) y `fuente` (el `fact_id` que la ancla). No se
 * recalcula ninguna posición: se busca el literal que el propio verificador registró.
 */

import { construirPuente } from "./puente.js";

const ICONOS_AVISO = { info: "i", advertencia: "▲", critico: "✖" };

/**
 * Índice `texto_original` → `{fuente, estado}` a partir de las aserciones del turno.
 *
 * Solo se indexan los tokens monetarios (`cent:`): resaltar el año o el número de días
 * llena el párrafo de subrayados y tapa lo que importa, que es el dinero.
 */
export function indiceAserciones(gobernanza) {
  const indice = new Map();
  for (const asercion of (gobernanza && gobernanza.aserciones) || []) {
    if (!asercion || typeof asercion.token !== "string") continue;
    if (!asercion.token.startsWith("cent:")) continue;
    const literal = String(asercion.texto_original || "").trim();
    if (!literal) continue;
    if (!indice.has(literal)) {
      indice.set(literal, { fuente: asercion.fuente || null, estado: asercion.estado || "ANCLADA" });
    }
  }
  return indice;
}

/** Envuelve en `<button>` cada cifra reconocida del párrafo; el resto va como texto. */
function resaltarCifras(texto, indice, alPulsarFacto) {
  const fragmento = document.createDocumentFragment();
  const literales = [...indice.keys()].filter(Boolean).sort((a, b) => b.length - a.length);
  if (literales.length === 0) {
    fragmento.append(document.createTextNode(texto));
    return fragmento;
  }

  let buffer = "";
  let posicion = 0;
  const vaciar = () => {
    if (buffer) {
      fragmento.append(document.createTextNode(buffer));
      buffer = "";
    }
  };

  while (posicion < texto.length) {
    const literal = literales.find((candidato) => texto.startsWith(candidato, posicion));
    if (!literal) {
      buffer += texto[posicion];
      posicion += 1;
      continue;
    }
    vaciar();
    const { fuente, estado } = indice.get(literal);
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = estado === "NO_ANCLADA" ? "cifra cifra--sin-anclar" : "cifra";
    boton.textContent = literal;
    boton.title =
      estado === "NO_ANCLADA"
        ? "Cifra SIN ANCLAR: no existe en el FactSet"
        : `Anclada en ${fuente || "el FactSet"}`;
    boton.setAttribute(
      "aria-label",
      `${literal}. ${estado === "NO_ANCLADA" ? "Cifra sin anclar." : "Ver el campo del FactSet que la respalda."}`,
    );
    boton.addEventListener("click", () => {
      if (typeof alPulsarFacto === "function") alPulsarFacto(fuente, literal, estado);
    });
    fragmento.append(boton);
    posicion += literal.length;
  }
  vaciar();
  return fragmento;
}

function conTitulo(contenedor, titulo) {
  if (!titulo) return;
  const rotulo = document.createElement("p");
  rotulo.className = "bloque__titulo";
  rotulo.textContent = titulo;
  contenedor.prepend(rotulo);
}

/* ------------------------------------------------------------------------- */
/* Un renderizador por tipo de bloque                                         */
/* ------------------------------------------------------------------------- */

function bloqueTexto(bloque, contexto) {
  const nodo = document.createElement("div");
  nodo.className = `bloque bloque--texto${bloque.enfasis ? " es-enfasis" : ""}`;
  const parrafo = document.createElement("p");
  parrafo.append(resaltarCifras(String(bloque.texto || ""), contexto.aserciones, contexto.alPulsarFacto));
  nodo.append(parrafo);
  conTitulo(nodo, bloque.titulo);
  return nodo;
}

function bloqueKV(bloque, contexto) {
  const nodo = document.createElement("div");
  nodo.className = "bloque bloque--kv";
  const lista = document.createElement("div");
  lista.className = "kv";

  for (const item of bloque.items || []) {
    const pulsable = Boolean(item.fact_id) && typeof contexto.alPulsarFacto === "function";
    const fila = document.createElement(pulsable ? "button" : "div");
    fila.className = "kv__fila";
    if (pulsable) {
      fila.type = "button";
      fila.title = `Anclado en ${item.fact_id}`;
      fila.addEventListener("click", () => contexto.alPulsarFacto(item.fact_id, item.clave, "ANCLADA"));
    }
    const clave = document.createElement("span");
    clave.className = "kv__clave";
    clave.textContent = item.clave;
    const valor = document.createElement("span");
    valor.className = "kv__valor";
    valor.textContent = item.valor;
    fila.append(clave, valor);
    lista.append(fila);
  }

  nodo.append(lista);
  conTitulo(nodo, bloque.titulo);
  return nodo;
}

function bloquePuente(bloque, contexto) {
  const nodo = document.createElement("div");
  nodo.className = "bloque bloque--puente";
  conTitulo(nodo, bloque.titulo);
  nodo.append(
    construirPuente(bloque, {
      alPulsar: (factId, etiqueta) => contexto.alPulsarFacto(factId, etiqueta, "ANCLADA"),
      animado: true,
    }),
  );
  return nodo;
}

function bloqueTabla(bloque) {
  const nodo = document.createElement("div");
  nodo.className = "bloque bloque--tabla";
  const envoltura = document.createElement("div");
  envoltura.className = "tabla-envoltura";
  const tabla = document.createElement("table");
  tabla.className = "tabla";

  if ((bloque.columnas || []).length) {
    const cabecera = document.createElement("thead");
    const fila = document.createElement("tr");
    for (const columna of bloque.columnas) {
      const celda = document.createElement("th");
      celda.scope = "col";
      celda.textContent = columna;
      fila.append(celda);
    }
    cabecera.append(fila);
    tabla.append(cabecera);
  }

  const cuerpo = document.createElement("tbody");
  for (const filaDatos of bloque.filas || []) {
    const fila = document.createElement("tr");
    for (const valor of filaDatos) {
      const celda = document.createElement("td");
      celda.textContent = valor;
      fila.append(celda);
    }
    cuerpo.append(fila);
  }
  tabla.append(cuerpo);
  envoltura.append(tabla);
  nodo.append(envoltura);

  if (bloque.nota) {
    const nota = document.createElement("p");
    nota.className = "tabla__nota";
    nota.textContent = bloque.nota;
    nodo.append(nota);
  }
  conTitulo(nodo, bloque.titulo);
  return nodo;
}

function bloqueAviso(bloque, contexto) {
  const severidad = bloque.severidad || "info";
  const nodo = document.createElement("div");
  nodo.className = `bloque bloque--aviso aviso aviso--${severidad}`;
  nodo.setAttribute("role", "note");

  const icono = document.createElement("span");
  icono.className = "aviso__icono";
  icono.setAttribute("aria-hidden", "true");
  icono.textContent = ICONOS_AVISO[severidad] || "i";

  const cuerpo = document.createElement("div");
  if (bloque.titulo) {
    const titulo = document.createElement("p");
    titulo.className = "bloque__titulo";
    titulo.textContent = bloque.titulo;
    cuerpo.append(titulo);
  }
  const parrafo = document.createElement("p");
  parrafo.append(resaltarCifras(String(bloque.texto || ""), contexto.aserciones, contexto.alPulsarFacto));
  cuerpo.append(parrafo);

  nodo.append(icono, cuerpo);
  return nodo;
}

function bloqueDesconocido(bloque) {
  const nodo = document.createElement("div");
  nodo.className = "bloque bloque--texto";
  const parrafo = document.createElement("p");
  parrafo.textContent =
    bloque.texto || bloque.nota || `[bloque de tipo "${bloque.tipo}" no soportado por esta consola]`;
  nodo.append(parrafo);
  conTitulo(nodo, bloque.titulo);
  return nodo;
}

const RENDERIZADORES = {
  texto: bloqueTexto,
  kv: bloqueKV,
  puente: bloquePuente,
  tabla: bloqueTabla,
  aviso: bloqueAviso,
};

/**
 * Renderiza la lista de bloques **en el orden en que llegó**.
 *
 * @param {Array} bloques `respuesta.bloques`
 * @param {{aserciones: Map, alPulsarFacto: Function}} contexto
 * @returns {DocumentFragment}
 */
export function renderizarBloques(bloques, contexto) {
  const fragmento = document.createDocumentFragment();
  for (const bloque of bloques || []) {
    const renderizador = RENDERIZADORES[bloque.tipo] || bloqueDesconocido;
    fragmento.append(renderizador(bloque, contexto));
  }
  return fragmento;
}

/**
 * Botonera con las acciones que sugirió la respuesta. Ninguna es irreversible.
 *
 * @param {Array} acciones `respuesta.acciones`
 * @param {(accion:object)=>void} alPulsar
 */
export function renderizarAcciones(acciones, alPulsar) {
  if (!acciones || acciones.length === 0) return null;
  const barra = document.createElement("div");
  barra.className = "acciones";
  for (const accion of acciones) {
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = `boton boton--accion${accion.id === "DERIVAR_ASESOR" ? " es-derivar" : ""}`;
    boton.textContent = accion.etiqueta;
    boton.dataset.accion = accion.id;
    boton.title = `${accion.id} · ${accion.riesgo}`;
    boton.addEventListener("click", () => alPulsar(accion));
    barra.append(boton);
  }
  return barra;
}

/** Brief de siete líneas para el asesor, en monoespaciado y sin reformatear. */
export function renderizarBrief(resumenAsesor, { contextRef = null, cola = null } = {}) {
  const caja = document.createElement("div");
  caja.className = "brief";

  const cabecera = document.createElement("div");
  cabecera.className = "brief__cabecera";
  const izquierda = document.createElement("span");
  izquierda.textContent = `Contexto para el asesor${cola ? ` · ${cola}` : ""}`;
  cabecera.append(izquierda);
  if (contextRef) {
    const referencia = document.createElement("span");
    referencia.className = "brief__ref";
    referencia.textContent = contextRef;
    cabecera.append(referencia);
  }

  const texto = document.createElement("pre");
  texto.className = "brief__texto";
  texto.textContent = resumenAsesor || "";

  caja.append(cabecera, texto);
  return caja;
}
