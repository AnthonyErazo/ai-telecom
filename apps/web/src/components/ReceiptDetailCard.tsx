/**
 * ReceiptDetailCard — Tarjeta de desglose de factura
 *
 * Aparece como un mensaje especial dentro del chat de BillSense cuando el cliente
 * solicita ver el detalle de su factura, y como cuerpo principal de la pantalla
 * "Mi Recibo". Sigue la misma estructura con la que Movistar presenta un recibo al
 * cliente: cargos por categoría (Cargos Mensuales, Cargos Adicionales, Descuentos y
 * Bonificaciones, Redondeo, Notas de crédito y Notas de débito), deuda pasada y total a pagar,
 * más un resumen de cuenta (estado, vencimiento, código de pago).
 *
 * Cada categoría es la suma de líneas reales del `FactSet` (`agruparLineas`, en
 * `lib/recibo.ts`) — nada se calcula aquí salvo esa suma; ningún porcentaje ni
 * proporción se inventa.
 */
import { useState } from "react";
import {
  AlertCircle, ChevronDown, ChevronUp, CreditCard, Download,
  FileText, ShieldCheck, TrendingDown, TrendingUp,
} from "lucide-react";
import type { FactSet } from "../api/types";
import { agruparLineas } from "../lib/recibo";

const soles = (c: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(c / 100);

interface Props {
  hechos: FactSet;
  cuentaId?: string;
  onDownload: () => void;
  mostrarDetalleCargos?: boolean;
}

export function ReceiptDetailCard({
  hechos,
  cuentaId,
  onDownload,
  mostrarDetalleCargos = false,
}: Props) {
  const [grupoAbierto, setGrupoAbierto] = useState<string | null>(null);

  const total = hechos.total_actual_cent;
  const previo = hechos.total_previo_cent;
  const delta = hechos.delta_total_cent;
  const deudaAnterior = hechos.deuda_anterior_cent ?? 0;
  const totalAPagar = hechos.total_a_pagar_cent ?? (total + deudaAnterior);
  const sube = delta >= 0;

  const periodo = hechos.periodo_actual ?? "—";
  const vencimiento = hechos.fecha_vencimiento ? String(hechos.fecha_vencimiento) : null;
  // Sin objeto `Date`: comparar dos strings "YYYY-MM-DD" ordena igual que comparar
  // las fechas, y evita el desfase de huso horario de parsear una fecha con `Date`.
  const vencido = Boolean(vencimiento && vencimiento < new Date().toISOString().slice(0, 10));

  const grupos = agruparLineas(hechos.lineas);

  const maxVal = Math.max(total, previo) || 1;
  const pctCurr = (total / maxVal) * 100;
  const pctPrev = (previo / maxVal) * 100;

  return (
    <div className="mm-rdc">

      {/* ── 1. Cabecera ─────────────────────────────────────────── */}
      <div className="mm-rdc-header">
        <div className="mm-rdc-hdr-icon">
          <FileText size={18} />
        </div>
        <div className="mm-rdc-hdr-info">
          <p className="mm-rdc-hdr-title">Detalle de Facturación</p>
          <p className="mm-rdc-hdr-cycle">Ciclo: {periodo}</p>
        </div>
        <div className={`mm-rdc-delta ${sube ? "up" : "down"}`}>
          {sube ? <TrendingUp size={13} /> : <TrendingDown size={13} />}
          <span>{soles(Math.abs(delta))}</span>
        </div>
      </div>

      {/* ── 2. Hero — Total a pagar ──────────────────────────────── */}
      <div className="mm-rdc-hero">
        <p className="mm-rdc-hero-label">Total a Pagar</p>
        <p className="mm-rdc-hero-amount">{soles(totalAPagar)}</p>
        {vencimiento && (
          <div className={`mm-rdc-due${vencido ? " vencido" : ""}`}>
            <AlertCircle size={11} />
            <span>{vencido ? "Venció el" : "Vence"}: {vencimiento}</span>
          </div>
        )}
      </div>

      {/* ── 3. Desglose por categoría ───────────────────────────── */}
      <div className="mm-rdc-breakdown">
        {grupos.map((grupo) => {
          const abierto = mostrarDetalleCargos || grupoAbierto === grupo.grupo;
          const puedeAbrir = !mostrarDetalleCargos && grupo.lineas.length > 0;
          return (
            <div key={grupo.grupo}>
              <button
                type="button"
                className={`mm-rdc-bk-row cat${grupo.aFavor ? " credito" : ""}${puedeAbrir ? " clicable" : ""}`}
                onClick={() => puedeAbrir && setGrupoAbierto(abierto ? null : grupo.grupo)}
              >
                <span>{grupo.etiqueta}{puedeAbrir && (abierto ? <ChevronUp size={13} /> : <ChevronDown size={13} />)}</span>
                <span>{grupo.aFavor ? "− " : ""}{soles(Math.abs(grupo.monto_cent))}</span>
              </button>
              {abierto && (
                <div className="mm-rdc-bk-sub">
                  {grupo.lineas.map((linea, indice) => (
                    <div className="mm-rdc-bk-subrow" key={`${linea.concepto_id}-${indice}`}>
                      <span className="mm-rdc-bk-concepto">
                        <strong>{linea.nombre_comercial || linea.concepto_id}</strong>
                        {linea.cuota_numero != null && linea.cuotas_totales != null && (
                          <small>Cuota {linea.cuota_numero} de {linea.cuotas_totales}</small>
                        )}
                        {linea.dias_prorrateo != null && (
                          <small>{linea.dias_prorrateo} días prorrateados</small>
                        )}
                      </span>
                      <span>{soles(linea.monto_actual_cent)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {deudaAnterior > 0 && (
          <div className="mm-rdc-bk-row alert">
            <span>⚠ Deuda pasada</span>
            <span className="text-red">{soles(deudaAnterior)}</span>
          </div>
        )}
        <div className="mm-rdc-divider" />
        <div className="mm-rdc-bk-row total">
          <span>Total a pagar</span>
          <span>{soles(totalAPagar)}</span>
        </div>
      </div>

      {/* ── 4. Resumen de mi cuenta ─────────────────────────────── */}
      <div className="mm-rdc-cuenta">
        <p className="mm-rdc-cuenta-title">Resumen de mi cuenta</p>
        <div className="mm-rdc-cuenta-grid">
          <div>
            <small>Estado</small>
            <strong className={vencido ? "text-red" : "text-green"}>
              {vencido ? <><AlertCircle size={12} /> Vencido</> : <><ShieldCheck size={12} /> Vigente</>}
            </strong>
          </div>
          <div>
            <small>Vencimiento</small>
            <strong>{vencimiento ?? "—"}</strong>
          </div>
          {cuentaId && (
            <div>
              <small><CreditCard size={11} /> Código de pago</small>
              <strong>{cuentaId}</strong>
            </div>
          )}
          <div>
            <small>Total</small>
            <strong>{soles(totalAPagar)}</strong>
          </div>
        </div>
      </div>

      {/* ── 5. Comparativa gráfica ──────────────────────────────── */}
      <div className="mm-rdc-compare">
        <p className="mm-rdc-compare-label">Comparativa con el mes anterior</p>
        <div className="mm-rdc-bars">
          <div className="mm-rdc-bar-group">
            <div className="mm-rdc-bar-track">
              <div
                className="mm-rdc-bar-fill prev"
                style={{ width: `${pctPrev}%` }}
                title={soles(previo)}
              />
            </div>
            <span>Mes ant.</span>
            <strong>{soles(previo)}</strong>
          </div>
          <div className="mm-rdc-bar-group">
            <div className="mm-rdc-bar-track">
              <div
                className={`mm-rdc-bar-fill curr ${sube ? "up" : "down"}`}
                style={{ width: `${pctCurr}%` }}
                title={soles(total)}
              />
            </div>
            <span>Este mes</span>
            <strong>{soles(total)}</strong>
          </div>
        </div>
      </div>

      {/* ── 6. Botón PDF ────────────────────────────────────────── */}
      <button className="mm-rdc-pdf" onClick={onDownload}>
        <Download size={14} /> Descargar PDF detallado
      </button>
    </div>
  );
}
