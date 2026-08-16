import {
  CalendarClock, CalendarRange, CheckCircle2, CircleDollarSign, PauseCircle, Receipt, Sparkles, Star,
} from "lucide-react";
import type { ExtractBlock, HitoCiclo } from "../api/types";

type BloqueCiclos = ExtractBlock<"ciclos">;

const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
const money = (centimos: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(centimos / 100);

function fechaCorta(iso?: string | null): string {
  if (!iso) return "";
  const [, mes, dia] = iso.split("-").map(Number);
  return dia && mes ? `${dia} ${MESES[mes - 1]}` : iso;
}

/** Un día antes de una fecha ISO exclusiva ("fin" de tramo/ciclo), para mostrar el
 * último día realmente incluido — igual convención que `etiqueta_rango_fechas` en el
 * backend. Aritmética UTC pura sobre una fecha que ya vino del servidor: no inventa
 * ni recalcula ningún hecho, solo ajusta cómo se lee un extremo exclusivo. */
function ultimoDiaIncluido(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const fecha = new Date(Date.UTC(y, m - 1, d));
  fecha.setUTCDate(fecha.getUTCDate() - 1);
  return fecha.toISOString().slice(0, 10);
}

function diasEntre(aIso: string, bIso: string): number {
  const [ay, am, ad] = aIso.split("-").map(Number);
  const [by, bm, bd] = bIso.split("-").map(Number);
  return (Date.UTC(by, bm - 1, bd) - Date.UTC(ay, am - 1, ad)) / 86_400_000;
}

/** Posición horizontal (0-100%) de una fecha dentro del rango [inicio, fin) del
 * ciclo. Es aritmética de layout sobre fechas que YA trajo el backend — la línea de
 * tiempo se dibuja a escala real en vez de repartir los puntos a distancias iguales. */
function posicionPct(fechaIso: string, inicioIso: string, finIso: string): number {
  const total = diasEntre(inicioIso, finIso);
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, (diasEntre(inicioIso, fechaIso) / total) * 100));
}

/** Símbolo distintivo por tipo de hito, para que cada fecha se reconozca de un
 * vistazo sin leer la etiqueta: estrella para los bordes del ciclo, recibo con
 * monto para la facturación, pausa/check para el corte y la reconexión. */
function icono(tipo: HitoCiclo["tipo"]) {
  switch (tipo) {
    case "suspension": return PauseCircle;
    case "reconexion": return CheckCircle2;
    case "facturacion": return CircleDollarSign;
    case "vencimiento": return CalendarClock;
    case "inicio":
    case "cierre":
      return Star;
    default: return CalendarRange;
  }
}

const ETIQUETA_NODO: Record<HitoCiclo["tipo"], string> = {
  inicio: "Inicio",
  cierre: "Fin",
  facturacion: "Facturación",
  vencimiento: "Vencimiento",
  suspension: "Corte de servicio",
  reconexion: "Reconexión",
  prorrateo: "Cambio",
};

