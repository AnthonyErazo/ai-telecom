"""CLI de indexado del corpus RAG. Idempotente.

Lee el catálogo, las FAQs y las casuísticas que produce ``datagen``, construye el índice
vectorial sobre pgvector y deja constancia de qué se indexó y con qué modelo.

Uso típico::

    python -m packages.retriever.indexar
    python -m packages.retriever.indexar --corpus data/sintetico --embedder mock
    python -m packages.retriever.indexar --solo faq --forzar
    python -m packages.retriever.indexar --verificar --json

**Idempotencia:** cada documento se compara por hash de contenido contra lo ya
almacenado con el mismo modelo. Ejecutarlo dos veces seguidas no vectoriza nada, no
escribe ninguna fila y no gasta ninguna llamada a la API de embeddings. Se puede colgar
de ``make seed`` sin miedo.

**Cambiar de modelo de embeddings obliga a reindexar.** Cada fila guarda la firma del
modelo (``nombre:modelo:dimensión``) y las búsquedas filtran por ella: al cambiar de
modelo el índice antiguo deja de verse, y esta CLI vuelve a vectorizar todo. Para
retirar las filas del modelo anterior, ejecútela con ``--limpiar`` apuntando a ese
modelo.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packages.core_domain.enums import CorpusRag
from packages.retriever.bm25 import IndiceBM25, implementacion_activa
from packages.retriever.corpus import cargar_corpus, documentos_de_faqs
from packages.retriever.vectorial import IndiceVectorial, crear_embedder

__all__ = ["construir_analizador", "indexar_corpus", "main"]

_LOG = logging.getLogger("packages.retriever.indexar")

#: Códigos de salida.
_OK = 0
_ERROR = 1
_SIN_PERSISTENCIA = 3


def construir_analizador() -> argparse.ArgumentParser:
    """Define la interfaz de línea de comandos."""
    analizador = argparse.ArgumentParser(
        prog="python -m packages.retriever.indexar",
        description="Indexa catálogo, FAQs y casuísticas en el índice vectorial (idempotente).",
    )
    analizador.add_argument(
        "--corpus",
        type=Path,
        default=None,
        metavar="RUTA",
        help="Directorio del corpus (por defecto $CORPUS_PATH o data/sintetico)",
    )
    analizador.add_argument(
        "--embedder",
        choices=("mock", "gemini"),
        default=None,
        help="Proveedor de embeddings (por defecto $EMBED_MODE o $LLM_MODE)",
    )
    analizador.add_argument(
        "--dim", type=int, default=None, metavar="N", help="Dimensión (por defecto $EMBED_DIM)"
    )
    analizador.add_argument(
        "--dsn", default=None, metavar="URL", help="DSN de PostgreSQL (por defecto $DATABASE_URL)"
    )
    analizador.add_argument(
        "--solo",
        action="append",
        choices=tuple(str(corpus) for corpus in CorpusRag),
        default=None,
        metavar="CORPUS",
        help="Indexa solo estos corpus (repetible)",
    )
    analizador.add_argument(
        "--forzar",
        action="store_true",
        help="Revectoriza aunque el contenido no haya cambiado",
    )
    analizador.add_argument(
        "--limpiar",
        action="store_true",
        help="Borra lo indexado con este modelo antes de indexar",
    )
    analizador.add_argument(
        "--verificar",
        action="store_true",
        help="Solo informa del estado del índice; no escribe nada",
    )
    analizador.add_argument(
        "--memoria",
        action="store_true",
        help="Fuerza el índice en memoria (prueba en seco, no persiste)",
    )
    analizador.add_argument(
        "--estricto",
        action="store_true",
        help="Falla si no se pudo persistir en pgvector (para el Makefile y CI)",
    )
    analizador.add_argument(
        "--consulta",
        default=None,
        metavar="TEXTO",
        help="Tras indexar, ejecuta una búsqueda de humo y muestra los resultados",
    )
    analizador.add_argument("--json", action="store_true", help="Salida en JSON")
    analizador.add_argument("-v", "--verbose", action="store_true", help="Log detallado")
    return analizador


def indexar_corpus(
    ruta: str | Path | None = None,
    *,
    embedder_modo: str | None = None,
    dimension: int | None = None,
    dsn: str | None = None,
    corpus_seleccionados: Sequence[str] | None = None,
    forzar: bool = False,
    limpiar: bool = False,
    solo_verificar: bool = False,
    forzar_memoria: bool = False,
) -> dict[str, Any]:
    """Carga los tres corpus y sincroniza el índice vectorial.

    Args:
        ruta: directorio del corpus.
        embedder_modo: ``"mock"`` o ``"gemini"``.
        dimension: dimensión de los vectores.
        dsn: DSN de PostgreSQL.
        corpus_seleccionados: subconjunto de corpus a indexar.
        forzar: revectoriza todo.
        limpiar: borra antes de indexar.
        solo_verificar: no escribe; solo informa.
        forzar_memoria: usa el índice en memoria aunque haya base de datos.

    Returns:
        Informe con el estado del índice y el conteo por corpus.
    """
    corpus = cargar_corpus(ruta)
    embedder = crear_embedder(embedder_modo, dimension)
    indice = IndiceVectorial(embedder, dsn=dsn, forzar_memoria=forzar_memoria)

    elegidos = (
        [CorpusRag(nombre) for nombre in corpus_seleccionados]
        if corpus_seleccionados
        else list(CorpusRag)
    )

    informe: dict[str, Any] = {
        "origen": corpus.origen,
        "corpus_disponible": corpus.resumen(),
        "modelo": indice.modelo,
        "dimension": embedder.dimension,
        "respaldo": "pgvector" if indice.disponible_bd else "memoria",
        "motivo_degradacion": indice.motivo_degradacion,
        "bm25_implementacion": implementacion_activa(),
        "solo_verificar": solo_verificar,
        "indexado": {},
    }

    # El índice léxico se reconstruye en cada arranque del proceso (es memoria pura y
    # el corpus es pequeño); aquí solo se comprueba que el corpus lo permite.
    bm25 = IndiceBM25(documentos_de_faqs(corpus.faqs))
    informe["bm25_documentos"] = len(bm25)

    if solo_verificar:
        for corpus_rag in elegidos:
            informe["indexado"][str(corpus_rag)] = {
                "en_indice": indice.contar(corpus_rag),
                "en_corpus": len(corpus.documentos(corpus_rag)),
            }
        informe["total_en_indice"] = indice.contar()
        return informe

    if limpiar:
        for corpus_rag in elegidos:
            borradas = indice.limpiar(corpus_rag)
            _LOG.info("limpiado %s: %d filas", corpus_rag, borradas)

    for corpus_rag in elegidos:
        documentos = corpus.documentos(corpus_rag)
        resultado = indice.indexar(documentos, forzar=forzar)
        resultado["en_indice"] = indice.contar(corpus_rag)
        informe["indexado"][str(corpus_rag)] = resultado

    informe["total_en_indice"] = indice.contar()
    informe["_indice"] = indice  # se usa para la búsqueda de humo; no se serializa
    return informe


def _formatear_texto(informe: dict[str, Any]) -> str:
    """Informe legible para la terminal."""
    lineas = [
        "indexado del corpus RAG",
        f"  origen        : {informe['origen']}",
        f"  modelo        : {informe['modelo']} (dim {informe['dimension']})",
        f"  respaldo      : {informe['respaldo']}",
    ]
    if informe.get("motivo_degradacion"):
        lineas.append(f"  degradación   : {informe['motivo_degradacion']}")
    lineas.append(f"  bm25          : {informe['bm25_documentos']} FAQs ({informe['bm25_implementacion']})")
    lineas.append("  corpus:")
    for nombre, datos in informe["indexado"].items():
        if informe.get("solo_verificar"):
            lineas.append(
                f"    {nombre:<20} en índice {datos['en_indice']:>4} / en corpus {datos['en_corpus']:>4}"
            )
        else:
            lineas.append(
                f"    {nombre:<20} total {datos['total']:>4} · vectorizados {datos['vectorizados']:>4}"
                f" · sin cambios {datos['sin_cambios']:>4} · en índice {datos['en_indice']:>4}"
            )
    lineas.append(f"  total en índice: {informe['total_en_indice']}")
    return "\n".join(lineas)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada de la CLI. Devuelve el código de salida del proceso."""
    analizador = construir_analizador()
    argumentos = analizador.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if argumentos.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        informe = indexar_corpus(
            argumentos.corpus,
            embedder_modo=argumentos.embedder,
            dimension=argumentos.dim,
            dsn=argumentos.dsn,
            corpus_seleccionados=argumentos.solo,
            forzar=argumentos.forzar,
            limpiar=argumentos.limpiar,
            solo_verificar=argumentos.verificar,
            forzar_memoria=argumentos.memoria,
        )
    except Exception as error:  # la CLI reporta, no revienta
        _LOG.error("fallo al indexar: %s: %s", type(error).__name__, error)
        if argumentos.verbose:
            raise
        return _ERROR

    indice: IndiceVectorial = informe.pop("_indice", None)

    muestra: list[dict[str, Any]] = []
    if argumentos.consulta and indice is not None:
        for resultado in indice.buscar(argumentos.consulta, k=5):
            muestra.append(
                {
                    "doc_id": resultado.doc_id,
                    "corpus": str(resultado.corpus),
                    "similitud": round(resultado.puntaje, 4),
                    "titulo": resultado.documento.titulo,
                }
            )
        informe["consulta"] = argumentos.consulta
        informe["muestra"] = muestra

    if argumentos.json:
        print(json.dumps(informe, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_formatear_texto(informe))
        if muestra:
            print(f"\n  búsqueda de humo: {argumentos.consulta!r}")
            for fila in muestra:
                print(f"    {fila['similitud']:>7.4f}  {fila['doc_id']:<48} {fila['titulo']}")

    if informe["respaldo"] == "memoria" and not argumentos.memoria:
        _LOG.warning(
            "NO SE PERSISTIÓ NADA: el índice se construyó en memoria y este proceso termina "
            "ahora. Levante PostgreSQL con pgvector y defina DATABASE_URL para persistir."
        )
        if argumentos.estricto:
            return _SIN_PERSISTENCIA
    return _OK


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    sys.exit(main())
