"""Recuperación híbrida: la orquestación de los tres corpus en una sola llamada.

``recuperar(factset, utterance)`` hace tres cosas distintas porque son tres problemas
distintos, y mezclarlos sería el error clásico de "meto todo en un índice vectorial":

1. **Catálogo — lookup por clave.** Los ``concepto_id`` ya vienen en el FactSet. No se
   busca nada: se lee la ficha de cada concepto que efectivamente varió. Recuperación
   con precisión 1 por construcción.
2. **FAQ — híbrido BM25 + vectorial fusionado con RRF (k=60), filtrado por los
   ``concepto_id`` del FactSet.** El filtro es lo que impide ofrecerle al modelo una FAQ
   sobre reconexiones cuando en el recibo no hubo ninguna reconexión.
3. **Casuística — por firma causal.** El escenario ya está determinado (causas +
   modalidad + signo del delta), así que se busca por coincidencia exacta de firma y
   solo se cae a similitud vectorial cuando la combinación no estaba prevista.

Todo lo que sale de aquí está **saneado**: :class:`ContextoRecuperado` no transporta ni
un dígito del corpus. No es una recomendación de uso, es una propiedad del tipo — el
texto crudo no cruza la frontera de este módulo. Así, la regla nº 4 ("``ALLOWED`` se
construye solo desde el FactSet") se cumple porque no hay ninguna otra cifra en el
prompt que pudiera competir.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import CorpusRag, TipoEvidencia
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import ItemEvidencia
from packages.retriever.bm25 import IndiceBM25
from packages.retriever.corpus import (
    CorpusCompleto,
    DocumentoCorpus,
    IndiceCasuisticas,
    IndiceCatalogo,
    Procedencia,
    ResultadoBusqueda,
    cargar_corpus,
    documentos_de_catalogo,
    documentos_de_faqs,
    firma_causal_de_factset,
)
from packages.retriever.saneador import sanear_detallado
from packages.retriever.vectorial import Embedder, ErrorEmbedder, IndiceVectorial

__all__ = [
    "K_RRF",
    "ContextoRecuperado",
    "FragmentoContexto",
    "Recuperador",
    "fusion_rrf",
    "recuperador_por_defecto",
    "recuperar",
    "reiniciar_recuperador",
]

_LOG = logging.getLogger(__name__)

#: Constante de la fusión Reciprocal Rank Fusion. 60 es el valor de la literatura y el
#: que fija la especificación: amortigua las diferencias entre las cabezas de los dos
#: rankings y evita que un puntaje BM25 alto arrolle a todo el ranking vectorial.
K_RRF = 60

#: Tope de conceptos que se inyectan al prompt. Un recibo con más conceptos variados que
#: esto no es un problema de recuperación: es un caso de derivación.
_MAX_CONCEPTOS = 10

#: Casuísticas por respuesta: una guía la narración; dos ya se contradicen entre sí.
_MAX_CASUISTICAS = 2


# --------------------------------------------------------------------------- #
# Fusión Reciprocal Rank Fusion
# --------------------------------------------------------------------------- #
def fusion_rrf(
    rankings: Sequence[Sequence[ResultadoBusqueda]],
    *,
    k_rrf: int = K_RRF,
    pesos: Sequence[float] | None = None,
    limite: int | None = None,
) -> list[ResultadoBusqueda]:
    """Fusiona varios rankings con Reciprocal Rank Fusion.

    ``RRF(d) = Σ_r peso_r / (k + posicion_r(d))``. Se fusiona por **posición**, no por
    puntaje: BM25 y el coseno viven en escalas incomparables y normalizarlas exigiría
    supuestos que no se sostienen. El orden es lo único que ambos miden igual.

    Args:
        rankings: listas ya ordenadas, cada una de una fuente.
        k_rrf: constante de amortiguación.
        pesos: peso por ranking (por defecto, todos 1.0).
        limite: número máximo de resultados devueltos.

    Returns:
        Resultados fusionados, ordenados por puntaje RRF descendente y con ``doc_id``
        como desempate para que el resultado sea determinista.
    """
    factores = list(pesos) if pesos is not None else [1.0] * len(rankings)
    if len(factores) != len(rankings):
        raise ValueError("hay que dar un peso por ranking")

    acumulado: dict[str, float] = {}
    detalles: dict[str, dict[str, float]] = {}
    documentos: dict[str, DocumentoCorpus] = {}

    for ranking, peso in zip(rankings, factores, strict=True):
        for posicion, resultado in enumerate(ranking, start=1):
            doc_id = resultado.doc_id
            acumulado[doc_id] = acumulado.get(doc_id, 0.0) + peso / (k_rrf + posicion)
            documentos.setdefault(doc_id, resultado.documento)
            detalle = detalles.setdefault(doc_id, {})
            detalle[str(resultado.procedencia)] = resultado.puntaje
            detalle[f"pos_{resultado.procedencia}"] = float(posicion)

    ordenados = sorted(acumulado.items(), key=lambda par: (-par[1], par[0]))
    if limite is not None:
        ordenados = ordenados[:limite]

    return [
        ResultadoBusqueda(
            documento=documentos[doc_id],
            puntaje=puntaje,
            procedencia=Procedencia.RRF,
            posicion=posicion,
            detalle={**detalles[doc_id], "rrf": puntaje},
        )
        for posicion, (doc_id, puntaje) in enumerate(ordenados, start=1)
    ]


# --------------------------------------------------------------------------- #
# Contexto devuelto
# --------------------------------------------------------------------------- #
class FragmentoContexto(BaseModel):
    """Un documento recuperado, **ya saneado**, listo para el prompt.

    Es el único tipo que sale del retriever hacia la capa LLM. Su ``texto`` no contiene
    dígitos: las cifras del corpus se sustituyeron por marcadores y quedaron listadas en
    ``retirados`` para la auditoría.
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    corpus: CorpusRag
    titulo: str = Field(description="Saneado")
    texto: str = Field(description="Saneado: sin ninguna cifra")
    puntaje: float = 0.0
    procedencia: Procedencia
    retirados: list[str] = Field(
        default_factory=list, description="Cifras del corpus que se neutralizaron"
    )
    metadatos: dict[str, Any] = Field(default_factory=dict)

    @property
    def tipo_evidencia(self) -> TipoEvidencia:
        """Traduce el corpus al vocabulario de ``ItemEvidencia``."""
        return {
            CorpusRag.CATALOGO: TipoEvidencia.CATALOGO,
            CorpusRag.FAQ: TipoEvidencia.FAQ,
            CorpusRag.CASUISTICA: TipoEvidencia.CASUISTICA,
        }[self.corpus]

    def a_item_evidencia(self, limite_snippet: int = 240) -> ItemEvidencia:
        """Proyecta a ``ItemEvidencia`` para ``GET /v1/evidencia/{explicacion_id}``."""
        snippet = self.texto if len(self.texto) <= limite_snippet else (
            self.texto[: limite_snippet - 1].rstrip() + "…"
        )
        return ItemEvidencia(tipo=str(self.tipo_evidencia), ref_id=self.doc_id, snippet=snippet)


