import {
  CalendarClock, CalendarRange, CheckCircle2, CircleDollarSign, PauseCircle, Receipt, Sparkles, Star,
} from "lucide-react";
import type { CicloExplicado, ExtractBlock, HitoCiclo } from "../api/types";

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

/** Separación mínima (en % del ancho) entre dos etiquetas para no pisarse. Con
 * ciclos cortos o hitos muy próximos (p. ej. cierre y vencimiento del mismo mes en
 * cuentas con poca separación) dos posiciones a escala real pueden caer casi
 * encima; en vez de dejarlas ilegibles, la segunda baja a una segunda fila. */
const SEPARACION_MINIMA_PCT = 16;

/** Reparte los hitos de una franja en dos filas quicando la posición real de cada
 * uno choca con la del hito anterior en su misma fila — así ninguna etiqueta queda
 * ilegible por superposición, sin mover ni inventar ninguna fecha. */
function repartirEnFilas(
  puntos: HitoCiclo[],
  rango: { inicio: string; fin: string } | null,
): Array<{ hito: HitoCiclo; left: number; fila: number }> {
  const ultimoPorFila = [-Infinity, -Infinity];
  return puntos.map((hito, indice) => {
    const left = rango
      ? posicionPct(hito.fecha, rango.inicio, rango.fin)
      : (indice / Math.max(1, puntos.length - 1)) * 100;
    const fila = left - ultimoPorFila[0] >= SEPARACION_MINIMA_PCT ? 0 : 1;
    ultimoPorFila[fila] = left;
    return { hito, left, fila };
  });
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

type EstadoPago = NonNullable<CicloExplicado["estado_pago"]>;

const ESTADO_PAGO: Record<EstadoPago, { etiqueta: string; clase: string }> = {
  pagado: { etiqueta: "Pagado", clase: "ok" },
  pendiente: { etiqueta: "Pendiente", clase: "alerta" },
  por_pagar: { etiqueta: "Por pagar", clase: "info" },
  vencido: { etiqueta: "Vencido", clase: "alerta" },
};

export function CycleExplanationCard({ bloque }: { bloque: BloqueCiclos }) {
  const ciclos = bloque.ciclos.slice(0, 2);
  const explicado = ciclos.find((c) => c.actual);

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
        <span className="mm-cycle-badges-row">
          {ciclo.completo != null && (
            <span className={`mm-cycle-estado${ciclo.completo ? "" : " parcial"}`}>
              {ciclo.completo ? "Completo" : "Parcial"}
            </span>
          )}
          {ciclo.estado_pago && (
            <span className={`mm-cycle-estado ${ESTADO_PAGO[ciclo.estado_pago].clase}`}>
              {ESTADO_PAGO[ciclo.estado_pago].etiqueta}
            </span>
          )}
        </span>
        {(ciclo.inicio || ciclo.cierre) && (
          <p><CalendarRange size={10} /> {fechaCorta(ciclo.inicio)} → {fechaCorta(ciclo.cierre)}</p>
        )}
        {ciclo.vencimiento && <em><CalendarClock size={10} /> Vence {fechaCorta(ciclo.vencimiento)}</em>}
      </div>)}
    </div>

    {bloque.causas.length > 0 && <div className="mm-cycle-causes">
      <p className="mm-cycle-section-title">Causas de la variación</p>
      {bloque.causas.map((causa, indice) => <div key={`${causa.etiqueta}-${indice}`}>
        <span>{causa.etiqueta}</span>
        <strong>{money(causa.monto_cent)} <small>{Math.round(causa.participacion_bp / 100)}%</small></strong>
      </div>)}
    </div>}
  </section>;
}
