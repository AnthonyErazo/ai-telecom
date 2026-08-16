/**
 * PagoModal — pasos para pagar el recibo, sin derivar a ningún canal.
 *
 * A propósito NO abre ninguna pasarela ni redirige a un banco: el cliente paga
 * desde su propia banca móvil o billetera de preferencia, y esta pantalla solo le
 * da el código de pago (el mismo número de cuenta/servicio que ya usa la app) y los
 * pasos para usarlo. Nada de lo que muestra se calcula — el total y el vencimiento
 * salen tal cual del `FactSet` ya cargado.
 */
import { useState } from "react";
import { Check, Copy, CreditCard, ShieldCheck, Wallet, X } from "lucide-react";
import type { FactSet } from "../api/types";

const soles = (c: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(c / 100);

const PASOS = [
  "Copia tu código de pago.",
  "Abre tu banca móvil, billetera digital o app de tu banco de preferencia.",
  'Busca la opción "Pagar servicios" y elige Movistar.',
  "Ingresa el código de pago y confirma el monto mostrado.",
];

export function PagoModal({ hechos, cuentaId, onClose }: { hechos: FactSet; cuentaId: string; onClose: () => void }) {
  const [copiado, setCopiado] = useState(false);
  const totalAPagar = hechos.total_a_pagar_cent ?? (hechos.total_actual_cent + (hechos.deuda_anterior_cent ?? 0));
  const vencimiento = hechos.fecha_vencimiento ? String(hechos.fecha_vencimiento) : null;

  const copiar = async () => {
    try {
      await navigator.clipboard.writeText(cuentaId);
      setCopiado(true);
      setTimeout(() => setCopiado(false), 2000);
    } catch {
      setCopiado(false);
    }
  };

  return (
    <div className="mm-modal-overlay" role="dialog" aria-modal="true" aria-label="Pagar mi recibo" onClick={onClose}>
      <div className="mm-modal" onClick={(e) => e.stopPropagation()}>
        <div className="mm-modal-header">
          <div className="mm-modal-header-icon"><Wallet size={18} /></div>
          <h2>Pagar mi recibo</h2>
          <button className="mm-modal-close" onClick={onClose} aria-label="Cerrar"><X size={18} /></button>
        </div>

        <div className="mm-modal-body">
          <div className="mm-modal-total">
            <span>Total a pagar</span>
            <strong>{soles(totalAPagar)}</strong>
            {vencimiento && <small>Vence: {vencimiento}</small>}
          </div>

          <div className="mm-modal-codigo">
            <div>
              <small><CreditCard size={11} /> Código de pago</small>
              <strong>{cuentaId}</strong>
            </div>
            <button className={`mm-modal-copiar${copiado ? " copiado" : ""}`} onClick={() => void copiar()}>
              {copiado ? <><Check size={14} /> Copiado</> : <><Copy size={14} /> Copiar</>}
            </button>
          </div>

          <p className="mm-modal-pasos-title">Cómo pagar</p>
          <ol className="mm-modal-pasos">
            {PASOS.map((paso, indice) => (
              <li key={indice}><span className="mm-modal-paso-num">{indice + 1}</span><span>{paso}</span></li>
            ))}
          </ol>

          <div className="mm-modal-nota">
            <ShieldCheck size={13} />
            <span>El pago lo haces directamente en tu banco o billetera de preferencia — no te pedimos ningún dato aquí.</span>
          </div>
        </div>

        <button className="mm-btn-primary mm-modal-submit" onClick={onClose}>Cerrar</button>
      </div>
    </div>
  );
}