def _fragmento(resultado: ResultadoBusqueda) -> FragmentoContexto:
    """Sanea un resultado de búsqueda y lo convierte en fragmento de contexto.

    Es el **único** camino por el que un documento del corpus llega al exterior del
    retriever, y pasa siempre por el saneador. No hay una variante "sin sanear".
    """
    titulo = sanear_detallado(resultado.documento.titulo)
    cuerpo = sanear_detallado(resultado.documento.contenido_prompt())
    return FragmentoContexto(
        doc_id=resultado.doc_id,
        corpus=resultado.corpus,
        titulo=titulo.texto,
        texto=cuerpo.texto,
        puntaje=round(resultado.puntaje, 6),
        procedencia=resultado.procedencia,
        retirados=[*titulo.originales, *cuerpo.originales],
        metadatos=dict(resultado.documento.metadatos),
    )


class ContextoRecuperado(BaseModel):
    """Las tres partes de la recuperación, separadas y con su procedencia.

    Se devuelven por separado a propósito: el generador no las usa igual. Las fichas de
    catálogo definen conceptos, las FAQs aportan formulaciones probadas y la casuística
    dicta el **orden** del relato. Fundirlas en una lista plana perdería esa información.
    """

    model_config = ConfigDict(extra="forbid")

    consulta: str = ""
    firma_causal: str = ""
    conceptos_filtro: list[str] = Field(
        default_factory=list, description="concepto_id del FactSet usados como filtro"
    )
    conceptos: list[FragmentoContexto] = Field(
        default_factory=list, description="Catálogo, por lookup de clave"
    )
    faqs: list[FragmentoContexto] = Field(
        default_factory=list, description="FAQ, por híbrido BM25 + vectorial con RRF"
    )
    casuisticas: list[FragmentoContexto] = Field(
        default_factory=list, description="Casuísticas, por firma causal"
    )
    conceptos_fuera_catalogo: list[str] = Field(
        default_factory=list,
        description="Conceptos del recibo sin ficha: regla dura de derivación",
    )
    degradado: bool = False
    motivos: list[str] = Field(default_factory=list, description="Por qué se degradó")
    metricas: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @property
    def fragmentos(self) -> list[FragmentoContexto]:
        """Los tres corpus en una sola lista, en orden de inyección al prompt."""
        return [*self.conceptos, *self.faqs, *self.casuisticas]

    @property
    def vacio(self) -> bool:
        """Verdadero si no se recuperó nada (la explicación seguirá siendo posible)."""
        return not self.fragmentos

    @property
    def sustituciones(self) -> list[str]:
        """Todas las cifras del corpus que se neutralizaron, para la auditoría."""
        return [retirado for fragmento in self.fragmentos for retirado in fragmento.retirados]

    @property
    def guion_sugerido(self) -> list[str]:
        """Orden narrativo que propone la casuística principal (vacío si no hay)."""
        if not self.casuisticas:
            return []
        guion = self.casuisticas[0].metadatos.get("guion")
        return [str(paso) for paso in guion] if isinstance(guion, list) else []

    @property
    def advertencia_narrativa(self) -> str | None:
        """Malentendido típico que la casuística principal pide prevenir."""
        if not self.casuisticas:
            return None
        advertencia = self.casuisticas[0].metadatos.get("advertencia")
        return str(advertencia) if advertencia else None

    # ------------------------------------------------------------------ #
    def a_prompt(self, *, incluir_guion: bool = True) -> str:
        """Bloque ``CONTEXTO`` del prompt, **ya saneado**.

        Es el bloque 3 de los cuatro fijos de la sección 5.2. Se cierra con una
        instrucción explícita: el contexto define conceptos, no aporta cifras. Esa frase
        no es decorativa — es la que evita que el modelo trate un marcador como si fuera
        un dato del cliente.
        """
        if self.vacio:
            return (
                "### CONTEXTO\n"
                "(sin documentos recuperados; explique únicamente con el FACTSET)\n"
            )

        lineas: list[str] = [
            "### CONTEXTO (definiciones y guías de redacción; NO contiene datos de este cliente)"
        ]

        if self.conceptos:
            lineas.append("\n[CONCEPTOS DEL RECIBO — definiciones oficiales del catálogo]")
            for fragmento in self.conceptos:
                concepto_id = fragmento.metadatos.get("concepto_id", fragmento.doc_id)
                lineas.append(f"- {concepto_id} ({fragmento.titulo}): {fragmento.texto}")

        if self.faqs:
            lineas.append("\n[PREGUNTAS FRECUENTES — formulaciones ya validadas con clientes]")
            for fragmento in self.faqs:
                lineas.append(f"- {fragmento.titulo}\n  {fragmento.texto}")

        if self.casuisticas:
            lineas.append("\n[CASUÍSTICA — cómo se cuenta este caso]")
            for fragmento in self.casuisticas:
                lineas.append(f"- {fragmento.titulo}: {fragmento.texto}")
            if incluir_guion and self.guion_sugerido:
                lineas.append("  Orden sugerido: " + " > ".join(self.guion_sugerido))
            if self.advertencia_narrativa:
                lineas.append("  Cuidado: " + self.advertencia_narrativa)

        lineas.append(
            "\nEste bloque no contiene ninguna cifra: donde el corpus tenía un número aparece "
            "un marcador entre comillas angulares. Todas las cifras de su respuesta deben salir "
            "del bloque FACTSET, sin excepción."
        )
        return "\n".join(lineas)

    def items_evidencia(self) -> list[ItemEvidencia]:
        """Evidencia citable para ``GET /v1/evidencia/{explicacion_id}``."""
        return [fragmento.a_item_evidencia() for fragmento in self.fragmentos]

    def resumen_auditoria(self) -> dict[str, Any]:
        """Carga útil del evento ``RETRIEVE`` de la cadena de auditoría.

        Incluye qué se recuperó, de dónde y **cuántas cifras se neutralizaron**: es la
        prueba, en el log, de que ninguna cifra del corpus pudo llegar a la respuesta.
        """
        return {
            "firma_causal": self.firma_causal,
            "conceptos_filtro": list(self.conceptos_filtro),
            "conceptos": [fragmento.doc_id for fragmento in self.conceptos],
            "faqs": [
                {"doc_id": fragmento.doc_id, "puntaje": fragmento.puntaje}
                for fragmento in self.faqs
            ],
            "casuisticas": [
                {"doc_id": fragmento.doc_id, "procedencia": str(fragmento.procedencia)}
                for fragmento in self.casuisticas
            ],
            "conceptos_fuera_catalogo": list(self.conceptos_fuera_catalogo),
            "cifras_neutralizadas": len(self.sustituciones),
            "cifras_neutralizadas_detalle": self.sustituciones[:50],
            "degradado": self.degradado,
            "motivos": list(self.motivos),
            **self.metricas,
        }


