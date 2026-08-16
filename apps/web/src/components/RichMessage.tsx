/**
 * RichMessage — bloques del backend pintados con formato rico para una burbuja
 * de chat angosta (chat de texto y BillSense Voz en Mi Movistar).
 *
 * No inventa nada: pinta exactamente los `Block[]` que ya llegan en
 * `Explanation.bloques` (mismo contrato que consume `Blocks.tsx` en la vista
 * desktop). Lo único que añade es presentación — negrita, ícono y color por
 * severidad/palabra clave — para que se lea de un vistazo en un móvil.
 */
import type { ComponentType, ReactNode } from "react";
import {
  AlertOctagon, AlertTriangle, Banknote, CalendarClock, CalendarRange,
  Info, PauseCircle, Percent, PhoneCall, Receipt, Repeat, Wallet,
} from "lucide-react";
import type { Block } from "../api/types";

export const money = (cents: number) =>
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(cents / 100);

type Icono = ComponentType<{ size?: number; className?: string }>;

// ── Ícono por palabra clave ──────────────────────────────────────────────
// Heurística puramente cosmética: no cambia el texto, solo elige qué dibujo
// acompaña un bloque según de qué habla. Si nada calza, se usa un ícono
// genérico de recibo.
const PALABRAS_CLAVE: Array<[RegExp, Icono]> = [
  [/prorrate/i, CalendarRange],
  [/reconexi/i, PhoneCall],
  [/suspensi/i, PauseCircle],
  [/descuento|promoci/i, Percent],
  [/deuda|saldo/i, Wallet],
  [/vencimiento|vence/i, CalendarClock],
  [/cambio de plan/i, Repeat],
  [/equipo|financia/i, Banknote],
];

export function iconoPara(...textos: Array<string | null | undefined>): Icono {
  for (const texto of textos) {
    if (!texto) continue;
    for (const [patron, Icono] of PALABRAS_CLAVE) if (patron.test(texto)) return Icono;
  }
  return Receipt;
}

export function iconoSeveridad(severidad: string): Icono {
  if (severidad === "critico") return AlertOctagon;
  if (severidad === "advertencia") return AlertTriangle;
  return Info;
}

// ── Negrita inline ───────────────────────────────────────────────────────
// Red de seguridad: el texto generado hoy no trae `**`, pero si algún día lo
// trae (o una plantilla lo usa a mano) se pinta en negrita en vez de mostrar
// los asteriscos literales.
export function conNegrita(texto: string): ReactNode {
  const partes = texto.split(/(\*\*[^*]+\*\*)/g).filter(Boolean);
  if (partes.length <= 1) return texto;
  return partes.map((parte, indice) =>
    parte.startsWith("**") && parte.endsWith("**")
      ? <strong key={indice}>{parte.slice(2, -2)}</strong>
      : <span key={indice}>{parte}</span>
  );
}

export function RichMessage({ bloques }: { bloques: Block[] }) {
  return <div className="mm-rich">
    {bloques.map((bloque, indice) => {
      const key = `${bloque.tipo}-${indice}`;

      if (bloque.tipo === "texto") {
        const Icono = iconoPara(bloque.titulo, bloque.texto);
        return <div className={`mm-rich-texto${bloque.enfasis ? " enfasis" : ""}`} key={key}>
          {bloque.enfasis && <Icono size={15} className="mm-rich-icon" />}
          <div>
            {bloque.titulo && <strong className="mm-rich-titulo">{bloque.titulo}</strong>}
            <p>{conNegrita(bloque.texto)}</p>
          </div>
        </div>;
      }

      if (bloque.tipo === "aviso") {
        const Icono = iconoSeveridad(bloque.severidad);
        return <div className={`mm-rich-aviso ${bloque.severidad}`} key={key}>
          <Icono size={16} className="mm-rich-icon" />
          <div>
            {bloque.titulo && <strong>{bloque.titulo}</strong>}
            <p>{conNegrita(bloque.texto)}</p>
          </div>
        </div>;
      }

      if (bloque.tipo === "kv") {
        return <div className="mm-rich-kv" key={key}>
          {bloque.titulo && <p className="mm-rich-titulo">{bloque.titulo}</p>}
          <dl>
            {bloque.items.map((item) => {
              const Icono = iconoPara(item.clave);
              return <div key={`${item.clave}-${item.valor}`}>
                <dt><Icono size={13} className="mm-rich-icon" />{item.clave}</dt>
                <dd>{item.valor}</dd>
              </div>;
            })}
          </dl>
        </div>;
      }

      if (bloque.tipo === "tabla") {
        return <div className="mm-rich-tabla" key={key}>
          {bloque.titulo && <p className="mm-rich-titulo">{bloque.titulo}</p>}
          <div className="mm-rich-tabla-scroll">
            <table>
              <thead><tr>{bloque.columnas.map((columna) => <th key={columna}>{columna}</th>)}</tr></thead>
              <tbody>{bloque.filas.map((fila, indiceFila) =>
                <tr key={indiceFila}>{fila.map((celda, indiceCelda) => <td key={indiceCelda}>{celda}</td>)}</tr>
              )}</tbody>
            </table>
          </div>
          {bloque.nota && <small>{bloque.nota}</small>}
        </div>;
      }

      // puente — waterfall compacto: previo -> incrementos/decrementos -> actual.
      return <div className="mm-rich-puente" key={key}>
        {bloque.titulo && <p className="mm-rich-titulo">{bloque.titulo}</p>}
        {bloque.barras.map((barra) =>
          <div className={`mm-rich-puente-fila ${barra.tipo}`} key={`${barra.etiqueta}-${barra.monto_cent}`}>
            <span>{barra.etiqueta}</span>
            <strong>{money(barra.monto_cent)}</strong>
          </div>
        )}
      </div>;
    })}
  </div>;
}
