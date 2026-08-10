"""Índice léxico BM25 sobre las FAQs, con tokenizador para español de Perú.

Por qué hace falta un índice léxico habiendo vectores: los clientes escriben con las
palabras que ven en el recibo ("reconexión", "prorrateo", "Movistar Total"). Esos
términos son raros en el corpus general y BM25 los premia justamente por eso, mientras
que un embedding tiende a diluirlos entre parafraseos. El vector aporta lo contrario:
recupera "me vino más caro" cuando la FAQ dice "por qué aumentó mi recibo". Se fusionan
en :mod:`packages.retriever.hibrido` con RRF; ninguno de los dos basta solo.

Tokenizador
-----------
Minúsculas, sin tildes (``NFD`` y descarte de diacríticos: ``ñ`` pasa a ``n`` y ``ü`` a
``u``, de forma consistente en documentos y en consultas), sin signos de puntuación y
sin palabras vacías. Es deliberadamente simple: no hay stemming porque en un corpus de
FAQ tan acotado el stemming introduce más falsos positivos que recall, y porque un
tokenizador trivial es reproducible y auditable, que es lo que exige la demo.

Implementación
--------------
Usa ``rank-bm25`` (Apache-2.0) cuando está instalado, que es la dependencia fijada en
la especificación. Si no lo está, cae a una implementación propia de BM25 Okapi
equivalente, para que el retriever nunca deje de funcionar por una dependencia
ausente. La variable de entorno ``BM25_IMPL`` fuerza una u otra (``rank_bm25`` | ``puro``).
"""

from __future__ import annotations

import logging
import math
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Final

from packages.core_domain.enums import CorpusRag
from packages.retriever.corpus import DocumentoCorpus, Procedencia, ResultadoBusqueda

__all__ = [
    "STOPWORDS_ES",
    "IndiceBM25",
    "implementacion_activa",
    "normalizar",
    "tokenizar",
]

_LOG = logging.getLogger(__name__)

#: Parámetros clásicos de BM25 Okapi.
_K1: Final = 1.5
_B: Final = 0.75

#: Palabras vacías del español. Se omiten a propósito ``mas`` (para conservar la señal
#: de "más caro" / "más barato", que es literalmente la pregunta del desafío) y ``no``
#: (distingue "no tuve servicio" de "tuve servicio").
# Se mantiene como texto corrido y no como lista literal: así la lista es legible y
# revisable por alguien de negocio, que es quien decide qué palabra es vacía.
_STOPWORDS_CRUDAS: Final = """
    a al algo algun alguna algunas alguno algunos ante antes aquel aquella aquellas
    aquello aquellos aqui asi aun aunque cada como con contra cual cuales cuando cuanto
    de del desde donde dos e el ella ellas ello ellos en entre era eran eres es esa esas
    ese eso esos esta estaba estan estar estas este esto estos estoy fue fueron ha haber
    habia han hasta hay he la las le les lo los me mi mis mucho muy nada ni nos nosotros
    nuestra nuestro o os otra otras otro otros para pero poco por porque que quien
    quienes se sea segun ser si siempre sin sobre son su sus tal tambien tan tanto te
    tener tengo ti tiene tienen todo todos tu tus tuyo un una unas uno unos usted ustedes
    va van vamos ver vos y ya yo
"""

STOPWORDS_ES: Final[frozenset[str]] = frozenset(_STOPWORDS_CRUDAS.split())

_RE_TOKEN: Final = re.compile(r"[a-z0-9]+")


