/**
 * El puente: gráfico de cascada del recibo previo al actual, dibujado a mano en SVG.
 *
 * Sin librerías. Una fila por barra del bloque `puente` que envía el backend, en el
 * mismo orden y con los mismos importes: barra de entrada (recibo anterior), una barra
 * por causa agregada y barra de total (recibo actual). Este módulo **no decide nada**;
 * si el backend no manda el bloque, no hay puente.
 *
 * Orientación horizontal a propósito: las etiquetas de causa son largas ("ajustes por
 * días de suspensión") y en vertical habría que rotarlas, que es justo lo que no se
 * puede leer proyectado en una sala.
 *
 * La animación (una barra tras otra) es el momento de la demo: se activa añadiendo la
 * clase `es-dibujado` en el siguiente cuadro, y `prefers-reduced-motion` la desactiva
 * desde el CSS.
 */

import { formatearSoles, formatearSignado } from "./formato.js";

const NS = "http://www.w3.org/2000/svg";

const ANCHO = 640;
const MARGEN_SUP = 12;
const MARGEN_INF = 10;
const ALTO_FILA = 38;
const ALTO_BARRA = 20;
const X_ETIQUETA = 186;
const X_BARRA_INI = 198;
const X_BARRA_FIN = 506;
const X_MONTO = 636;
const RETRASO_MS = 95;

const crear = (nombre, atributos = {}) => {
  const nodo = document.createElementNS(NS, nombre);
  for (const [clave, valor] of Object.entries(atributos)) {
    if (valor !== null && valor !== undefined) nodo.setAttribute(clave, String(valor));
  }
  return nodo;
};

/** Etiqueta recortada al ancho de la columna, con el texto íntegro en el `<title>`. */
function recortar(texto, tope) {
  if (texto.length <= tope) return texto;
  return `${texto.slice(0, tope - 1).trimEnd()}…`;
}

/**
 * Calcula el tramo (desde, hasta) de cada barra en el eje de importes.
 *
 * `entrada` y `total` arrancan en cero; los incrementos y decrementos se apilan sobre el
 * acumulado, que es lo que convierte la lista de causas en una cascada.
 */
function calcularSegmentos(barras) {
  let acumulado = 0;
  return barras.map((barra) => {
    const monto = Number(barra.monto_cent) || 0;
    let desde;
    let hasta;
    if (barra.tipo === "entrada") {
      desde = 0;
      hasta = monto;
      acumulado = monto;
    } else if (barra.tipo === "total" || barra.tipo === "proyeccion") {
      desde = 0;
      hasta = monto;
    } else {
      desde = acumulado;
      hasta = acumulado + monto;
      acumulado = hasta;
    }
    return { barra, monto, desde, hasta, acumulado };
  });
}

/**
 * Construye el `<figure>` con la cascada.
 *
 * @param {{titulo?:string, barras:Array}} bloque bloque `puente` tal como llega del API.
 * @param {{alPulsar?:(factId:string, etiqueta:string)=>void, animado?:boolean}} opciones
 * @returns {HTMLElement}
 */