export function CycleExplanationCard({ bloque }: { bloque: BloqueCiclos }) {
  const ciclos = bloque.ciclos.slice(0, 2);
  const explicado = ciclos.find((c) => c.actual);

  // Una franja por ciclo, con sus hitos en orden cronológico y su tramo parcial (si
  // lo tuvo) — el mismo dato que ya trae el bloque, solo agrupado por `periodo` para
  // dibujar el diagrama "Ciclo de facturación 1 / 2" a escala real en vez de una
  // fila con los puntos a distancias iguales.
  const bandas = ciclos
    .map((ciclo) => ({
      ciclo,
      // El vencimiento NO entra en la línea proporcional: es la fecha límite de
      // pago, no un borde del ciclo, y suele caer días después del cierre — mezclarlo
      // en la misma regla lo haría parecer el fin del ciclo.
      puntos: bloque.hitos
        .filter((h) => h.periodo === ciclo.periodo && h.tipo !== "vencimiento")
        .slice()
        .sort((a, b) => a.fecha.localeCompare(b.fecha)),
      vencimiento: bloque.hitos.find((h) => h.periodo === ciclo.periodo && h.tipo === "vencimiento"),
      segmento: bloque.segmentos_parciales.find((s) => s.periodo === ciclo.periodo),
    }))
    .filter((banda) => banda.puntos.length > 0 || banda.vencimiento);

  return <section className="mm-cycle-card">
    <header className="mm-cycle-header">
      <span className="mm-cycle-header-icon"><CalendarRange size={17} /></span>
      <div>
        <strong>{bloque.titulo || "Comparación de ciclos"}</strong>
        <span className="mm-cycle-badges">
          <span className="mm-cycle-badge">{bloque.modalidad.replace("_", " ")}</span>
          {explicado?.es_mas_reciente && (
            <span className="mm-cycle-badge reciente"><Sparkles size={10} /> Ciclo más reciente</span>
          )}
        </span>
      </div>
    </header>

    <div className="mm-cycle-track">
      {ciclos.map((ciclo) => <div className={`mm-cycle-node${ciclo.actual ? " actual" : ""}`} key={ciclo.periodo}>
        <span className="mm-cycle-dot"><Receipt size={13} /></span>
        <small>{ciclo.actual ? "Ciclo explicado" : "Ciclo anterior"}</small>
        <strong>{ciclo.periodo}</strong>
        <b>{money(ciclo.total_cent)}</b>
        {ciclo.completo != null && (
          <span className={`mm-cycle-estado${ciclo.completo ? "" : " parcial"}`}>
            {ciclo.completo ? "Completo" : "Parcial"}
          </span>
        )}
        {(ciclo.inicio || ciclo.cierre) && (
          <p><CalendarRange size={10} /> {fechaCorta(ciclo.inicio)} → {fechaCorta(ciclo.cierre)}</p>
        )}
        {ciclo.vencimiento && <em><CalendarClock size={10} /> Vence {fechaCorta(ciclo.vencimiento)}</em>}
      </div>)}
    </div>

    {bandas.length > 0 && <div className="mm-cycle-timeline">
      <p className="mm-cycle-section-title">Qué ocurrió en cada ciclo</p>
      {bandas.map(({ ciclo, puntos, vencimiento, segmento }, indice) => {
        const rango = ciclo.inicio && ciclo.cierre ? { inicio: ciclo.inicio, fin: ciclo.cierre } : null;
        return <div className="mm-cycle-band" key={ciclo.periodo}>
          <div className="mm-cycle-band-head">
            <span>Ciclo de facturación {indice + 1} · {ciclo.periodo}</span>
            <span className="mm-cycle-band-tags">
              {ciclo.completo === false && <em className="parcial">Parcial</em>}
              {ciclo.es_mas_reciente && <em><Sparkles size={9} /> Más reciente</em>}
            </span>
          </div>

          {puntos.length > 0 && <div className="mm-cycle-band-line">
            <div className="mm-cycle-band-rail" />
            {segmento && rango && <div
              className="mm-cycle-band-segmento"
              style={{
                left: `${posicionPct(segmento.inicio, rango.inicio, rango.fin)}%`,
                width: `${Math.max(
                  3,
                  posicionPct(segmento.fin, rango.inicio, rango.fin)
                    - posicionPct(segmento.inicio, rango.inicio, rango.fin),
                )}%`,
              }}
            />}
            {puntos.map((hito, i) => {
              const Icono = icono(hito.tipo);
              const left = rango
                ? posicionPct(hito.fecha, rango.inicio, rango.fin)
                : (i / Math.max(1, puntos.length - 1)) * 100;
              return <div className={`mm-cycle-hito tipo-${hito.tipo}`} style={{ left: `${left}%` }} key={`${hito.fecha}-${hito.tipo}-${i}`}>
                <span className="mm-cycle-hito-dot" title={ETIQUETA_NODO[hito.tipo]}><Icono size={13} /></span>
                <b>{fechaCorta(hito.fecha)}</b>
                <small>{hito.etiqueta}</small>
              </div>;
            })}
          </div>}

          {segmento && <p className="mm-cycle-band-nota">
            <strong>Período parcial:</strong> {fechaCorta(segmento.inicio)}–{fechaCorta(ultimoDiaIncluido(segmento.fin))} · {segmento.dias} día{segmento.dias === 1 ? "" : "s"}
            {segmento.monto_cent != null && <> · {money(segmento.monto_cent)}</>}
            <br /><span className="mm-cycle-band-causa">{segmento.causa}</span>
          </p>}

          {vencimiento && <div className="mm-cycle-band-vencimiento">
            <CalendarClock size={12} />
            <span><b>{fechaCorta(vencimiento.fecha)}</b> Vencimiento</span>
            <small>no es fin de ciclo</small>
          </div>}
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
