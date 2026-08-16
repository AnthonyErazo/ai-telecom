/**
 * Agrupación de líneas del recibo en las categorías con las que Movistar presenta
 * un recibo al cliente (Cargos Mensuales, Cargos Adicionales, Descuentos y
 * Bonificaciones, Redondeo, Notas de crédito y Notas de débito). Cada línea ya trae `familia`,
 * `causa` y `concepto_id` desde el `FactSet` — esto solo las clasifica, no calcula
 * ningún importe nuevo: cada grupo es la suma de `monto_actual_cent` reales.
 *
 * Compartido entre `ReceiptDetailCard` y el PDF que genera `MiMovistar` para que
 * las dos vistas cuenten exactamente la misma historia.
 */
import type { LineaFactSet } from "../api/types";

export type GrupoRecibo = "mensuales" | "adicionales" | "descuentos" | "redondeo" | "devoluciones" | "debitos";

export const ETIQUETA_GRUPO: Record<GrupoRecibo, string> = {
  mensuales: "Cargos Mensuales",
  adicionales: "Cargos Adicionales",
  descuentos: "Descuentos y Bonificaciones",
  redondeo: "Redondeo",
  devoluciones: "Notas de crédito",
  debitos: "Notas de débito",
};

const ORDEN_GRUPOS: GrupoRecibo[] = ["mensuales", "adicionales", "descuentos", "redondeo", "devoluciones", "debitos"];

//: Grupos cuyo importe resta al total (se pintan en verde / negativo).
export const GRUPOS_A_FAVOR: ReadonlySet<GrupoRecibo> = new Set(["descuentos", "devoluciones"]);

function grupoDe(linea: LineaFactSet): GrupoRecibo {
  if (linea.concepto_id === "REDONDEO") return "redondeo";
  if (linea.causa === "NOTA_CREDITO") return "devoluciones";
  if (linea.causa === "NOTA_DEBITO") return "debitos";
  if (linea.familia === "CREDITO") return "descuentos";
  if (linea.familia === "RECURRENTE") return "mensuales";
  // UNICO, AJUSTE, FINANCIAMIENTO, IMPUESTO y cualquier familia futura: un cargo
  // real que no encaja en las categorías anteriores es un cargo adicional, nunca
  // se descarta en silencio.
  return "adicionales";
}

export interface GrupoAgregado {
  grupo: GrupoRecibo;
  etiqueta: string;
  monto_cent: number;
  aFavor: boolean;
  lineas: LineaFactSet[];
}

export function agruparLineas(lineas: LineaFactSet[] | undefined): GrupoAgregado[] {
  const porGrupo = new Map<GrupoRecibo, LineaFactSet[]>();
  for (const linea of lineas ?? []) {
    const grupo = grupoDe(linea);
    const lista = porGrupo.get(grupo) ?? [];
    lista.push(linea);
    porGrupo.set(grupo, lista);
  }
  return ORDEN_GRUPOS.map((grupo) => {
    const lineasDelGrupo = porGrupo.get(grupo) ?? [];
    return {
      grupo,
      etiqueta: ETIQUETA_GRUPO[grupo],
      monto_cent: lineasDelGrupo.reduce((acumulado, linea) => acumulado + linea.monto_actual_cent, 0),
      aFavor: GRUPOS_A_FAVOR.has(grupo),
      lineas: lineasDelGrupo,
    };
  }).filter((agregado) => agregado.lineas.length > 0);
}