# --------------------------------------------------------------------------- #
# Recuperador
# --------------------------------------------------------------------------- #
class Recuperador:
    """Mantiene los índices vivos y ejecuta la recuperación de los tres corpus."""

    def __init__(
        self,
        corpus: CorpusCompleto,
        *,
        indice_vectorial: IndiceVectorial | None = None,
        indice_bm25: IndiceBM25 | None = None,
        sincronizar: bool = True,
    ) -> None:
        self.corpus = corpus
        self.catalogo: IndiceCatalogo = corpus.indice_catalogo
        self.casuisticas: IndiceCasuisticas = corpus.indice_casuisticas
        self.bm25 = indice_bm25 or IndiceBM25(documentos_de_faqs(corpus.faqs))
        self.vectorial = indice_vectorial or IndiceVectorial()
        self.motivos_arranque: list[str] = []
        if sincronizar:
            # Idempotente: si el índice ya está al día no cuesta ni una llamada de
            # embeddings. Sirve para que un proceso recién arrancado con índice en
            # memoria funcione sin haber pasado por la CLI de indexado.
            try:
                self.vectorial.indexar(corpus.documentos())
            except Exception as error:
                # Embedder caído, base sin migrar, dimensión cambiada: da igual la
                # causa. El recuperador arranca y sirve BM25 puro. Construirlo nunca
                # puede fallar: es lo que tiene que sobrevivir para que la explicación
                # siga siendo posible aunque la infraestructura no acompañe.
                detalle = error if isinstance(error, ErrorEmbedder) else f"{type(error).__name__}: {error}"
                self.motivos_arranque.append(f"no se pudo sincronizar el índice vectorial: {detalle}")
                _LOG.warning("arranque sin índice vectorial (%s); se sirve BM25 puro", detalle)

    # ------------------------------------------------------------------ #
    @classmethod
    def desde_corpus(
        cls,
        ruta: str | Path | None = None,
        *,
        embedder: Embedder | None = None,
        indice_vectorial: IndiceVectorial | None = None,
        sincronizar: bool = True,
    ) -> Recuperador:
        """Carga los tres corpus del disco y construye los índices."""
        corpus = cargar_corpus(ruta)
        indice = indice_vectorial or IndiceVectorial(embedder)
        return cls(corpus, indice_vectorial=indice, sincronizar=sincronizar)

    # ------------------------------------------------------------------ #
    def _consulta_efectiva(self, factset: FactSet, utterance: str) -> str:
        """Consulta de búsqueda: el mensaje del cliente, o uno derivado de las causas.

        El ``utterance`` puede llegar vacío (la App abre la explicación sin que el
        cliente escriba nada). En ese caso la consulta se construye con las etiquetas de
        causa del FactSet, que es exactamente lo que el cliente habría preguntado.
        """
        texto = (utterance or "").strip()
        if texto:
            return texto
        etiquetas = [causa.etiqueta_cliente for causa in factset.causas_agregadas]
        base = "por qué cambió mi recibo este mes"
        return f"{base} {' '.join(etiquetas)}".strip()

    def _conceptos_del_factset(self, factset: FactSet) -> list[str]:
        """``concepto_id`` del recibo, con los que variaron primero.

        El orden importa: es el que decide qué fichas entran en el prompt cuando hay más
        conceptos que el tope.
        """
        explicables = [linea.concepto_id for linea in factset.lineas_explicables()]
        resto = [
            linea.concepto_id for linea in factset.lineas if linea.concepto_id not in explicables
        ]
        return [*explicables, *resto]

    # -- 1. Catálogo: lookup por clave ---------------------------------- #
    def _recuperar_conceptos(self, concepto_ids: Sequence[str]) -> list[FragmentoContexto]:
        """Lookup por clave. Sin búsqueda: el ``concepto_id`` ya viene dado."""
        fichas = self.catalogo.obtener_varios(concepto_ids[:_MAX_CONCEPTOS])
        documentos = documentos_de_catalogo(fichas)
        return [
            _fragmento(
                ResultadoBusqueda(
                    documento=documento,
                    puntaje=1.0,
                    procedencia=Procedencia.LOOKUP_CLAVE,
                    posicion=posicion,
                )
            )
            for posicion, documento in enumerate(documentos, start=1)
        ]

    # -- 2. FAQ: híbrido BM25 + vectorial con RRF ----------------------- #
    def _recuperar_faqs(
        self,
        consulta: str,
        concepto_ids: Sequence[str],
        k: int,
        motivos: list[str],
    ) -> tuple[list[FragmentoContexto], bool]:
        """Búsqueda híbrida filtrada por los conceptos del FactSet.

        Returns:
            Los fragmentos y si hubo degradación (vector caído → BM25 puro).
        """
        # Se piden más candidatos de los que se devuelven: RRF necesita profundidad en
        # ambos rankings para que la fusión aporte algo.
        profundidad = max(k * 3, 10)
        lexico = self.bm25.buscar(
            consulta, k=profundidad, conceptos=concepto_ids, corpus=CorpusRag.FAQ
        )

        degradado = False
        vectorial: list[ResultadoBusqueda] = []
        try:
            vectorial = self.vectorial.buscar(
                consulta, k=profundidad, corpus=CorpusRag.FAQ, conceptos=concepto_ids
            )
        except ErrorEmbedder as error:
            degradado = True
            motivos.append(f"búsqueda vectorial no disponible: {error}")
            _LOG.warning("FAQ: se degrada a BM25 puro (%s)", error)

        fusionados = fusion_rrf([lexico, vectorial], k_rrf=K_RRF, limite=k)
        return [_fragmento(resultado) for resultado in fusionados], degradado

    # -- 3. Casuística: por firma causal -------------------------------- #
    def _recuperar_casuisticas(
        self, firma: str, consulta: str, k: int, motivos: list[str]
    ) -> tuple[list[FragmentoContexto], bool]:
        """Coincidencia exacta de firma y, si falta, similitud vectorial.

        Si hay coincidencia exacta se devuelve **solo esa**: el escenario está
        determinado y una segunda casuística solo puede contradecir a la primera sobre
        cómo contar el mismo caso. La similitud vectorial es el plan B para
        combinaciones no previstas (típicamente, escenarios compuestos).
        """
        fragmentos: list[FragmentoContexto] = []
        vistos: set[str] = set()

        exacta = self.casuisticas.por_firma(firma)
        if exacta is not None:
            documento = exacta.a_documento()
            vistos.add(documento.doc_id)
            return [
                _fragmento(
                    ResultadoBusqueda(
                        documento=documento,
                        puntaje=1.0,
                        procedencia=Procedencia.FIRMA_CAUSAL,
                        posicion=1,
                    )
                )
            ], False

        degradado = False
        try:
            candidatos = self.vectorial.buscar(
                f"{firma} {consulta}", k=k + len(vistos), corpus=CorpusRag.CASUISTICA
            )
        except ErrorEmbedder as error:
            degradado = True
            candidatos = []
            motivos.append(f"casuísticas sin búsqueda vectorial: {error}")
            _LOG.warning("casuísticas: sin vector (%s)", error)

        if not fragmentos and not candidatos:
            motivos.append(f"sin casuística para la firma {firma}")

        for resultado in candidatos:
            if resultado.doc_id in vistos:
                continue
            fragmentos.append(_fragmento(resultado))
            vistos.add(resultado.doc_id)
            if len(fragmentos) >= k:
                break
        return fragmentos, degradado

    # ------------------------------------------------------------------ #
    def recuperar(
        self,
        factset: FactSet,
        utterance: str = "",
        k: int = 5,
        *,
        k_casuisticas: int = _MAX_CASUISTICAS,
        conceptos_extra: Iterable[str] = (),
    ) -> ContextoRecuperado:
        """Recupera el contexto de los tres corpus para explicar un recibo.

        Args:
            factset: hechos ya validados. Es la fuente del filtro por concepto y de la
                firma causal; el retriever **no** lee la base de facturación.
            utterance: mensaje del cliente, como dato. Nunca se interpreta como
                instrucción: aquí solo se usa como consulta de búsqueda.
            k: número de FAQs a devolver.
            k_casuisticas: número de guiones narrativos (más de dos se contradicen).
            conceptos_extra: conceptos adicionales a documentar (p. ej. uno que el
                cliente nombró y que no varió).

        Returns:
            El contexto **saneado**, con las tres partes separadas y su procedencia.
        """
        motivos: list[str] = list(self.motivos_arranque)
        consulta = self._consulta_efectiva(factset, utterance)
        firma = firma_causal_de_factset(factset)

        concepto_ids = self._conceptos_del_factset(factset)
        for concepto_id in conceptos_extra:
            if concepto_id not in concepto_ids:
                concepto_ids.append(concepto_id)

        fuera_catalogo = self.catalogo.faltantes(concepto_ids)
        if fuera_catalogo:
            motivos.append("conceptos fuera de catálogo: " + ", ".join(fuera_catalogo))

        conceptos = self._recuperar_conceptos(concepto_ids)
        faqs, degradado_faq = self._recuperar_faqs(consulta, concepto_ids, k, motivos)
        casuisticas, degradado_cas = self._recuperar_casuisticas(
            firma, consulta, k_casuisticas, motivos
        )

        if not self.vectorial.disponible_bd and self.vectorial.motivo_degradacion:
            motivos.append(f"índice vectorial en memoria: {self.vectorial.motivo_degradacion}")

        contexto = ContextoRecuperado(
            consulta=consulta,
            firma_causal=firma,
            conceptos_filtro=concepto_ids,
            conceptos=conceptos,
            faqs=faqs,
            casuisticas=casuisticas,
            conceptos_fuera_catalogo=fuera_catalogo,
            degradado=degradado_faq or degradado_cas,
            motivos=motivos,
            metricas={
                "bm25_implementacion": self.bm25.implementacion,
                "respaldo_vectorial": "pgvector" if self.vectorial.disponible_bd else "memoria",
                "modelo_embeddings": self.vectorial.modelo,
                "k": k,
                "k_rrf": K_RRF,
            },
        )
        _LOG.info(
            "RETRIEVE firma=%s conceptos=%d faqs=%d casuisticas=%d cifras_neutralizadas=%d",
            firma,
            len(conceptos),
            len(faqs),
            len(casuisticas),
            len(contexto.sustituciones),
        )
        return contexto

    # ------------------------------------------------------------------ #
    def explicar_concepto(self, concepto_id: str) -> FragmentoContexto | None:
        """Ficha saneada de un concepto suelto (``GET /v1/catalogo/{concepto_id}``)."""
        ficha = self.catalogo.obtener(concepto_id)
        if ficha is None:
            return None
        documento = documentos_de_catalogo([ficha])[0]
        return _fragmento(
            ResultadoBusqueda(
                documento=documento, puntaje=1.0, procedencia=Procedencia.LOOKUP_CLAVE, posicion=1
            )
        )

    def reindexar(self, *, forzar: bool = False) -> dict[str, int]:
        """Reconstruye BM25 y sincroniza el índice vectorial. Idempotente."""
        self.bm25.reconstruir(documentos_de_faqs(self.corpus.faqs))
        return self.vectorial.indexar(self.corpus.documentos(), forzar=forzar)

    def estado(self) -> dict[str, Any]:
        """Resumen operativo para ``/salud`` y para el arranque."""
        return {
            "corpus": self.corpus.resumen(),
            "origen": self.corpus.origen,
            "bm25": {"documentos": len(self.bm25), "implementacion": self.bm25.implementacion},
            "vectorial": self.vectorial.estado(),
            "casuisticas_indexadas": len(self.casuisticas),
        }


