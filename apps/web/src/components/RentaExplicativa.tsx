/**
 * RentaExplicativa — "Cómo se calcula tu recibo"
 *
 * Traduce el `FactSet` que ya trajo la sesión (modalidad_renta, ciclo, tramos por
 * línea, causas agregadas) a una vista que un cliente entiende sin jerga: qué tipo
 * de renta tiene, cuándo corre su ciclo, y por qué cada línea prorrateada tiene el
 * importe que tiene. Ningún importe se calcula aquí — todo sale del FactSet tal
 * cual, igual que en `ReceiptDetailCard.tsx`; lo único que se arma en el navegador
 * es texto (fechas legibles, agrupaciones), nunca dinero.
 */
import {
  Bot, CalendarClock, CalendarDays, Layers, Mail, Sparkles,
} from "lucide-react";
import type { FactSet } from "../api/types";
import { iconoPara, money } from "./RichMessage";

const MESES = [
  "enero", "febrero", "marzo", "abril", "mayo", "junio",
  "julio", "agosto", "setiembre", "octubre", "noviembre", "diciembre",
];

/** Parsea "YYYY-MM-DD" a sus componentes sin pasar por `Date` local: un `new
 * Date("2026-07-16")` se interpreta como medianoche UTC, y en un huso negativo
 * (Perú, UTC-5) `toLocaleDateString` lo muestra un día antes. Aquí no hace falta
 * el objeto `Date` para solo mostrar la fecha. */
function partes(iso: string): { dia: number; mes: number; anio: number } | null {
  const [anio, mes, dia] = iso.split("-").map(Number);
  if (!anio || !mes || !dia) return null;
  return { dia, mes, anio };
}

function legible(iso?: string | null): string | null {
  const p = iso ? partes(iso) : null;
  if (!p) return null;
  const anioActual = new Date().getFullYear();
  return `${p.dia} de ${MESES[p.mes - 1]}${p.anio !== anioActual ? ` de ${p.anio}` : ""}`;
}

/** Misma fecha, `dias` antes — para la fecha aproximada de envío del recibo (10
 * días antes del vencimiento). Se calcula en UTC para no arrastrar el mismo
 * desfase de huso horario que `legible()` evita. */
function menosDias(iso: string, dias: number): string {
  const p = partes(iso);
  if (!p) return "";
  const fecha = new Date(Date.UTC(p.anio, p.mes - 1, p.dia));
  fecha.setUTCDate(fecha.getUTCDate() - dias);
  const anioActual = new Date().getFullYear();
  const anio = fecha.getUTCFullYear();
  return `${fecha.getUTCDate()} de ${MESES[fecha.getUTCMonth()]}${anio !== anioActual ? ` de ${anio}` : ""}`;
}

