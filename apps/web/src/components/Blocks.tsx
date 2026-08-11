import type { Block } from "../api/types";

export const money = (cents: number) => new Intl.NumberFormat("es-PE", { style: "currency", currency: "PEN" }).format(cents / 100);

export function Blocks({ blocks }: { blocks: Block[] }) {
  return <div className="blocks">{blocks.map((block, index) => {
    const key = `${block.tipo}-${index}`;
    if (block.tipo === "texto") return <article className={block.enfasis ? "block emphasis" : "block"} key={key}>{block.titulo && <h3>{block.titulo}</h3>}<p>{block.texto}</p></article>;
    if (block.tipo === "aviso") return <article className={`block notice ${block.severidad}`} key={key}>{block.titulo && <h3>{block.titulo}</h3>}<p>{block.texto}</p></article>;
    if (block.tipo === "kv") return <article className="block" key={key}>{block.titulo && <h3>{block.titulo}</h3>}<dl>{block.items.map((item) => <div key={`${item.clave}-${item.valor}`}><dt>{item.clave}</dt><dd>{item.valor}</dd></div>)}</dl></article>;
    if (block.tipo === "tabla") return <article className="block table-wrap" key={key}>{block.titulo && <h3>{block.titulo}</h3>}<table><thead><tr>{block.columnas.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{block.filas.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table>{block.nota && <small>{block.nota}</small>}</article>;
    return <article className="block" key={key}>{block.titulo && <h3>{block.titulo}</h3>}<div className="bridge">{block.barras.map((bar) => <div className={`bridge-row ${bar.tipo}`} key={`${bar.etiqueta}-${bar.monto_cent}`}><span>{bar.etiqueta}</span><strong>{money(bar.monto_cent)}</strong></div>)}</div></article>;
  })}</div>;
}
