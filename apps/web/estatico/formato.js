/**
 * Formato de importes, periodos y fechas.
 *
 * Regla innegociable del proyecto: **todo monto es un entero en céntimos**. Aquí no se
 * hace aritmética de dinero con decimales; solo se convierte un entero a la escritura
 * peruana. `formatearSoles` replica exactamente `packages/core_domain/dinero.py`:
 * separador de millar `,`, separador decimal `.` y el signo DELANTE del símbolo
 * (`-S/ 20.29`), como en los recibos peruanos.
 */

export const CENTIMOS_POR_SOL = 100;
export const SIMBOLO_MONEDA = "S/";

const MESES = [
  "enero", "febrero", "marzo", "abril", "mayo", "junio",
  "julio", "agosto", "setiembre", "octubre", "noviembre", "diciembre",
];

/** `123450 -> "1,234.50"`. El signo se antepone al número. */
export function formatearNumero(centimos) {
  const entero = Math.trunc(Number(centimos) || 0);
  const signo = entero < 0 ? "-" : "";
  const absoluto = Math.abs(entero);
  const soles = Math.floor(absoluto / CENTIMOS_POR_SOL);
  const resto = absoluto % CENTIMOS_POR_SOL;
  const conMillar = String(soles).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${signo}${conMillar}.${String(resto).padStart(2, "0")}`;
}

/** `123450 -> "S/ 1,234.50"`, `-2029 -> "-S/ 20.29"`. */
export function formatearSoles(centimos) {
  const entero = Math.trunc(Number(centimos) || 0);
  const signo = entero < 0 ? "-" : "";
  return `${signo}${SIMBOLO_MONEDA} ${formatearNumero(Math.abs(entero))}`;
}

/** Igual que `formatearSoles` pero marcando explícitamente el `+` de una subida. */
export function formatearSignado(centimos) {
  const entero = Math.trunc(Number(centimos) || 0);
  if (entero > 0) return `+${formatearSoles(entero)}`;
  return formatearSoles(entero);
}

/** `"2026-07" -> "julio de 2026"`. Devuelve el original si no encaja el patrón. */
export function periodoLegible(periodo) {
  if (typeof periodo !== "string") return "";
  const encontrado = /^(\d{4})-(\d{2})$/.exec(periodo.trim());
  if (!encontrado) return periodo;
  const mes = MESES[Number(encontrado[2]) - 1];
  return mes ? `${mes} de ${encontrado[1]}` : periodo;
}

/** `"2026-08-13" -> "13/08/2026"`. Acepta también fechas con hora (ISO). */
export function fechaLegible(iso) {
  if (typeof iso !== "string" || iso.length < 10) return "";
  const [anio, mes, dia] = iso.slice(0, 10).split("-");
  if (!anio || !mes || !dia) return iso;
  return `${dia}/${mes}/${anio}`;
}

/** Marca de hora corta `HH:MM:SS` para la bitácora. */
export function horaLegible(iso) {
  if (typeof iso !== "string") return "";
  const fecha = new Date(iso);
  if (Number.isNaN(fecha.getTime())) return iso;
  return fecha.toLocaleTimeString("es-PE", { hour12: false });
}

/** Puntos básicos a porcentaje legible: `8473 -> "84.73 %"`. */
export function porcentajeBp(bp) {
  const valor = Number(bp) || 0;
  return `${(valor / 100).toFixed(2)} %`;
}

/** Fracción 0..1 a porcentaje: `0.98 -> "98 %"`. */
export function porcentajeFraccion(fraccion) {
  const valor = Number(fraccion);
  if (!Number.isFinite(valor)) return "—";
  return `${Math.round(valor * 100)} %`;
}

/** Recorta un hash largo para que quepa en una tarjeta: `3227801e…83a4aa`. */
export function hashCorto(hash, cabeza = 8, cola = 6) {
  if (typeof hash !== "string" || hash.length <= cabeza + cola + 1) return hash || "—";
  return `${hash.slice(0, cabeza)}…${hash.slice(-cola)}`;
}

/** Convierte `CAMBIO_PLAN` en `Cambio plan` para rótulos de la interfaz. */
export function humanizarCodigo(codigo) {
  if (!codigo) return "";
  const limpio = String(codigo).replace(/_/g, " ").toLowerCase();
  return limpio.charAt(0).toUpperCase() + limpio.slice(1);
}