export function RentaExplicativa({
  hechos,
  onPreguntar,
}: {
  hechos: FactSet;
  onPreguntar: (texto: string) => void;
}) {
  const esAdelantada = hechos.modalidad_renta === "ADELANTADA";
  const inicio = legible(hechos.ciclo_inicio);
  const fin = legible(hechos.ciclo_fin);
  const vence = legible(hechos.fecha_vencimiento);
  const envioAprox = hechos.fecha_vencimiento ? menosDias(hechos.fecha_vencimiento, 10) : null;

  const lineasConTramos = (hechos.lineas ?? []).filter((l) => (l.tramos?.length ?? 0) > 1);
  const causas = hechos.causas_agregadas ?? [];
  const beneficios = hechos.beneficios_vigentes ?? [];
  const financiamientos = hechos.financiamientos ?? [];

  return (
    <div className="mm-renta-body">
      {/* ── 1. Tipo de renta ─────────────────────────────────────────── */}
      <div className={`mm-renta-tipo ${esAdelantada ? "ra" : "rv"}`}>
        <div className="mm-renta-tipo-icon">{esAdelantada ? <Sparkles size={22} /> : <CalendarClock size={22} />}</div>
        <div>
          <span className="mm-renta-tipo-badge">{esAdelantada ? "Renta Adelantada (RA)" : "Renta Vencida (RV)"}</span>
          <p>
            {esAdelantada
              ? "Tu recibo cobra por adelantado: este pago corresponde al servicio que vas a disfrutar en el ciclo que empieza, no al que ya terminó."
              : "Tu recibo cobra por lo ya disfrutado: primero usas el servicio durante el ciclo, y al terminar se te factura por esos días."}
          </p>
        </div>
      </div>

      {/* ── 2. Ciclo de facturación ──────────────────────────────────── */}
      {(inicio || fin || vence) && (
        <div className="mm-renta-card">
          <p className="mm-renta-card-title"><CalendarDays size={15} /> Tu ciclo de facturación</p>
          <div className="mm-renta-ciclo-line">
            {inicio && <div className="mm-renta-ciclo-step"><span className="dot start" /><small>Inicio</small><strong>{inicio}</strong></div>}
            <div className="mm-renta-ciclo-bar" />
            {fin && <div className="mm-renta-ciclo-step"><span className="dot end" /><small>Fin</small><strong>{fin}</strong></div>}
          </div>
          {vence && <div className="mm-renta-ciclo-row"><CalendarClock size={13} /><span>Vencimiento del pago</span><strong>{vence}</strong></div>}
          {envioAprox && <div className="mm-renta-ciclo-row muted"><Mail size={13} /><span>Envío aprox. del recibo</span><strong>{envioAprox}</strong></div>}
          {typeof hechos.dias_ciclo === "number" && <p className="mm-renta-card-nota">Ciclo de {hechos.dias_ciclo} días.</p>}
        </div>
      )}

      {/* ── 3. Por qué estos cargos (tramos de prorrateo reales) ───────── */}
      {lineasConTramos.length > 0 && (
        <div className="mm-renta-card">
          <p className="mm-renta-card-title"><Layers size={15} /> Por qué estos cargos tienen ese importe</p>
          <p className="mm-renta-card-nota">
            Cuando un servicio cambia a mitad de ciclo (alta nueva, cambio de plan, corte y
            reconexión), el cobro se divide en tramos: cada uno con su tarifa y sus días.
          </p>
          {lineasConTramos.map((linea) => (
            <div className="mm-renta-linea" key={linea.concepto_id}>
              <strong className="mm-renta-linea-nombre">{linea.nombre_comercial}</strong>
              <div className="mm-renta-tramos">
                {(linea.tramos ?? []).map((tramo, indice) => (
                  <div className={`mm-renta-tramo${tramo.estado === "SUSPENDIDO" ? " suspendido" : ""}`} key={`${linea.concepto_id}-${indice}`}>
                    <div className="mm-renta-tramo-etq">
                      <span>{tramo.etiqueta}</span>
                      {tramo.estado === "SUSPENDIDO" && <span className="mm-renta-tramo-tag">Sin servicio</span>}
                    </div>
                    <div className="mm-renta-tramo-cifras">
                      <span>{tramo.dias} día{tramo.dias === 1 ? "" : "s"}</span>
                      <strong>{tramo.facturable ? money(tramo.monto_prorrateado_cent) : "S/ 0.00"}</strong>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── 4. Causas de la variación ────────────────────────────────── */}
      {causas.length > 0 && (
        <div className="mm-renta-card">
          <p className="mm-renta-card-title">Causas de la variación</p>
          {causas.map((causa) => {
            const Icono = iconoPara(causa.etiqueta_cliente, causa.causa_oficial, causa.causa);
            return (
              <div className="mm-renta-causa" key={causa.etiqueta_cliente}>
                <Icono size={15} className="mm-rich-icon" />
                <span>{causa.etiqueta_cliente}</span>
                <div className="mm-renta-causa-cifras">
                  <strong>{money(Math.abs(causa.monto_cent))}</strong>
                  <small>{(causa.participacion_bp / 100).toFixed(0)}%</small>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── 5. Beneficios vigentes / financiamiento ─────────────────── */}
      {(beneficios.length > 0 || financiamientos.length > 0) && (
        <div className="mm-renta-card">
          {beneficios.length > 0 && (
            <>
              <p className="mm-renta-card-title">Beneficios vigentes</p>
              <div className="mm-renta-chips">
                {beneficios.map((beneficio) => <span className="mm-renta-chip" key={beneficio}>{beneficio}</span>)}
              </div>
            </>
          )}
          {financiamientos.map((plan) => {
            const siguiente = plan.cronograma[0];
            return (
              <div className="mm-renta-financiamiento" key={plan.equipo}>
                <strong>{plan.equipo}</strong>
                {siguiente && <span>Cuota {siguiente.numero} de {siguiente.de_total} · {money(siguiente.monto_cent)}</span>}
              </div>
            );
          })}
        </div>
      )}

      {/* ── 6. CTA a BillSense ───────────────────────────────────────── */}
      <button
        className="mm-btn-primary"
        onClick={() => onPreguntar(
          lineasConTramos.length > 0
            ? "Explícame el prorrateo de mi recibo"
            : "Explícame por qué mi recibo cambió este mes"
        )}
      >
        <Bot size={18} /> Pregúntale a BillSense sobre esto
      </button>
    </div>
  );
}