def normalizar(texto: str) -> str:
    """Minúsculas y sin diacríticos. Se aplica igual a documentos y a consultas."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(caracter for caracter in descompuesto if unicodedata.category(caracter) != "Mn")


def tokenizar(
    texto: str, *, quitar_stopwords: bool = True, longitud_minima: int = 2
) -> list[str]:
    """Convierte texto libre en la lista de términos que indexa BM25.

    Args:
        texto: pregunta del cliente o contenido de un documento.
        quitar_stopwords: descarta palabras vacías del español.
        longitud_minima: descarta términos más cortos (ruido de puntuación).

    Returns:
        Términos normalizados, en orden de aparición y con repeticiones (BM25 usa la
        frecuencia de término).
    """
    tokens = _RE_TOKEN.findall(normalizar(texto))
    return [
        token
        for token in tokens
        if len(token) >= longitud_minima and not (quitar_stopwords and token in STOPWORDS_ES)
    ]


# --------------------------------------------------------------------------- #
# Motor BM25
# --------------------------------------------------------------------------- #
class _BM25OkapiPuro:
    """BM25 Okapi en Python puro. Respaldo si ``rank-bm25`` no está instalado.

    Usa la variante de IDF con suavizado ``ln(1 + (N - n + 0.5)/(n + 0.5))``, siempre
    positiva: evita los pesos negativos que da la formulación original con términos muy
    frecuentes, sin necesidad de la corrección por epsilon.
    """

    def __init__(self, corpus_tokenizado: Sequence[Sequence[str]]) -> None:
        self._documentos = [Counter(tokens) for tokens in corpus_tokenizado]
        self._longitudes = [len(tokens) for tokens in corpus_tokenizado]
        self._total = len(corpus_tokenizado)
        self._longitud_media = (
            sum(self._longitudes) / self._total if self._total else 0.0
        )
        frecuencia_documental: Counter[str] = Counter()
        for documento in self._documentos:
            frecuencia_documental.update(documento.keys())
        self._idf = {
            termino: math.log(1.0 + (self._total - n + 0.5) / (n + 0.5))
            for termino, n in frecuencia_documental.items()
        }

    def get_scores(self, consulta: Sequence[str]) -> list[float]:
        """Puntaje BM25 de la consulta contra cada documento del corpus."""
        puntajes = [0.0] * self._total
        if not self._total or self._longitud_media == 0:
            return puntajes
        for termino in consulta:
            idf = self._idf.get(termino)
            if idf is None:
                continue
            for indice, documento in enumerate(self._documentos):
                frecuencia = documento.get(termino, 0)
                if not frecuencia:
                    continue
                normalizacion = _K1 * (
                    1 - _B + _B * self._longitudes[indice] / self._longitud_media
                )
                puntajes[indice] += idf * frecuencia * (_K1 + 1) / (frecuencia + normalizacion)
        return puntajes


def _clase_motor() -> tuple[type, str]:
    """Elige el motor BM25: ``rank-bm25`` si está disponible, si no el propio."""
    preferencia = (os.getenv("BM25_IMPL") or "").strip().lower()
    if preferencia == "puro":
        return _BM25OkapiPuro, "puro"
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
    except ImportError:
        if preferencia == "rank_bm25":
            raise
        _LOG.warning(
            "rank-bm25 no está instalado; se usa la implementación propia de BM25 Okapi "
            "(instale la dependencia para el comportamiento de referencia)"
        )
        return _BM25OkapiPuro, "puro"
    return BM25Okapi, "rank_bm25"


def implementacion_activa() -> str:
    """Nombre del motor BM25 en uso: ``"rank_bm25"`` o ``"puro"``. Se registra en auditoría."""
    return _clase_motor()[1]


class IndiceBM25:
    """Índice léxico sobre una colección de documentos (en la práctica, las FAQs).

    Es un índice en memoria y se reconstruye en el arranque: el corpus son decenas o
    centenares de documentos, no millones. Persistirlo añadiría una fuente de
    desincronización a cambio de nada.
    """

    def __init__(
        self, documentos: Iterable[DocumentoCorpus] = (), *, tokenizador=tokenizar
    ) -> None:
        self._tokenizador = tokenizador
        self._documentos: list[DocumentoCorpus] = []
        self._motor: object | None = None
        self._implementacion = "vacio"
        self.reconstruir(documentos)

    # ------------------------------------------------------------------ #
    def reconstruir(self, documentos: Iterable[DocumentoCorpus]) -> int:
        """Reindexa desde cero. Devuelve el número de documentos indexados."""
        self._documentos = list(documentos)
        if not self._documentos:
            self._motor = None
            self._implementacion = "vacio"
            return 0
        corpus_tokenizado = [
            self._tokenizador(documento.texto_indexable()) or ["_"]
            for documento in self._documentos
        ]
        clase, nombre = _clase_motor()
        self._motor = clase(corpus_tokenizado)
        self._implementacion = nombre
        return len(self._documentos)

    def __len__(self) -> int:
        return len(self._documentos)

    @property
    def implementacion(self) -> str:
        """Motor efectivamente en uso para este índice."""
        return self._implementacion

    @property
    def documentos(self) -> list[DocumentoCorpus]:
        """Documentos indexados, en orden de indexación."""
        return list(self._documentos)

    # ------------------------------------------------------------------ #
    def puntajes(self, consulta: str) -> list[float]:
        """Puntaje BM25 crudo de la consulta contra cada documento indexado."""
        if self._motor is None:
            return []
        terminos = self._tokenizador(consulta)
        if not terminos:
            return [0.0] * len(self._documentos)
        return [float(valor) for valor in self._motor.get_scores(terminos)]  # type: ignore[attr-defined]

    def buscar(
        self,
        consulta: str,
        k: int = 5,
        *,
        conceptos: Sequence[str] | None = None,
        corpus: CorpusRag | None = None,
        incluir_transversales: bool = True,
    ) -> list[ResultadoBusqueda]:
        """Busca los ``k`` documentos más relevantes, con filtro por metadatos.

        Args:
            consulta: texto libre del cliente.
            k: número máximo de resultados.
            conceptos: si se indica, solo se consideran documentos que hablen de alguno
                de esos ``concepto_id``. Es el filtro por FactSet de la sección 6: no se
                le ofrece al modelo una FAQ sobre reconexiones si en el recibo no hay
                ninguna reconexión.
            corpus: restringe a un corpus concreto.
            incluir_transversales: mantiene los documentos sin conceptos asociados, que
                sirven para cualquier recibo (p. ej. "¿cuándo vence mi recibo?").

        Returns:
            Resultados ordenados por puntaje descendente, con ``puntaje > 0``. El
            desempate es por ``doc_id`` para que el orden sea determinista.
        """
        puntajes = self.puntajes(consulta)
        if not puntajes:
            return []

        filtro = set(conceptos) if conceptos else None
        candidatos: list[tuple[float, DocumentoCorpus]] = []
        for documento, puntaje in zip(self._documentos, puntajes, strict=True):
            if puntaje <= 0.0:
                continue
            if corpus is not None and documento.corpus is not corpus:
                continue
            if filtro is not None:
                if documento.conceptos:
                    if not filtro.intersection(documento.conceptos):
                        continue
                elif not incluir_transversales:
                    continue
            candidatos.append((puntaje, documento))

        candidatos.sort(key=lambda par: (-par[0], par[1].doc_id))
        return [
            ResultadoBusqueda(
                documento=documento,
                puntaje=puntaje,
                procedencia=Procedencia.BM25,
                posicion=posicion,
                detalle={"bm25": puntaje},
            )
            for posicion, (puntaje, documento) in enumerate(candidatos[:k], start=1)
        ]
