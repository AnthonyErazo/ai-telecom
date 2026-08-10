/**
 * Lectura del FactSet desde el navegador: resolver un `fact_id` a su valor real.
 *
 * Un `fact_id` es la ruta legible que el backend usa para anclar cada cifra
 * (`FactSet.mapa_tokens()` en `packages/core_domain/esquemas/factset.py`). La gramática
 * completa, tal como la emite el motor:
 *
 *   factset:<campo>            · totales, días de ciclo, periodos y fechas
 *   factset:<fecha>.anio|.dia  · sub-campos derivados de una fecha
 *   invariante:<campo>         · residual_cent, suma_deltas_cent, delta_total_cent
 *   linea:<concepto_id>.<campo>
 *   tramo:<concepto_id>#<i>.<campo>
 *   causa:<CLAVE>.<campo>      · CLAVE = causa | causa_oficial | SIN_CAUSA
 *   financiamiento:<equipo>.<campo>
 *   cat:<concepto_id>          · viene de GET /v1/evidencia, no del FactSet
 *
 * Aquí NO se calcula nada que el backend no haya calculado: se lee el JSON que devolvió
 * `GET /v1/hechos`. La única excepción es `total_a_pagar_cent`, que en el modelo es una
 * `@property` (`total_actual_cent + deuda_anterior_cent`) y por eso no viaja en el JSON;
 * se recompone y se marca como derivada.
 */

import { formatearSoles, periodoLegible, fechaLegible, porcentajeBp } from "./formato.js";

/** Rótulos en lenguaje de negocio para los campos que se citan más. */
const DESCRIPCIONES = {
  "factset:total_actual_cent": "Total del recibo del periodo actual",
  "factset:total_previo_cent": "Total del recibo del periodo anterior",
  "factset:delta_total_cent": "Diferencia entre los dos recibos",
  "factset:deuda_anterior_cent": "Saldo pendiente de recibos anteriores",
  "factset:total_a_pagar_cent": "Total del periodo más la deuda anterior (campo derivado)",
  "factset:dias_ciclo": "Días que dura el ciclo de facturación",
  "factset:periodo_actual": "Periodo que se está explicando",
  "factset:periodo_previo": "Periodo con el que se compara",
  "factset:ciclo_inicio": "Primer día del ciclo facturado",
  "factset:ciclo_fin": "Fin del ciclo facturado (exclusivo)",
  "factset:fecha_vencimiento": "Fecha de vencimiento del recibo",
  "factset:confianza_global": "Confianza global de la atribución de causas",
  "invariante:residual_cent": "Residual de conciliación (debe ser 0, tolerancia ±1 céntimo)",
  "invariante:suma_deltas_cent": "Suma de las variaciones por concepto",
  "invariante:delta_total_cent": "Diferencia entre totales que debe reproducirse",
};

const SUFIJOS = {
  monto_actual_cent: "importe de este periodo",
  monto_previo_cent: "importe del periodo anterior",
  delta_cent: "variación del concepto",
  monto_cent: "importe atribuido a la causa",
  participacion: "peso de la causa sobre la variación total",
  confianza: "confianza de la atribución",
  dias_prorrateo: "días prorrateados",
  cuota_numero: "número de cuota",
  cuotas_totales: "cuotas totales del financiamiento",
  dias: "días del tramo",
  tarifa_mensual_cent: "tarifa mensual del tramo",
  monto_prorrateado_cent: "importe cobrado por el tramo",
  descuento_cent: "descuento aplicado al tramo",
  inicio: "inicio del tramo",
  fin: "fin del tramo (exclusivo)",
  fin_inclusivo: "último día del tramo",
  principal_cent: "capital financiado",
  saldo_final_cent: "saldo tras la cuota",
};

const esMonetario = (campo) => typeof campo === "string" && campo.endsWith("_cent");
const esFecha = (valor) => typeof valor === "string" && /^\d{4}-\d{2}-\d{2}/.test(valor);
const esPeriodo = (valor) => typeof valor === "string" && /^\d{4}-\d{2}$/.test(valor);

/** Clave con la que el motor agrupa una causa: `causa || causa_oficial || SIN_CAUSA`. */
export function claveDeCausa(causa) {
  return causa.causa || causa.causa_oficial || "SIN_CAUSA";
}

