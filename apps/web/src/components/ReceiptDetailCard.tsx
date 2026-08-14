/**
 * ReceiptDetailCard — Tarjeta de desglose de factura
 *
 * Aparece como un mensaje especial dentro del chat de BillSense cuando
 * el cliente solicita ver el detalle de su factura o cuando el bot
 * considera apropiado mostrarlo. Está diseñada para verse bien dentro
 * de un flujo de chat en móvil (max-width 85 % del ancho).
 *
 * Estructura (siguiendo anatomía real de factura telco):
 *   1. Cabecera    — ícono + título + período
 *   2. Hero         — total a pagar + vencimiento en rojo
 *   3. Timeline    — barra visual del ciclo de facturación
 *   4. Desglose    — filas contables (base + IGV + extras + total)
 *   5. Comparativa — mes anterior vs mes actual
 *   6. Acción      — botón "Descargar PDF original"
 */
import { AlertCircle, Download, FileText, TrendingDown, TrendingUp } from "lucide-react";
import type { FactSet } from "../api/types";

const soles = (c: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(c / 100);

interface Props {
  hechos: FactSet;
  onDownload: () => void;
}

export function ReceiptDetailCard({ hechos, onDownload }: Props) {
  /* ── Cálculo tributario (IGV Perú 18 %) ─────────────────────────
     La API entrega el total incluyendo impuesto. Separamos:
     base_imponible = total / 1.18  (redondeado a centésimos)
     igv            = total - base_imponible
  ──────────────────────────────────────────────────────────────── */
  const total         = hechos.total_actual_cent;
  const baseImponible = Math.round(total / 1.18);
  const igv           = total - baseImponible;
  const previo        = hechos.total_previo_cent;
  const delta         = hechos.delta_total_cent;
  const saldoAnt      = (hechos.deuda_anterior_cent as number | undefined) ?? 0;
  const prorrateo     = (hechos.prorrateo_cent    as number | undefined) ?? 0;
  const reconexion    = (hechos.reconexion_cent   as number | undefined) ?? 0;
  const sube          = delta >= 0;

  /* ── Datos de período ────────────────────────────────────────── */
  const periodo       = hechos.periodo_actual ?? "—";
  const vencimiento   = hechos.fecha_vencimiento ? String(hechos.fecha_vencimiento) : null;

  /* ── Porcentaje de la barra de comparativa ───────────────────── */
  const maxVal  = Math.max(total, previo) || 1;
  const pctCurr = (total  / maxVal) * 100;
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
        <p className="mm-rdc-hero-amount">{soles(total)}</p>
        {vencimiento && (
          <div className="mm-rdc-due">
            <AlertCircle size={11} />
            <span>Vence: {vencimiento}</span>
          </div>
        )}
      </div>

      {/* ── 3. Línea de tiempo del ciclo ────────────────────────── */}
      <div className="mm-rdc-timeline">
        <div className="mm-rdc-tl-row">
          {/* Inicio */}
          <div className="mm-rdc-tl-step">
            <div className="mm-rdc-tl-dot start" />
            <span>Inicio ciclo</span>
          </div>
          {/* Barra */}
          <div className="mm-rdc-tl-bar">
            <div className="mm-rdc-tl-fill" style={{ width: "55%" }} />
          </div>
          {/* Vencimiento */}
          <div className="mm-rdc-tl-step">
            <div className={`mm-rdc-tl-dot ${vencimiento ? "mid" : "start"}`} />
            <span>{vencimiento ? "Vencimiento" : "Pago"}</span>
          </div>
          {/* Barra corta */}
          <div className="mm-rdc-tl-bar short">
            <div className="mm-rdc-tl-fill danger" style={{ width: "80%" }} />
          </div>
          {/* Fin */}
          <div className="mm-rdc-tl-step">
            <div className="mm-rdc-tl-dot end" />
            <span>Fin ciclo</span>
          </div>
        </div>
        <p className="mm-rdc-tl-caption">Período facturado: {periodo}</p>
      </div>

      {/* ── 4. Desglose contable ────────────────────────────────── */}
      <div className="mm-rdc-breakdown">
        <div className="mm-rdc-bk-row subtle">
          <span>Saldo mes anterior</span>
          <span>{soles(saldoAnt)}</span>
        </div>
        <div className="mm-rdc-bk-row">
          <span>Servicios del mes (base imponible)</span>
          <span>{soles(baseImponible)}</span>
        </div>
        <div className="mm-rdc-bk-row tax">
          <span>IGV 18%</span>
          <span>{soles(igv)}</span>
        </div>
        {prorrateo > 0 && (
          <div className="mm-rdc-bk-row">
            <span>Prorrateo</span>
            <span>{soles(prorrateo)}</span>
          </div>
        )}
        {reconexion > 0 && (
          <div className="mm-rdc-bk-row alert">
            <span>⚠ Cargo por reconexión</span>
            <span className="text-red">{soles(reconexion)}</span>
          </div>
        )}
        <div className="mm-rdc-divider" />
        <div className="mm-rdc-bk-row total">
          <span>Total a pagar servicio</span>
          <span>{soles(total)}</span>
        </div>
      </div>

      {/* ── 5. Comparativa gráfica ──────────────────────────────── */}
      <div className="mm-rdc-compare">
        <p className="mm-rdc-compare-label">Evolución del consumo</p>
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
        <Download size={14} /> Descargar PDF original
      </button>
    </div>
  );
}