export function construirPuente(bloque, { alPulsar = null, animado = true } = {}) {
  const barras = Array.isArray(bloque.barras) ? bloque.barras : [];
  const figura = document.createElement("figure");
  figura.className = "puente";
  figura.style.margin = "0";

  if (barras.length === 0) {
    const vacio = document.createElement("p");
    vacio.className = "puente__pie";
    vacio.textContent = "Este recibo no trae causas agregadas: no hay puente que dibujar.";
    figura.append(vacio);
    return figura;
  }

  const segmentos = calcularSegmentos(barras);
  const valores = [0];
  for (const segmento of segmentos) valores.push(segmento.desde, segmento.hasta);
  const minimo = Math.min(...valores);
  const maximo = Math.max(...valores);
  const rango = maximo - minimo || 1;
  const escala = (valor) => X_BARRA_INI + ((valor - minimo) / rango) * (X_BARRA_FIN - X_BARRA_INI);

  const alto = MARGEN_SUP + segmentos.length * ALTO_FILA + MARGEN_INF;
  const envoltura = document.createElement("div");
  envoltura.className = "puente__envoltura";

  const lienzo = crear("svg", {
    class: `puente__lienzo${animado ? "" : " es-dibujado"}`,
    viewBox: `0 0 ${ANCHO} ${alto}`,
    role: "img",
    "aria-label": `Cascada del recibo: ${segmentos
      .map((s) => `${s.barra.etiqueta}, ${formatearSoles(s.monto)}`)
      .join("; ")}`,
  });
  if (animado) lienzo.classList.add("puente--animado");

  // Eje de origen: de dónde parte todo importe.
  lienzo.append(
    crear("line", {
      class: "puente__base",
      x1: escala(0), y1: MARGEN_SUP - 4,
      x2: escala(0), y2: alto - MARGEN_INF + 2,
    }),
  );

  segmentos.forEach((segmento, indice) => {
    const { barra, monto, desde, hasta } = segmento;
    const yFila = MARGEN_SUP + indice * ALTO_FILA;
    const yBarra = yFila + (ALTO_FILA - ALTO_BARRA) / 2;
    const xIzquierda = escala(Math.min(desde, hasta));
    const anchoBarra = Math.max(2.5, Math.abs(escala(hasta) - escala(desde)));
    const creciente = hasta >= desde;

    const fila = crear("g", {
      class: `puente__fila puente__fila--${barra.tipo}`,
      "data-fact-id": barra.fact_id || "",
    });

    const titulo = crear("title");
    titulo.textContent = `${barra.etiqueta}: ${formatearSoles(monto)}`;
    fila.append(titulo);

    // Zona sensible: todo el ancho de la fila responde al puntero y al teclado.
    fila.append(crear("rect", {
      class: "puente__zona",
      x: 0, y: yFila, width: ANCHO, height: ALTO_FILA, rx: 4,
    }));

    const etiqueta = String(barra.etiqueta || "");
    const largo = etiqueta.length > 26;
    fila.append(
      (() => {
        const texto = crear("text", {
          class: "puente__etiqueta",
          x: X_ETIQUETA, y: yFila + ALTO_FILA / 2 + 4.5,
          "text-anchor": "end",
          "font-size": largo ? 11.5 : 13,
        });
        texto.textContent = recortar(etiqueta, largo ? 34 : 26);
        return texto;
      })(),
    );

    const rectangulo = crear("rect", {
      class: "puente__barra",
      x: xIzquierda, y: yBarra,
      width: anchoBarra, height: ALTO_BARRA,
      rx: 3,
    });
    rectangulo.style.transformOrigin = creciente ? "left center" : "right center";
    rectangulo.style.transitionDelay = `${indice * RETRASO_MS}ms`;
    fila.append(rectangulo);

    const montoTexto = crear("text", {
      class: "puente__monto",
      x: X_MONTO, y: yFila + ALTO_FILA / 2 + 4.5,
      "text-anchor": "end",
    });
    // La entrada y el total son saldos; las causas son variaciones y llevan signo.
    montoTexto.textContent =
      barra.tipo === "entrada" || barra.tipo === "total" || barra.tipo === "proyeccion"
        ? formatearSoles(monto)
        : formatearSignado(monto);
    montoTexto.style.transitionDelay = `${indice * RETRASO_MS + 120}ms`;
    fila.append(montoTexto);

    // Conector con la fila siguiente cuando esta se apila sobre el acumulado.
    const siguiente = segmentos[indice + 1];
    if (siguiente && siguiente.barra.tipo !== "entrada" && siguiente.barra.tipo !== "total"
        && siguiente.barra.tipo !== "proyeccion") {
      const conector = crear("line", {
        class: "puente__conector",
        x1: escala(hasta), y1: yBarra + ALTO_BARRA,
        x2: escala(hasta), y2: yFila + ALTO_FILA + (ALTO_FILA - ALTO_BARRA) / 2,
      });
      conector.style.transitionDelay = `${indice * RETRASO_MS + 160}ms`;
      fila.append(conector);
    }

    if (barra.fact_id && typeof alPulsar === "function") {
      fila.setAttribute("tabindex", "0");
      fila.setAttribute("role", "button");
      fila.setAttribute(
        "aria-label",
        `${barra.etiqueta}: ${formatearSoles(monto)}. Ver de qué campo del FactSet sale.`,
      );
      const abrir = () => alPulsar(barra.fact_id, barra.etiqueta);
      fila.addEventListener("click", abrir);
      fila.addEventListener("keydown", (evento) => {
        if (evento.key === "Enter" || evento.key === " ") {
          evento.preventDefault();
          abrir();
        }
      });
    }

    lienzo.append(fila);
  });

  envoltura.append(lienzo);
  figura.append(envoltura);

  const pie = document.createElement("figcaption");
  pie.className = "puente__pie";
  pie.textContent =
    "Cada barra es un importe entero del FactSet: el puente no puede introducir una cifra nueva.";
  figura.append(pie);

  return figura;
}

/** Dispara la animación una vez que el nodo ya está en el documento. */
export function animarPuente(figura) {
  const lienzo = figura.querySelector(".puente__lienzo");
  if (!lienzo || !lienzo.classList.contains("puente--animado")) return;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => lienzo.classList.add("es-dibujado"));
  });
}