function formatearValor(valor, campo) {
  if (valor === null || valor === undefined) return "—";
  if (esMonetario(campo)) return formatearSoles(valor);
  if (campo === "participacion" && typeof valor === "number") return porcentajeBp(valor);
  if (campo === "confianza" || campo === "confianza_global") return `${(Number(valor) * 100).toFixed(0)} %`;
  if (esPeriodo(valor)) return periodoLegible(valor);
  if (esFecha(valor)) return fechaLegible(valor);
  if (Array.isArray(valor)) return `${valor.length} elemento(s)`;
  if (typeof valor === "object") return JSON.stringify(valor);
  return String(valor);
}

/**
 * Resuelve un `fact_id` contra el FactSet.
 *
 * @returns {{factId:string, ambito:string, referencia:string|null, campo:string|null,
 *            valor:*, texto:string, descripcion:string, resuelto:boolean}}
 */
export function resolverFacto(factset, factId) {
  const salida = {
    factId,
    ambito: "",
    referencia: null,
    campo: null,
    valor: null,
    texto: "—",
    descripcion: DESCRIPCIONES[factId] || "",
    resuelto: false,
  };
  if (!factId || typeof factId !== "string") return salida;

  const corte = factId.indexOf(":");
  if (corte < 0) return salida;
  salida.ambito = factId.slice(0, corte);
  const resto = factId.slice(corte + 1);

  if (!factset) return salida;

  const partes = resto.split(".");
  const cabeza = partes[0];
  const campo = partes.length > 1 ? partes[partes.length - 1] : null;
  salida.campo = campo;

  const terminar = (valor, referencia, campoUsado) => {
    salida.valor = valor;
    salida.referencia = referencia ?? null;
    salida.resuelto = valor !== undefined && valor !== null;
    salida.texto = formatearValor(valor, campoUsado || campo || cabeza);
    if (!salida.descripcion && (campoUsado || campo)) {
      salida.descripcion = SUFIJOS[campoUsado || campo] || "";
    }
    return salida;
  };

  switch (salida.ambito) {
    case "factset": {
      if (cabeza === "total_a_pagar_cent") {
        // @property del modelo: no viaja en el JSON, se recompone.
        const valor = (factset.total_actual_cent || 0) + (factset.deuda_anterior_cent || 0);
        return terminar(valor, "FactSet", "total_a_pagar_cent");
      }
      const base = factset[cabeza];
      if (partes.length === 2 && (campo === "anio" || campo === "dia")) {
        if (typeof base === "string") {
          const valor = campo === "anio" ? Number(base.slice(0, 4)) : Number(base.slice(8, 10));
          salida.descripcion = campo === "anio" ? `Año de ${cabeza}` : `Día de ${cabeza}`;
          return terminar(valor, "FactSet", cabeza);
        }
        return salida;
      }
      return terminar(base, "FactSet", cabeza);
    }

    case "invariante": {
      const invariante = factset.invariante || {};
      return terminar(invariante[cabeza], "Invariante de conciliación", cabeza);
    }

    case "linea": {
      const linea = (factset.lineas || []).find((item) => item.concepto_id === cabeza);
      if (!linea) return salida;
      salida.descripcion = salida.descripcion || `${linea.nombre_comercial}: ${SUFIJOS[campo] || campo || ""}`;
      return terminar(campo ? linea[campo] : linea, linea.nombre_comercial, campo);
    }

    case "tramo": {
      const [conceptoId, indice] = cabeza.split("#");
      const linea = (factset.lineas || []).find((item) => item.concepto_id === conceptoId);
      const tramo = linea && (linea.tramos || [])[Number(indice)];
      if (!tramo) return salida;
      salida.descripcion = salida.descripcion || `${tramo.etiqueta}: ${SUFIJOS[campo] || campo || ""}`;
      return terminar(campo ? tramo[campo] : tramo, tramo.etiqueta, campo);
    }

    case "causa": {
      const causa = (factset.causas_agregadas || []).find((item) => claveDeCausa(item) === cabeza);
      if (!causa) return salida;
      salida.descripcion = salida.descripcion || `${causa.etiqueta_cliente}: ${SUFIJOS[campo] || campo || ""}`;
      const valor = campo === "participacion" ? causa.participacion_bp : causa[campo];
      return terminar(valor, causa.etiqueta_cliente, campo);
    }

    case "financiamiento": {
      const plan = (factset.financiamientos || []).find((item) => item.equipo === cabeza);
      if (!plan) return salida;
      return terminar(campo ? plan[campo] : plan, plan.equipo, campo);
    }

    case "cat":
      salida.referencia = cabeza;
      salida.descripcion = salida.descripcion || "Ficha del concepto en el catálogo";
      return salida;

    default:
      return salida;
  }
}