# --------------------------------------------------------------------------- #
# Recuperador por defecto del proceso
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def recuperador_por_defecto() -> Recuperador:
    """Recuperador compartido del proceso (índices construidos una sola vez)."""
    return Recuperador.desde_corpus()


def reiniciar_recuperador() -> None:
    """Descarta el recuperador cacheado (tests, recarga de corpus, cambio de modelo)."""
    recuperador_por_defecto.cache_clear()


def recuperar(
    factset: FactSet,
    utterance: str = "",
    k: int = 5,
    *,
    recuperador: Recuperador | None = None,
    **extras: Any,
) -> ContextoRecuperado:
    """Recupera el contexto de los tres corpus. **Punto de entrada del retriever.**

    Args:
        factset: hechos validados del recibo.
        utterance: mensaje del cliente (dato, nunca instrucción).
        k: número de FAQs a devolver.
        recuperador: instancia concreta; por defecto, la compartida del proceso.
        **extras: se reenvían a :meth:`Recuperador.recuperar` (``k_casuisticas``,
            ``conceptos_extra``).

    Returns:
        :class:`ContextoRecuperado` con las tres partes separadas y **ya saneadas**.
    """
    activo = recuperador or recuperador_por_defecto()
    return activo.recuperar(factset, utterance, k, **extras)
