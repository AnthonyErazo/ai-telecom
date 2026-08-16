import { CalendarClock, CalendarRange, PauseCircle, PhoneCall, Receipt } from "lucide-react";
import type { ExtractBlock } from "../api/types";

type BloqueCiclos = ExtractBlock<"ciclos">;

const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
const money = (centimos: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(centimos / 100);

function fechaCorta(iso?: string | null): string {
  if (!iso) return "";
  const [, mes, dia] = iso.split("-").map(Number);
  return dia && mes ? `${dia} ${MESES[mes - 1]}` : iso;
}

function icono(tipo: string) {
  if (tipo === "suspension") return PauseCircle;
  if (tipo === "reconexion") return PhoneCall;
  if (tipo === "vencimiento") return CalendarClock;
  return CalendarRange;
}

export function CycleExplanationCard({ bloque }: { bloque: BloqueCiclos }) {
  return <section className="mm-cycle-card">
    <header className="mm-cycle-header">
      <span className="mm-cycle-header-icon"><CalendarRange size={17} /></span>
      <div><strong>{bloque.titulo || "Comparación de ciclos"}</strong><small>{bloque.modalidad.replace("_", " ")}</small></div>
    </header>

    <div className="mm-cycle-track">
      {bloque.ciclos.slice(0, 2).map((ciclo, indice) => <div className={`mm-cycle-node${ciclo.actual ? " actual" : ""}`} key={ciclo.periodo}>
        <span className="mm-cycle-dot"><Receipt size={13} /></span>
        <small>{indice === 0 ? "Ciclo anterior" : "Ciclo explicado"}</small>
        <strong>{ciclo.periodo}</strong>
        <b>{money(ciclo.total_cent)}</b>
        {(ciclo.inicio || ciclo.cierre) && <p>{fechaCorta(ciclo.inicio)} → {fechaCorta(ciclo.cierre)}</p>}
        {ciclo.vencimiento && <em>Vence {fechaCorta(ciclo.vencimiento)}</em>}
      </div>)}
    </div>

    {bloque.hitos.length > 0 && <div className="mm-cycle-events">
      <p className="mm-cycle-section-title">Qué ocurrió dentro del ciclo</p>
      {bloque.hitos.map((hito, indice) => {
        const Icono = icono(hito.tipo);
        return <div className="mm-cycle-event" key={`${hito.fecha}-${hito.etiqueta}-${indice}`}>
          <Icono size={13} /><span><b>{fechaCorta(hito.fecha)}</b>{hito.etiqueta}</span>
        </div>;
      })}
    </div>}

    {bloque.causas.length > 0 && <div className="mm-cycle-causes">
      <p className="mm-cycle-section-title">Causas de la variación</p>
      {bloque.causas.map((causa, indice) => <div key={`${causa.etiqueta}-${indice}`}>
        <span>{causa.etiqueta}</span>
        <strong>{money(causa.monto_cent)} <small>{Math.round(causa.participacion_bp / 100)}%</small></strong>
      </div>)}
    </div>}
  </section>;
}