/** Indexa los items de `GET /v1/evidencia` por `fact_id` para el panel desplegable. */
export function indiceEvidencia(items) {
  const indice = new Map();
  for (const item of items || []) {
    if (!item.fact_id) continue;
    if (!indice.has(item.fact_id)) indice.set(item.fact_id, []);
    indice.get(item.fact_id).push(item);
  }
  return indice;
}

/**
 * Todos los importes en céntimos que el FactSet respalda, en valor con signo y absoluto.
 *
 * Es una réplica reducida del conjunto permitido del verificador
 * (`packages/llm_layer/verificador.py::construir_permitidos`) y **solo** se usa para la
 * simulación local de la alucinación cuando `/dev/alucinar` no está disponible. La
 * verificación de verdad la hace el backend, siempre.
 */
export function importesDelFactset(factset) {
  const valores = new Set();
  const anotar = (centimos) => {
    if (typeof centimos !== "number") return;
    valores.add(Math.trunc(centimos));
    valores.add(Math.abs(Math.trunc(centimos)));
  };
  if (!factset) return valores;

  anotar(factset.total_actual_cent);
  anotar(factset.total_previo_cent);
  anotar(factset.delta_total_cent);
  anotar(factset.deuda_anterior_cent);
  anotar((factset.total_actual_cent || 0) + (factset.deuda_anterior_cent || 0));
  const invariante = factset.invariante || {};
  anotar(invariante.residual_cent);
  anotar(invariante.suma_deltas_cent);
  anotar(invariante.delta_total_cent);
  for (const linea of factset.lineas || []) {
    anotar(linea.monto_actual_cent);
    anotar(linea.monto_previo_cent);
    anotar(linea.delta_cent);
    for (const tramo of linea.tramos || []) {
      anotar(tramo.tarifa_mensual_cent);
      anotar(tramo.monto_prorrateado_cent);
      anotar(tramo.descuento_cent);
    }
  }
  for (const causa of factset.causas_agregadas || []) anotar(causa.monto_cent);
  for (const plan of factset.financiamientos || []) {
    anotar(plan.principal_cent);
    for (const cuota of plan.cronograma || []) {
      anotar(cuota.monto_cent);
      anotar(cuota.saldo_final_cent);
    }
  }
  return valores;
}

/**
 * Plan B del modo adversario: envenena el texto **en el navegador**.
 *
 * Solo se usa si `POST /dev/alucinar` no existe (la API corre con `ENTORNO != dev` y
 * responde 404 `FUNCION_NO_DISPONIBLE`). Replica la estrategia de
 * `verificador.inyectar_alucinacion`: toma el primer importe del texto y lo sustituye
 * por otro del mismo orden de magnitud que **no** esté en el conjunto permitido. El
 * resultado se rotula siempre como simulación local: no es un veredicto del backend.
 */
export function simularAlucinacion(texto, factset, deltaCent = 731) {
  const permitidos = importesDelFactset(factset);
  const patron = /S\/\s?-?\d{1,3}(?:,\d{3})*\.\d{2}/;
  const encontrado = patron.exec(texto || "");
  const base = encontrado
    ? Math.round(Number(encontrado[0].replace(/[^\d.]/g, "")) * 100)
    : (factset && factset.total_actual_cent) || 0;

  let falso = base + deltaCent;
  let vueltas = 0;
  while (permitidos.has(falso) && vueltas < 50) {
    falso += deltaCent;
    vueltas += 1;
  }
  const escrito = formatearSoles(falso);
  const envenenado = encontrado
    ? texto.replace(patron, escrito)
    : `${texto}\n[DEMO ADVERSARIA] Además, este mes se le aplicó un cargo de ${escrito} por un servicio adicional.`;

  return {
    simulado: true,
    veredicto: "FAIL",
    infractores: [escrito],
    tokensInfractores: [`cent:${falso}`],
    noAncladas: 1,
    texto: envenenado,
  };
}
