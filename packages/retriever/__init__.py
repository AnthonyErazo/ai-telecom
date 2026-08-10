"""Recuperación híbrida sobre catálogo, FAQs y casuísticas. **El recibo no se vectoriza.**

Punto de importación único del retriever::

    from packages.retriever import recuperar

    contexto = recuperar(factset, utterance, k=5)
    bloque_contexto = contexto.a_prompt()   # ya saneado: sin una sola cifra del corpus

Los tres corpus y su modo de acceso (sección 6 de la especificación):

* ``concepto_catalogo`` — **lookup por clave** ``concepto_id``, que viene del FactSet.
* ``faq`` — híbrido BM25 + vectorial, fusión RRF (k=60), filtrado por los ``concepto_id``
  del FactSet.
* ``casuistica`` — vectorial por **firma causal** (causas + modalidad + signo del delta).

El recibo se consulta de forma estructurada en el motor determinístico: aquí no entra.
Y nada de lo que sale de este paquete conserva cifras — ver :mod:`.saneador`.
"""

from packages.retriever.bm25 import STOPWORDS_ES, IndiceBM25, tokenizar
from packages.retriever.corpus import (
    CASUISTICAS_SEMILLA,
    Casuistica,
    CorpusCompleto,
    DocumentoCorpus,
    Faq,
    IndiceCasuisticas,
    IndiceCatalogo,
    Procedencia,
    ResultadoBusqueda,
    cargar_casuisticas,
    cargar_catalogo,
    cargar_corpus,
    cargar_faqs,
    firma_causal,
    firma_causal_de_factset,
)
from packages.retriever.hibrido import (
    K_RRF,
    ContextoRecuperado,
    FragmentoContexto,
    Recuperador,
    fusion_rrf,
    recuperador_por_defecto,
    recuperar,
    reiniciar_recuperador,
)
from packages.retriever.saneador import ResultadoSaneado, contiene_cifras, sanear, sanear_detallado
from packages.retriever.vectorial import (
    Embedder,
    ErrorEmbedder,
    GeminiEmbedder,
    IndiceVectorial,
    MockEmbedder,
    crear_embedder,
)

__all__ = [
    "CASUISTICAS_SEMILLA",
    "K_RRF",
    "STOPWORDS_ES",
    "Casuistica",
    "ContextoRecuperado",
    "CorpusCompleto",
    "DocumentoCorpus",
    "Embedder",
    "ErrorEmbedder",
    "Faq",
    "FragmentoContexto",
    "GeminiEmbedder",
    "IndiceBM25",
    "IndiceCasuisticas",
    "IndiceCatalogo",
    "IndiceVectorial",
    "MockEmbedder",
    "Procedencia",
    "Recuperador",
    "ResultadoBusqueda",
    "ResultadoSaneado",
    "cargar_casuisticas",
    "cargar_catalogo",
    "cargar_corpus",
    "cargar_faqs",
    "contiene_cifras",
    "crear_embedder",
    "firma_causal",
    "firma_causal_de_factset",
    "fusion_rrf",
    "recuperador_por_defecto",
    "recuperar",
    "reiniciar_recuperador",
    "sanear",
    "sanear_detallado",
    "tokenizar",
]
