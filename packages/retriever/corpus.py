"""Los tres corpus recuperables. **El recibo no está aquí.**

Tesis del proyecto (regla innegociable nº 3): el recibo NO se vectoriza. Es un objeto
estructurado que se consulta por clave (``cuenta_id`` + ``periodo``) y se compara línea
a línea en el motor determinístico. Buscar un importe por similitud coseno sería
sustituir aritmética exacta por una corazonada.

Lo que sí se recupera son tres corpus de **conocimiento**, y cada uno con el modo de
acceso que le corresponde — la tabla de la sección 6 de la especificación:

===================  =========================================  ==================
Corpus               Acceso                                     Vector
===================  =========================================  ==================
``concepto_catalogo``  **lookup por clave** ``concepto_id``       secundario
``faq``                híbrido BM25 + vectorial, RRF k=60         sí
``casuistica``         vectorial por **firma causal**             sí
===================  =========================================  ==================

La diferencia no es cosmética. El ``concepto_id`` **ya viene dado** por el FactSet: no
hay nada que adivinar, y usar búsqueda semántica para resolver una clave conocida sería
introducir un error donde no lo había. Por eso :class:`IndiceCatalogo` **no tiene método
de búsqueda por texto**: solo ``obtener``.

Ninguna cifra de estos documentos llega al prompt: :mod:`packages.retriever.saneador`
las sustituye por marcadores antes de componer el contexto.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import (
    Canal,
    CausaOficial,
    CorpusRag,
    ModalidadRenta,
    TipoMovimiento,
)
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.recibo import ConceptoCatalogo
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas, raiz_proyecto

__all__ = [
    "CASUISTICAS_SEMILLA",
    "PREFIJO_DOC",
    "VAR_ENTORNO_CORPUS",
    "Casuistica",
    "CorpusCompleto",
    "DocumentoCorpus",
    "Faq",
    "IndiceCasuisticas",
    "IndiceCatalogo",
    "Procedencia",
    "ResultadoBusqueda",
    "SignoDelta",
    "cargar_casuisticas",
    "cargar_catalogo",
    "cargar_corpus",
    "cargar_faqs",
    "documentos_de_casuisticas",
    "documentos_de_catalogo",
    "documentos_de_faqs",
    "faqs_desde_catalogo",
    "firma_causal",
    "firma_causal_de_factset",
    "ruta_corpus_por_defecto",
    "signo_delta",
]

_LOG = logging.getLogger(__name__)

#: Variable de entorno que apunta al directorio de corpus generado por ``datagen``.
VAR_ENTORNO_CORPUS = "CORPUS_PATH"

#: Directorio por defecto del corpus (lo escribe ``packages.datagen.generar``).
RUTA_RELATIVA_CORPUS = Path("data") / "sintetico"

#: Prefijo del ``doc_id`` por corpus. Coincide con los valores de ``TipoEvidencia``
#: para que ``ItemEvidencia.tipo`` se derive sin tablas de traducción.
PREFIJO_DOC: dict[CorpusRag, str] = {
    CorpusRag.CATALOGO: "cat",
    CorpusRag.FAQ: "faq",
    CorpusRag.CASUISTICA: "casuistica",
}

#: Signo del delta total dentro de la firma causal.
SignoDelta = Literal["+", "-", "0"]


class Procedencia(StrEnum):
    """De dónde salió un resultado. Se registra para poder auditar la recuperación.

    Detalle interno del retriever (no forma parte del contrato de ``core_domain``),
    pero viaja al evento ``RETRIEVE`` de la cadena de auditoría: es la respuesta a
    "¿por qué este documento acabó en el prompt?".
    """

    LOOKUP_CLAVE = "lookup_clave"
    BM25 = "bm25"
    VECTORIAL = "vectorial"
    RRF = "rrf"
    FIRMA_CAUSAL = "firma_causal"


# --------------------------------------------------------------------------- #
# Documento genérico y resultado de búsqueda
# --------------------------------------------------------------------------- #
class DocumentoCorpus(BaseModel):
    """Unidad indexable: lo que ven BM25 y el índice vectorial.

    ``texto`` es el contenido **sin sanear** (así se guarda y así se busca: el
    saneador destruiría la señal léxica de "S/ 49.90" como ejemplo). El saneado ocurre
    al componer el prompt, que es el único momento en que importa.
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    corpus: CorpusRag
    titulo: str = ""
    texto: str
    conceptos: list[str] = Field(
        default_factory=list,
        description="concepto_id relacionados; vacío = documento transversal",
    )
    causas: list[TipoMovimiento] = Field(default_factory=list)
    firma_causal: str | None = None
    metadatos: dict[str, Any] = Field(default_factory=dict)

    def texto_indexable(self) -> str:
        """Texto que se entrega al tokenizador y al ``Embedder``.

        Incluye todo lo que ayuda a **encontrar** el documento: variantes de la
        pregunta, sinónimos, señales del cliente. Nada de eso tiene por qué acabar en
        el prompt.
        """
        return f"{self.titulo}\n{self.texto}".strip()

    def contenido_prompt(self) -> str:
        """Texto que se **inyecta** en el prompt (antes de sanear).

        Se separa de :meth:`texto_indexable` a propósito: al modelo se le da la
        respuesta de la FAQ, no las diez formas de preguntarla; la definición de
        cliente, no la definición técnica para el asesor. Si el documento no declara un
        contenido específico (por ejemplo, uno cargado a mano), se usa el texto entero.
        """
        especifico = self.metadatos.get("texto_prompt")
        return str(especifico) if especifico else self.texto


class ResultadoBusqueda(BaseModel):
    """Un documento recuperado, con su puntaje y su procedencia."""

    model_config = ConfigDict(extra="forbid")

    documento: DocumentoCorpus
    puntaje: float
    procedencia: Procedencia
    posicion: int = Field(default=0, description="Ranking 1..n dentro de su fuente")
    detalle: dict[str, float] = Field(
        default_factory=dict, description="Puntaje por fuente cuando hubo fusión RRF"
    )

    @property
    def doc_id(self) -> str:
        """Atajo al identificador del documento."""
        return self.documento.doc_id

    @property
    def corpus(self) -> CorpusRag:
        """Atajo al corpus de origen."""
        return self.documento.corpus


# --------------------------------------------------------------------------- #
# Firma causal — la clave con la que se recuperan las casuísticas
# --------------------------------------------------------------------------- #
def signo_delta(valor: int | float | str) -> SignoDelta:
    """Normaliza el signo del delta total a ``"+"``, ``"-"`` o ``"0"``."""
    if isinstance(valor, str):
        limpio = valor.strip()
        if limpio in {"+", "-", "0"}:
            return limpio  # type: ignore[return-value]
        valor = float(limpio)
    if valor > 0:
        return "+"
    if valor < 0:
        return "-"
    return "0"


def firma_causal(
    causas: Iterable[TipoMovimiento | str],
    modalidad: ModalidadRenta | str,
    signo: int | float | str,
) -> str:
    """Firma canónica y estable de un escenario: ``causas ordenadas # modalidad # signo``.

    Es la clave de recuperación de las casuísticas. Debe ser **idéntica** para el mismo
    escenario venga de donde venga, así que se canoniza: las causas se deduplican y se
    ordenan alfabéticamente, y el signo se reduce a tres valores.

    Ejemplos::

        firma_causal([CAMBIO_PLAN], ADELANTADA, 4500)      -> "CAMBIO_PLAN#ADELANTADA#+"
        firma_causal([], VENCIDA, 0)                        -> "SIN_CAUSA#VENCIDA#0"
        firma_causal([ALTA_PAQUETE, CAMBIO_PLAN], ...)      -> "ALTA_PAQUETE|CAMBIO_PLAN#..."

    Args:
        causas: movimientos atribuidos (se aceptan enums o sus cadenas).
        modalidad: modalidad de renta del cliente.
        signo: delta total en céntimos, o directamente ``"+"``/``"-"``/``"0"``.

    Returns:
        La firma canónica. Coincide byte a byte con ``FactSet.firma_causal()``: son la
        misma función vista desde los dos lados del contrato.
    """
    etiquetas = sorted({str(TipoMovimiento(causa)) for causa in causas if causa})
    cuerpo = "|".join(etiquetas) or "SIN_CAUSA"
    return f"{cuerpo}#{ModalidadRenta(modalidad)}#{signo_delta(signo)}"


def firma_causal_de_factset(factset: FactSet) -> str:
    """Firma causal de un FactSet, calculada con la función canónica de este módulo.

    ``FactSet.firma_causal()`` produce lo mismo; se recalcula aquí para que el retriever
    no dependa del orden en que el motor haya llenado ``causas_agregadas`` y para que
    exista un único punto donde arreglar la canonización si algún día cambia.
    """
    return firma_causal(
        (causa.causa for causa in factset.causas_agregadas if causa.causa),
        factset.modalidad_renta,
        factset.delta_total_cent,
    )


# --------------------------------------------------------------------------- #
# Corpus 2: FAQ
# --------------------------------------------------------------------------- #
class Faq(BaseModel):
    """Pregunta frecuente anonimizada. Corpus de acceso **híbrido**.

    Es el único corpus donde la búsqueda por texto está justificada: la pregunta del
    cliente ("¿por qué me vino más caro?") es lenguaje libre y hay que emparejarla con
    una formulación del corpus. BM25 aporta la coincidencia literal (los clientes usan
    las palabras del recibo) y el vector aporta la paráfrasis; RRF los fusiona.

    ``conceptos`` es el campo que hace posible el filtro por FactSet: solo se consideran
    las FAQs de los conceptos que **de verdad** aparecen en el recibo del cliente. Una
    FAQ sin conceptos es transversal y compite siempre.
    """

    model_config = ConfigDict(extra="forbid")

    faq_id: str
    pregunta: str
    respuesta: str
    conceptos: list[str] = Field(default_factory=list)
    causas: list[TipoMovimiento] = Field(default_factory=list)
    causas_oficiales: list[CausaOficial] = Field(
        default_factory=list, description="Vocabulario de la ficha (lo que emite datagen)"
    )
    canales: list[Canal] = Field(default_factory=list, description="Canales donde aplica")
    variantes: list[str] = Field(
        default_factory=list, description="Otras formas en que el cliente pregunta lo mismo"
    )
    etiquetas: list[str] = Field(default_factory=list)
    fuente: str = Field(default="faq_anonimizada", description="Procedencia del texto")

    @classmethod
    def desde_registro(cls, registro: dict[str, Any]) -> Faq:
        """Construye una FAQ desde un registro del corpus, sea del formato que sea."""
        return cls.model_validate(_normalizar_registro(registro, _ALIAS_FAQ))

    def a_documento(self) -> DocumentoCorpus:
        """Proyecta la FAQ a documento indexable."""
        cuerpo = "\n".join([self.pregunta, *self.variantes, self.respuesta])
        return DocumentoCorpus(
            doc_id=f"{PREFIJO_DOC[CorpusRag.FAQ]}:{self.faq_id}",
            corpus=CorpusRag.FAQ,
            titulo=self.pregunta,
            texto=cuerpo,
            conceptos=list(self.conceptos),
            causas=list(self.causas),
            metadatos={
                "faq_id": self.faq_id,
                "texto_prompt": self.respuesta,
                "etiquetas": list(self.etiquetas),
                "fuente": self.fuente,
            },
        )


# --------------------------------------------------------------------------- #
# Corpus 3: casuística
# --------------------------------------------------------------------------- #
class Casuistica(BaseModel):
    """Guion narrativo para un escenario de facturación. Corpus **vectorial por firma**.

    No explica conceptos ni responde preguntas: dice **en qué orden se cuenta la
    historia** para que un cliente la entienda. "Primero el total, luego las dos rentas
    que conviven, luego la tabla de tramos, luego el siguiente paso."

    Se recupera por firma causal, no por texto: el escenario ya está determinado por el
    FactSet (causas + modalidad + signo del delta), así que la recuperación empieza por
    una coincidencia exacta y solo cae a similitud vectorial cuando la combinación es
    nueva (por ejemplo, un escenario compuesto que no estaba previsto).
    """

    model_config = ConfigDict(extra="forbid")

    casuistica_id: str
    titulo: str
    descripcion: str = Field(description="Qué le pasó al cliente, en lenguaje simple")
    causas: list[TipoMovimiento] = Field(default_factory=list)
    modalidad: ModalidadRenta | None = Field(
        default=None, description="None = aplica a ambas modalidades"
    )
    signo: SignoDelta = "+"
    guion: list[str] = Field(
        default_factory=list, description="Orden sugerido de bloques de la respuesta"
    )
    senales: list[str] = Field(
        default_factory=list, description="Frases del cliente que suelen acompañar el caso"
    )
    conceptos: list[str] = Field(default_factory=list)
    advertencia: str | None = Field(
        default=None, description="Malentendido típico que la explicación debe prevenir"
    )
    guia_narrativa: str = Field(default="", description="Tono y foco de la respuesta")
    accion_sugerida: str = Field(default="", description="Siguiente paso propio del caso")
    prioridad: int = Field(default=100, description="Menor gana ante empate de firma")

    @classmethod
    def desde_registro(cls, registro: dict[str, Any]) -> Casuistica:
        """Construye una casuística desde un registro del corpus, sea del formato que sea.

        Si el registro trae una ``firma`` calculada por quien lo escribió, se compara con
        la que deriva este módulo: una discrepancia significaría que dos partes del
        sistema canonizan distinto la firma causal y que las casuísticas dejarían de
        recuperarse. Se avisa en el log en vez de dejarlo pasar en silencio.
        """
        crudo = dict(registro)
        firma_declarada = crudo.pop("firma", None)
        casuistica = cls.model_validate(_normalizar_registro(crudo, _ALIAS_CASUISTICA))
        if firma_declarada and firma_declarada not in casuistica.firmas():
            _LOG.warning(
                "casuística %s: firma declarada %r y firma derivada %s no coinciden",
                casuistica.casuistica_id,
                firma_declarada,
                casuistica.firmas(),
            )
        return casuistica

    def firmas(self) -> tuple[str, ...]:
        """Firmas causales que cubre (dos si la casuística vale para ambas modalidades)."""
        modalidades = (
            (self.modalidad,) if self.modalidad is not None else tuple(ModalidadRenta)
        )
        return tuple(firma_causal(self.causas, modalidad, self.signo) for modalidad in modalidades)

    def a_documento(self) -> DocumentoCorpus:
        """Proyecta la casuística a documento indexable.

        El texto indexable incluye las etiquetas de causa y las señales del cliente:
        son lo que da similitud útil cuando la firma exacta no existe.
        """
        partes = [
            self.descripcion,
            self.guia_narrativa,
            "Causas: " + ", ".join(str(causa) for causa in self.causas) if self.causas else "",
            "Modalidad: " + (str(self.modalidad) if self.modalidad else "cualquiera"),
            "Señales: " + " | ".join(self.senales) if self.senales else "",
            "Guion: " + " > ".join(self.guion) if self.guion else "",
            self.advertencia or "",
            self.accion_sugerida,
        ]
        # Al prompt van la situación y la guía de tono; el resto es señal de búsqueda.
        para_prompt = " ".join(parte for parte in (self.descripcion, self.guia_narrativa) if parte)
        return DocumentoCorpus(
            doc_id=f"{PREFIJO_DOC[CorpusRag.CASUISTICA]}:{self.casuistica_id}",
            corpus=CorpusRag.CASUISTICA,
            titulo=self.titulo,
            texto="\n".join(parte for parte in partes if parte),
            conceptos=list(self.conceptos),
            causas=list(self.causas),
            firma_causal=self.firmas()[0],
            metadatos={
                "casuistica_id": self.casuistica_id,
                "texto_prompt": para_prompt,
                "firmas": list(self.firmas()),
                "guion": list(self.guion),
                "advertencia": self.advertencia,
                "accion_sugerida": self.accion_sugerida,
                "prioridad": self.prioridad,
                "signo": self.signo,
                "modalidad": str(self.modalidad) if self.modalidad else None,
            },
        )


# --------------------------------------------------------------------------- #
# Corpus 1: catálogo — acceso por CLAVE, nunca por búsqueda
# --------------------------------------------------------------------------- #
_RE_NO_ALFANUM = re.compile(r"[^a-z0-9]+")


def _clave_sinonimo(termino: str) -> str:
    """Normaliza un sinónimo para el índice exacto (minúsculas, sin tildes, sin signos)."""
    descompuesto = unicodedata.normalize("NFD", termino.strip().lower())
    sin_tildes = "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")
    return _RE_NO_ALFANUM.sub(" ", sin_tildes).strip()


class IndiceCatalogo:
    """Acceso al catálogo de conceptos **por clave**.

    Deliberadamente **no expone búsqueda por texto**. El ``concepto_id`` llega dado por
    el FactSet: resolverlo por similitud sería cambiar un acierto seguro por una
    probabilidad. La única concesión es :meth:`por_sinonimo`, que es igualmente un
    lookup exacto sobre una tabla de sinónimos declarada en ``rules.yaml`` — sirve para
    preguntas sueltas del tipo "¿qué es el prorrateo?" sin FactSet de por medio.

    Para esas preguntas sueltas el catálogo sí se indexa en el vector como corpus
    **secundario** (ver :func:`documentos_de_catalogo`), pero eso no lo usa el flujo
    normal de explicación.
    """

    def __init__(self, conceptos: Iterable[ConceptoCatalogo]) -> None:
        self._por_id: dict[str, ConceptoCatalogo] = {}
        self._por_sinonimo: dict[str, str] = {}
        for concepto in conceptos:
            self._por_id[concepto.concepto_id] = concepto
            for termino in (concepto.nombre_comercial, concepto.nombre_tecnico, *concepto.sinonimos):
                clave = _clave_sinonimo(termino)
                # El primer concepto que reclama un sinónimo se lo queda: el orden del
                # catálogo es estable, así que la resolución es determinista.
                if clave and clave not in self._por_sinonimo:
                    self._por_sinonimo[clave] = concepto.concepto_id

    def __len__(self) -> int:
        return len(self._por_id)

    def __contains__(self, concepto_id: object) -> bool:
        return concepto_id in self._por_id

    @property
    def conceptos(self) -> list[ConceptoCatalogo]:
        """Todos los conceptos, en el orden del catálogo."""
        return list(self._por_id.values())

    def obtener(self, concepto_id: str) -> ConceptoCatalogo | None:
        """Lookup por clave. ``None`` si el concepto no está catalogado.

        Un ``None`` aquí no es un fallo de recuperación: es una **regla dura de
        derivación** (concepto fuera de catálogo, sección 4.8).
        """
        return self._por_id.get(concepto_id)

    def obtener_varios(self, concepto_ids: Iterable[str]) -> list[ConceptoCatalogo]:
        """Lookup por clave de varios conceptos, sin repetir y en el orden pedido."""
        vistos: set[str] = set()
        encontrados: list[ConceptoCatalogo] = []
        for concepto_id in concepto_ids:
            if concepto_id in vistos:
                continue
            vistos.add(concepto_id)
            concepto = self._por_id.get(concepto_id)
            if concepto is not None:
                encontrados.append(concepto)
        return encontrados

    def faltantes(self, concepto_ids: Iterable[str]) -> list[str]:
        """Conceptos pedidos que no existen en el catálogo (candidatos a derivación)."""
        return sorted({cid for cid in concepto_ids if cid not in self._por_id})

    def por_sinonimo(self, termino: str) -> ConceptoCatalogo | None:
        """Resuelve un término del cliente a un concepto, por tabla exacta de sinónimos."""
        concepto_id = self._por_sinonimo.get(_clave_sinonimo(termino))
        return self._por_id.get(concepto_id) if concepto_id else None

    def documentos(self) -> list[DocumentoCorpus]:
        """Proyección a documentos para el índice vectorial **secundario**."""
        return documentos_de_catalogo(self.conceptos)


class IndiceCasuisticas:
    """Índice de casuísticas por firma causal (coincidencia exacta)."""

    def __init__(self, casuisticas: Iterable[Casuistica]) -> None:
        self._todas: list[Casuistica] = list(casuisticas)
        self._por_firma: dict[str, Casuistica] = {}
        self._por_id: dict[str, Casuistica] = {}
        for casuistica in sorted(self._todas, key=lambda c: (c.prioridad, c.casuistica_id)):
            self._por_id[casuistica.casuistica_id] = casuistica
            for firma in casuistica.firmas():
                self._por_firma.setdefault(firma, casuistica)

    def __len__(self) -> int:
        return len(self._todas)

    @property
    def casuisticas(self) -> list[Casuistica]:
        """Todas las casuísticas cargadas."""
        return list(self._todas)

    def por_firma(self, firma: str) -> Casuistica | None:
        """Coincidencia exacta de firma causal."""
        return self._por_firma.get(firma)

    def por_id(self, casuistica_id: str) -> Casuistica | None:
        """Lookup por identificador."""
        return self._por_id.get(casuistica_id)

    def documentos(self) -> list[DocumentoCorpus]:
        """Proyección a documentos para el índice vectorial."""
        return documentos_de_casuisticas(self._todas)


# --------------------------------------------------------------------------- #
# Proyección a documentos
# --------------------------------------------------------------------------- #
def documentos_de_catalogo(conceptos: Iterable[ConceptoCatalogo]) -> list[DocumentoCorpus]:
    """Convierte fichas de catálogo en documentos indexables (uso secundario)."""
    documentos: list[DocumentoCorpus] = []
    for concepto in conceptos:
        indexable = [
            concepto.definicion_cliente,
            concepto.definicion_tecnica,
            concepto.ejemplo_variacion or "",
            "También conocido como: " + ", ".join(concepto.sinonimos) if concepto.sinonimos else "",
        ]
        # Al prompt va la definición de cliente y el ejemplo de variación. La
        # definición técnica es para el asesor: si llega al modelo, reaparece en la
        # respuesta con el vocabulario que el desafío pide evitar.
        para_prompt = [concepto.definicion_cliente, concepto.ejemplo_variacion or ""]
        documentos.append(
            DocumentoCorpus(
                doc_id=f"{PREFIJO_DOC[CorpusRag.CATALOGO]}:{concepto.concepto_id}",
                corpus=CorpusRag.CATALOGO,
                titulo=concepto.nombre_comercial,
                texto="\n".join(parte for parte in indexable if parte),
                conceptos=[concepto.concepto_id],
                causas=list(concepto.causas_permitidas),
                metadatos={
                    "concepto_id": concepto.concepto_id,
                    "texto_prompt": " ".join(parte for parte in para_prompt if parte),
                    "familia": str(concepto.familia),
                    "prorrateable": concepto.prorrateable,
                    "visible_cliente": concepto.visible_cliente,
                },
            )
        )
    return documentos


def documentos_de_faqs(faqs: Iterable[Faq]) -> list[DocumentoCorpus]:
    """Convierte FAQs en documentos indexables."""
    return [faq.a_documento() for faq in faqs]


def documentos_de_casuisticas(casuisticas: Iterable[Casuistica]) -> list[DocumentoCorpus]:
    """Convierte casuísticas en documentos indexables."""
    return [casuistica.a_documento() for casuistica in casuisticas]


# --------------------------------------------------------------------------- #
# Carga desde disco (lo que escribe datagen)
# --------------------------------------------------------------------------- #
def ruta_corpus_por_defecto() -> Path:
    """Directorio del corpus: ``$CORPUS_PATH`` si está definida, si no ``data/sintetico``."""
    desde_entorno = os.getenv(VAR_ENTORNO_CORPUS)
    if desde_entorno:
        return Path(desde_entorno)
    return raiz_proyecto() / RUTA_RELATIVA_CORPUS


# --------------------------------------------------------------------------- #
# ACL con el vocabulario de datagen
# --------------------------------------------------------------------------- #
# ``packages.datagen`` escribe el corpus con sus propios nombres de campo
# (``explicacion_simple``, ``situacion``, ``estructura``…). Traducirlos aquí, en un
# único sitio y de forma explícita, evita dos males: acoplar el retriever al generador
# y hacer permisivos los modelos con ``extra="allow"``, que dejaría pasar en silencio
# un campo mal escrito. Un campo desconocido que no esté en estas tablas **sigue
# fallando ruidosamente**.
_CAMPOS_DERIVADOS: frozenset[str] = frozenset({"texto_indexable"})

_ALIAS_CATALOGO: dict[str, str] = {
    "explicacion_simple": "definicion_cliente",
    "explicacion_detalle": "definicion_tecnica",
    "cuando_aparece": "ejemplo_variacion",
}

_ALIAS_FAQ: dict[str, str] = {
    "variantes_pregunta": "variantes",
}

_ALIAS_CASUISTICA: dict[str, str] = {
    "situacion": "descripcion",
    "estructura": "guion",
    "signo_delta": "signo",
    "error_frecuente": "advertencia",
    "senales_cliente": "senales",
}


def _normalizar_registro(registro: dict[str, Any], alias: dict[str, str]) -> dict[str, Any]:
    """Traduce los nombres de campo del corpus a los de este módulo.

    Descarta los campos derivados (``texto_indexable`` se recalcula aquí, y guardarlo
    sería tener dos versiones de la verdad).
    """
    normalizado: dict[str, Any] = {}
    for clave, valor in registro.items():
        if clave in _CAMPOS_DERIVADOS:
            continue
        destino = alias.get(clave, clave)
        # Un alias no puede pisar un campo que ya venía con su nombre nativo.
        if destino not in normalizado or normalizado[destino] in (None, "", [], {}):
            normalizado[destino] = valor
    return normalizado


def _leer_registros(ruta: Path) -> list[dict[str, Any]]:
    """Lee una lista de registros de un ``.json``, ``.jsonl``, ``.yaml`` o ``.yml``.

    Acepta tanto una lista en la raíz como un mapa con la lista bajo ``items``,
    ``datos`` o el nombre del corpus: datagen y los ficheros escritos a mano no tienen
    por qué coincidir en la envoltura.
    """
    sufijo = ruta.suffix.lower()
    texto = ruta.read_text(encoding="utf-8")
    if sufijo == ".jsonl":
        return [json.loads(linea) for linea in texto.splitlines() if linea.strip()]
    datos = yaml.safe_load(texto) if sufijo in {".yaml", ".yml"} else json.loads(texto)
    if isinstance(datos, list):
        return list(datos)
    if isinstance(datos, dict):
        for clave in ("items", "datos", "catalogo", "faqs", "casuisticas", "registros"):
            if isinstance(datos.get(clave), list):
                return list(datos[clave])
    raise ValueError(f"{ruta}: se esperaba una lista de registros")


def _buscar_fichero(directorio: Path, nombres: Sequence[str]) -> Path | None:
    """Primer fichero existente entre los nombres candidatos, con cualquier extensión."""
    for nombre in nombres:
        for sufijo in (".json", ".jsonl", ".yaml", ".yml"):
            candidato = directorio / f"{nombre}{sufijo}"
            if candidato.is_file():
                return candidato
    return None


def _conectar_supabase():
    """Conexión a Supabase, o ``None`` si no está configurada o no responde.

    Centralizada para que las tres cargas del corpus degraden igual: sin base de datos el
    sistema arranca con lo local y solo pierde riqueza de contexto, nunca corrección —las
    cifras salen del ``FactSet``, no del corpus.
    """
    import os

    cadena = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not cadena:
        return None
    try:
        import psycopg

        return psycopg.connect(cadena, connect_timeout=15)
    except Exception as exc:
        _LOG.info("corpus: Supabase no disponible (%s); se usa el local", type(exc).__name__)
        return None


def _faqs_de_supabase() -> list[Faq]:
    """Las preguntas frecuentes reales, desde ``faq_externa``.

    Solo se traen las **traducidas al español**: una FAQ en inglés dentro del índice
    ensucia BM25 —comparte pocas palabras con la pregunta de un cliente peruano— y, si se
    recuperase, metería inglés en el contexto del modelo. Las que aún no se han traducido
    se ignoran hasta que ``scripts/traducir_faqs.py`` las procese.
    """
    conexion = _conectar_supabase()
    if conexion is None:
        return []
    try:
        with conexion:
            filas = conexion.execute(
                """
                SELECT faq_id, pregunta, respuesta,
                       coalesce(etiquetas[1], ''), coalesce(origen, 'faq')
                FROM faq
                WHERE activo
                ORDER BY faq_id
                """
            ).fetchall()
    except Exception as exc:
        _LOG.info("FAQ: no se pudo leer de Supabase (%s)", type(exc).__name__)
        return []

    faqs = [
        Faq(
            faq_id=faq_id,
            pregunta=pregunta,
            respuesta=respuesta,
            etiquetas=[intencion] if intencion else [],
            fuente=fuente or "faq_externa",
        )
        for faq_id, pregunta, respuesta, intencion, fuente in filas
    ]
    if faqs:
        _LOG.info("FAQ: %d documentos reales desde Supabase", len(faqs))
    return faqs


def _conceptos_de_supabase() -> list[ConceptoCatalogo]:
    """Conceptos reales del operador, desde ``v_concepto_real``.

    Devuelve lista vacía —nunca falla— si no hay conexión configurada o si la vista no
    existe. El corpus es **color narrativo**: las cifras salen del ``FactSet``, así que
    quedarse sin catálogo empobrece la explicación pero no la vuelve incorrecta, y hacer
    que el sistema no arranque por eso sería desproporcionado.

    ``definicion_cliente`` se deja vacía a propósito: el dataset trae el nombre comercial
    y la clasificación del facturador, no una explicación redactada. Inventarla aquí sería
    volver a poner texto nuestro donde debe haber dato del operador.
    """
    import os

    cadena = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not cadena:
        return []
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg es opcional en esta ruta
        return []
    consulta = """
        SELECT concepto_id, nombre_comercial, grupo
        FROM v_concepto_real
        WHERE considerado AND apariciones >= 5
        ORDER BY apariciones DESC
    """
    try:
        with psycopg.connect(cadena, connect_timeout=15) as conexion:
            filas = conexion.execute(consulta).fetchall()
    except Exception as exc:
        _LOG.info("catálogo: Supabase no disponible (%s); se usa solo el local", type(exc).__name__)
        return []

    salida: list[ConceptoCatalogo] = []
    for concepto_id, nombre, grupo in filas:
        familia = "AJUSTE" if "DESCUENTO" in (grupo or "").upper() else "RECURRENTE"
        salida.append(
            ConceptoCatalogo(
                concepto_id=concepto_id,
                nombre_comercial=nombre or concepto_id,
                familia=familia,
                definicion_cliente="",
                visible_cliente=True,
            )
        )
    if salida:
        _LOG.info("catálogo: %d conceptos reales desde Supabase", len(salida))
    return salida


def cargar_catalogo(
    ruta: str | Path | None = None, reglas: ConfiguracionReglas | None = None
) -> list[ConceptoCatalogo]:
    """Carga el catálogo de conceptos.

    ``rules.yaml`` es la **fuente canónica**: es lo que valida el motor y lo que fija
    ``causas_permitidas``. Un fichero de catálogo en el directorio de corpus solo puede
    **añadir** conceptos que no estén en las reglas (por ejemplo, los que traiga el
    dataset real); nunca sobrescribe los existentes, para que el retriever y el motor
    no puedan discrepar sobre qué es un concepto.

    Args:
        ruta: directorio del corpus. Por defecto ``$CORPUS_PATH`` o ``data/sintetico``.
        reglas: configuración ya cargada (se reutiliza para no releer el YAML).

    Returns:
        Los conceptos del catálogo, los de reglas primero.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    conceptos: list[ConceptoCatalogo] = list(configuracion.catalogo)
    conocidos = {concepto.concepto_id for concepto in conceptos}

    # Supabase antes que el disco: los conceptos reales del operador salen de
    # `v_concepto_real`, que se recalcula sola al recargar el dataset. Los de
    # `rules.yaml` siguen mandando —fijan `causas_permitidas`, que el motor necesita—,
    # pero dejan de ser los únicos: de 31 escritos por el equipo se pasa a los que
    # realmente aparecen en los recibos de Movistar.
    for concepto in _conceptos_de_supabase():
        if concepto.concepto_id in conocidos:
            continue
        conceptos.append(concepto)
        conocidos.add(concepto.concepto_id)

    directorio = Path(ruta) if ruta is not None else ruta_corpus_por_defecto()
    fichero = _buscar_fichero(directorio, ("catalogo", "catalogo_conceptos", "conceptos"))
    if fichero is None:
        return conceptos

    extra = 0
    for registro in _leer_registros(fichero):
        concepto = ConceptoCatalogo.model_validate(
            _normalizar_registro(registro, _ALIAS_CATALOGO)
        )
        if concepto.concepto_id in conocidos:
            continue
        conceptos.append(concepto)
        conocidos.add(concepto.concepto_id)
        extra += 1
    if extra:
        _LOG.info("catálogo: %d conceptos adicionales desde %s", extra, fichero)
    _declarar_al_motor(configuracion, conceptos)
    return conceptos


def _declarar_al_motor(
    configuracion: ConfiguracionReglas, conceptos: list[ConceptoCatalogo]
) -> None:
    """Le dice al motor qué códigos existen en el dataset, además de los de ``rules.yaml``.

    Sin esto, la regla dura ``CONCEPTO_FUERA_CATALOGO`` derivaba a un asesor **toda** cuenta
    real: comprobaba contra los treinta y un conceptos que el equipo modeló a mano, mientras
    el recuperador ya tenía cargados los del dataset. Dos catálogos que no se hablaban, y el
    asistente negándose a explicar precisamente los recibos para los que se construyó.

    Se declara desde aquí y no al arrancar la API porque este es el punto donde el catálogo
    real acaba de cargarse: cualquier otro sitio tendría que volver a consultarlo.
    """
    del_dataset = {
        concepto.concepto_id
        for concepto in conceptos
        if not configuracion.existe_concepto(concepto.concepto_id)
    }
    if del_dataset:
        configuracion.registrar_conceptos_del_dataset(del_dataset)
        _LOG.info("catálogo: %d códigos del dataset declarados al motor", len(del_dataset))


def cargar_faqs(
    ruta: str | Path | None = None,
    *,
    conceptos: Sequence[ConceptoCatalogo] | None = None,
    generar_si_falta: bool = True,
) -> list[Faq]:
    """Carga las FAQs anonimizadas que escribe ``datagen``.

    Si no hay fichero y ``generar_si_falta`` es verdadero, se derivan FAQs del propio
    catálogo (:func:`faqs_desde_catalogo`): el sistema arranca y busca desde el primer
    minuto, sin corpus previo y sin inventar contenido, porque el texto sale de las
    definiciones ya validadas en ``rules.yaml``.
    """
    # Supabase manda. Las 480 FAQs de `faq_externa` son preguntas reales de clientes de
    # telecomunicaciones; el fichero local son 36 que escribimos nosotros, y usarlas
    # convertía la métrica de *Retrieval Accuracy* en una tautología: recuperábamos bien
    # porque preguntábamos con las mismas palabras con que habíamos escrito el corpus.
    desde_bd = _faqs_de_supabase()
    if desde_bd:
        return desde_bd

    directorio = Path(ruta) if ruta is not None else ruta_corpus_por_defecto()
    fichero = _buscar_fichero(directorio, ("faqs", "faq", "faqs_seed", "preguntas_frecuentes"))
    if fichero is not None:
        faqs = [Faq.desde_registro(registro) for registro in _leer_registros(fichero)]
        _LOG.info("FAQ: %d documentos desde %s", len(faqs), fichero)
        return faqs

    if not generar_si_falta:
        _LOG.warning("no se encontró corpus de FAQs en %s; se recupera sin FAQs", directorio)
        return []

    base = list(conceptos) if conceptos is not None else cargar_catalogo(directorio)
    faqs = faqs_desde_catalogo(base)
    _LOG.warning(
        "no se encontró corpus de FAQs en %s; se derivan %d FAQs del catálogo "
        "(ejecute `python -m packages.datagen.generar` para el corpus real)",
        directorio,
        len(faqs),
    )
    return faqs


def _casuisticas_de_supabase() -> list[Casuistica]:
    """Las casuísticas desde la tabla ``casuistica``.

    Se reconstruye el **registro del corpus** —los nombres que usa el fichero JSON— y se
    pasa por :meth:`Casuistica.desde_registro`, el mismo constructor que la ruta de disco.
    Traducir aquí los campos a mano habría creado un segundo mapeo que se desincroniza en
    cuanto alguien añada un campo: el alias vive en un solo sitio, ``_ALIAS_CASUISTICA``.

    ``signo_delta`` viaja como ``smallint`` porque el motor compara signos como enteros;
    el corpus lo escribe como ``"+"``/``"-"``. La traducción es de ida y vuelta y está
    aquí, no en la tabla, para que la columna siga siendo comparable en SQL.
    """
    conexion = _conectar_supabase()
    if conexion is None:
        return []
    try:
        with conexion:
            filas = conexion.execute(
                """
                SELECT casuistica_id, titulo, situacion, modalidad_renta::text, signo_delta,
                       causas::text[], conceptos, estructura, narrativa, error_frecuente,
                       accion_sugerida, senales_cliente, prioridad
                FROM casuistica
                WHERE activo
                ORDER BY prioridad, casuistica_id
                """
            ).fetchall()
    except Exception as exc:
        _LOG.info("casuísticas: no se pudo leer de Supabase (%s)", type(exc).__name__)
        return []

    signo_texto = {1: "+", -1: "-", 0: "0"}
    casuisticas: list[Casuistica] = []
    for fila in filas:
        registro = {
            "casuistica_id": fila[0],
            "titulo": fila[1],
            "situacion": fila[2] or "",
            "modalidad": fila[3],
            "signo_delta": signo_texto.get(fila[4], "0"),
            "causas": list(fila[5] or []),
            "conceptos": list(fila[6] or []),
            "estructura": list(fila[7] or []),
            "guia_narrativa": fila[8] or "",
            "error_frecuente": fila[9] or "",
            "accion_sugerida": fila[10] or "",
            "senales_cliente": list(fila[11] or []),
            "prioridad": fila[12],
        }
        try:
            casuisticas.append(Casuistica.desde_registro(registro))
        except Exception as exc:
            # Una casuística mal formada no puede tumbar el corpus entero: se descarta y
            # se dice cuál, que es lo que permite arreglarla.
            _LOG.warning("casuística %s descartada: %s", fila[0], exc)
    if casuisticas:
        _LOG.info("casuísticas: %d desde Supabase", len(casuisticas))
    return casuisticas


def cargar_casuisticas(ruta: str | Path | None = None) -> list[Casuistica]:
    """Carga las casuísticas del corpus, o la semilla incluida en el paquete.

    **Supabase manda.** Si la tabla ``casuistica`` responde, es la que vale: es donde el
    equipo edita los guiones sin tocar el repositorio, y donde los ve cualquiera del
    grupo. El fichero de disco pasa a ser la semilla que la pobló, y la semilla del
    paquete (:data:`CASUISTICAS_SEMILLA`) solo cubre las firmas causales que nadie traiga.

    Sin conexión se cae al fichero y luego a la semilla, en ese orden. Quedarse sin
    casuísticas no da respuestas falsas —las cifras salen del ``FactSet``— pero sí
    explicaciones más secas, así que degradar es mejor que no arrancar.
    """
    de_supabase = _casuisticas_de_supabase()
    if de_supabase:
        ids_bd = {casuistica.casuistica_id for casuistica in de_supabase}
        firmas_bd = {firma for casuistica in de_supabase for firma in casuistica.firmas()}
        return sorted(
            de_supabase
            + [
                casuistica
                for casuistica in CASUISTICAS_SEMILLA
                if casuistica.casuistica_id not in ids_bd
                and not firmas_bd.issuperset(casuistica.firmas())
            ],
            key=lambda c: (c.prioridad, c.casuistica_id),
        )

    directorio = Path(ruta) if ruta is not None else ruta_corpus_por_defecto()
    fichero = _buscar_fichero(directorio, ("casuisticas", "casuistica", "casuisticas_seed"))
    if fichero is None:
        return sorted(CASUISTICAS_SEMILLA, key=lambda c: (c.prioridad, c.casuistica_id))

    del_disco = [Casuistica.desde_registro(registro) for registro in _leer_registros(fichero)]
    ids = {casuistica.casuistica_id for casuistica in del_disco}
    firmas_cubiertas = {firma for casuistica in del_disco for firma in casuistica.firmas()}

    complemento = [
        casuistica
        for casuistica in CASUISTICAS_SEMILLA
        if casuistica.casuistica_id not in ids
        and not firmas_cubiertas.issuperset(casuistica.firmas())
    ]
    _LOG.info(
        "casuísticas: %d desde %s + %d de la semilla para firmas no cubiertas",
        len(del_disco),
        fichero,
        len(complemento),
    )
    return sorted([*del_disco, *complemento], key=lambda c: (c.prioridad, c.casuistica_id))


def faqs_desde_catalogo(conceptos: Iterable[ConceptoCatalogo]) -> list[Faq]:
    """Deriva FAQs del catálogo: una de definición y otra de variación por concepto.

    No inventa contenido: reutiliza ``definicion_cliente`` y ``ejemplo_variacion``, que
    ya están revisados en ``rules.yaml``. Sirve de corpus de arranque y garantiza que
    todo concepto del FactSet tenga al menos una FAQ que supere el filtro.
    """
    faqs: list[Faq] = []
    for concepto in conceptos:
        if not concepto.visible_cliente:
            continue
        nombre = concepto.nombre_comercial
        faqs.append(
            Faq(
                faq_id=f"CAT-{concepto.concepto_id}-QUE_ES",
                pregunta=f"¿Qué es {nombre} en mi recibo?",
                respuesta=concepto.definicion_cliente,
                conceptos=[concepto.concepto_id],
                causas=list(concepto.causas_permitidas),
                variantes=[
                    f"qué significa {nombre}",
                    f"para qué me cobran {nombre}",
                    *concepto.sinonimos,
                ],
                etiquetas=["definicion", str(concepto.familia)],
                fuente="derivada_del_catalogo",
            )
        )
        if concepto.ejemplo_variacion:
            faqs.append(
                Faq(
                    faq_id=f"CAT-{concepto.concepto_id}-VARIACION",
                    pregunta=f"¿Por qué cambió {nombre} respecto del mes pasado?",
                    respuesta=concepto.ejemplo_variacion,
                    conceptos=[concepto.concepto_id],
                    causas=list(concepto.causas_permitidas),
                    variantes=[
                        f"por qué subió {nombre}",
                        f"por qué bajó {nombre}",
                        f"por qué me vino más caro {nombre}",
                    ],
                    etiquetas=["variacion", str(concepto.familia)],
                    fuente="derivada_del_catalogo",
                )
            )
    return faqs


# --------------------------------------------------------------------------- #
# Semilla de casuísticas — guiones narrativos por escenario
# --------------------------------------------------------------------------- #
def _semilla() -> list[Casuistica]:
    """Construye la semilla de casuísticas de los ocho escenarios de la especificación."""
    M = TipoMovimiento
    return [
        Casuistica(
            casuistica_id="CAS-CAMBIO-PLAN-ADELANTADA-SUBE",
            titulo="Dos rentas en el mismo recibo tras cambiar de plan",
            descripcion=(
                "El cliente cambió de plan a mitad de ciclo y su renta se cobra por "
                "adelantado. El recibo trae la renta completa del ciclo siguiente con el "
                "plan nuevo y, además, el ajuste de los días del ciclo que ya había pagado "
                "con el plan anterior. Por eso el recibo puede subir aunque el plan nuevo "
                "sea más barato: es un efecto de una sola vez, no un cobro permanente."
            ),
            causas=[M.CAMBIO_PLAN],
            modalidad=ModalidadRenta.ADELANTADA,
            signo="+",
            guion=[
                "reconocer_la_pregunta",
                "monto_total_y_diferencia",
                "causa_principal_cambio_de_plan",
                "tabla_de_tramos",
                "aclarar_que_es_por_unica_vez",
                "siguiente_paso",
            ],
            senales=[
                "cambié a un plan más barato pero me cobraron más",
                "me están cobrando dos veces el plan",
                "por qué aparecen dos planes",
            ],
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN", "AJUSTE_RETROACTIVO_RENTA"],
            advertencia=(
                "No dé a entender que el cobro mayor se repetirá: el ajuste es del ciclo "
                "en curso y no vuelve a aparecer."
            ),
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-CAMBIO-PLAN-ADELANTADA-BAJA",
            titulo="Cambio de plan con renta adelantada y recibo menor",
            descripcion=(
                "El cliente cambió de plan y el recibo bajó. Conviene mostrar la renta del "
                "ciclo siguiente ya con el plan nuevo y el ajuste por los días del plan "
                "anterior, para que la bajada se entienda como estable y no como un error."
            ),
            causas=[M.CAMBIO_PLAN],
            modalidad=ModalidadRenta.ADELANTADA,
            signo="-",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_cambio_de_plan",
                "tabla_de_tramos",
                "que_esperar_el_proximo_mes",
                "siguiente_paso",
            ],
            senales=["me vino más barato", "está bien lo que me cobraron"],
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN", "AJUSTE_RETROACTIVO_RENTA"],
            prioridad=20,
        ),
        Casuistica(
            casuistica_id="CAS-CAMBIO-PLAN-VENCIDA-SUBE",
            titulo="Cambio de plan a mitad de ciclo con renta vencida",
            descripcion=(
                "Con renta vencida el recibo cobra el ciclo que acaba de cerrar, así que la "
                "renta aparece partida en dos: los días con el plan anterior y los días con "
                "el plan nuevo. La tabla de tramos es la explicación completa."
            ),
            causas=[M.CAMBIO_PLAN],
            modalidad=ModalidadRenta.VENCIDA,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_cambio_de_plan",
                "tabla_de_tramos",
                "que_esperar_el_proximo_mes",
                "siguiente_paso",
            ],
            senales=["por qué me cobran el plan partido", "cambié de plan a mitad de mes"],
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN"],
            prioridad=15,
        ),
        Casuistica(
            casuistica_id="CAS-CAMBIO-PLAN-VENCIDA-BAJA",
            titulo="Cambio de plan con renta vencida y recibo menor",
            descripcion=(
                "El plan nuevo es más barato y el ciclo cerrado ya lo refleja en parte. El "
                "próximo recibo mostrará la renta nueva completa."
            ),
            causas=[M.CAMBIO_PLAN],
            modalidad=ModalidadRenta.VENCIDA,
            signo="-",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_cambio_de_plan",
                "tabla_de_tramos",
                "que_esperar_el_proximo_mes",
            ],
            senales=["me vino menos", "ya se aplicó mi plan nuevo"],
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN"],
            prioridad=25,
        ),
        Casuistica(
            casuistica_id="CAS-EQUIPO-FINANCIADO",
            titulo="Cuota de equipo financiado",
            descripcion=(
                "El recibo incluye la cuota del equipo que el cliente compró en partes. La "
                "cuota no se prorratea: se cobra completa cada mes hasta terminar el "
                "financiamiento. Decir en qué cuota va y cuántas faltan reduce la ansiedad "
                "más que cualquier otra frase."
            ),
            causas=[M.ALTA_EQUIPO_FINANCIADO],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_equipo_financiado",
                "cuota_actual_y_restantes",
                "cuando_termina",
                "siguiente_paso",
            ],
            senales=[
                "qué es este cargo del celular",
                "hasta cuándo pago el equipo",
                "cuántas cuotas me faltan",
            ],
            conceptos=["CUOTA_EQUIPO_FINANCIADO", "INTERES_FINANCIAMIENTO"],
            advertencia="No presente la cuota como un aumento del plan: es un cargo aparte y con fin.",
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-CORTE-RECONEXION",
            titulo="Corte por deuda y posterior reconexión",
            descripcion=(
                "El servicio estuvo suspendido por falta de pago y luego se reconectó. El "
                "recibo trae dos efectos opuestos: un ajuste a favor por los días sin "
                "servicio y un cargo por la reconexión. Conviene mostrarlos juntos para que "
                "el cliente vea que lo descontado y lo cobrado son cosas distintas."
            ),
            causas=[M.RECONEXION, M.SUSPENSION],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "ajuste_por_dias_sin_servicio",
                "cargo_por_reconexion",
                "como_evitarlo_la_proxima_vez",
                "siguiente_paso",
            ],
            senales=[
                "me cortaron el servicio y me cobran igual",
                "qué es el cargo de reconexión",
                "no tuve servicio varios días",
            ],
            conceptos=["CARGO_RECONEXION", "AJUSTE_DIAS_SUSPENSION"],
            advertencia="Reconozca primero los días sin servicio; el cliente los vivió y espera verlos.",
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-AJUSTE-SUSPENSION",
            titulo="Ajuste a favor por días de suspensión",
            descripcion=(
                "El recibo incluye un descuento por los días en que el servicio estuvo "
                "suspendido. Es un ajuste a favor del cliente y baja el total."
            ),
            causas=[M.AJUSTE_SUSPENSION],
            modalidad=None,
            signo="-",
            guion=["monto_total_y_diferencia", "ajuste_por_dias_sin_servicio", "siguiente_paso"],
            senales=["me devolvieron los días sin servicio"],
            conceptos=["AJUSTE_DIAS_SUSPENSION"],
            prioridad=20,
        ),
        Casuistica(
            casuistica_id="CAS-FIN-DESCUENTO",
            titulo="Se terminó una promoción",
            descripcion=(
                "El cliente tenía un descuento por tiempo limitado y venció. El plan no "
                "cambió y no hay ningún cargo nuevo: lo que desapareció es la rebaja. Es la "
                "situación que más se percibe como cobro indebido, así que hay que decir "
                "con claridad qué promoción era y desde cuándo dejó de aplicar."
            ),
            causas=[M.FIN_DESCUENTO],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_promocion_vencida",
                "que_promocion_era_y_hasta_cuando",
                "alternativas_disponibles",
                "siguiente_paso",
            ],
            senales=[
                "me subieron el plan sin avisar",
                "antes pagaba menos",
                "ya no me aplican el descuento",
            ],
            conceptos=["DESCUENTO_PROMOCIONAL", "DESCUENTO_MOVISTAR_TOTAL", "RENTA_PLAN_MOVIL"],
            advertencia="No diga que subió el precio del plan: el precio no cambió, terminó el descuento.",
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-ALTA-PAQUETE",
            titulo="Compra de paquetes adicionales",
            descripcion=(
                "El cliente contrató paquetes durante el ciclo (datos, roaming o canales) y "
                "aparecen como cargos adicionales. Se explican indicando cuándo se "
                "contrataron y si son de una sola vez o recurrentes."
            ),
            causas=[M.ALTA_PAQUETE],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_paquetes",
                "detalle_de_paquetes",
                "si_es_recurrente_o_unico",
                "siguiente_paso",
            ],
            senales=["yo no compré nada", "qué son estos paquetes", "compré datos extra"],
            conceptos=["PAQUETE_DATOS_ADICIONAL", "PAQUETE_ROAMING", "PAQUETE_TV_PREMIUM"],
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-NOTA-CREDITO",
            titulo="Nota de crédito aplicada",
            descripcion=(
                "Se aplicó una nota de crédito que descuenta del recibo. Se explica el "
                "motivo del ajuste y se confirma que ya está aplicado, sin pedir ninguna "
                "gestión adicional al cliente."
            ),
            causas=[M.NOTA_CREDITO],
            modalidad=None,
            signo="-",
            guion=["monto_total_y_diferencia", "causa_principal_nota_credito", "siguiente_paso"],
            senales=["me hicieron un descuento", "qué es la nota de crédito"],
            conceptos=["NOTA_CREDITO"],
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-NOTA-DEBITO",
            titulo="Nota de débito aplicada",
            descripcion=(
                "Se aplicó una nota de débito por un cobro que no se facturó en su momento. "
                "Hay que decir a qué corresponde y de qué periodo viene."
            ),
            causas=[M.NOTA_DEBITO],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "causa_principal_nota_debito",
                "de_que_periodo_viene",
                "siguiente_paso",
            ],
            senales=["qué es la nota de débito", "me cobran algo de meses pasados"],
            conceptos=["NOTA_DEBITO"],
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-COMPUESTO-PLAN-EQUIPO",
            titulo="Cambio de plan y cuota de equipo en el mismo recibo",
            descripcion=(
                "Coinciden dos causas: el cambio de plan y la cuota del equipo financiado. "
                "Hay que separar cuánto pesa cada una en la diferencia, porque el cliente "
                "tiende a atribuirlo todo al cambio de plan."
            ),
            causas=[M.CAMBIO_PLAN, M.ALTA_EQUIPO_FINANCIADO],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "separar_las_dos_causas",
                "causa_principal_cambio_de_plan",
                "causa_principal_equipo_financiado",
                "tabla_de_tramos",
                "siguiente_paso",
            ],
            senales=["me subió todo junto", "no entiendo qué me cobran"],
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN", "CUOTA_EQUIPO_FINANCIADO"],
            advertencia="No atribuya toda la diferencia al cambio de plan si la cuota pesa igual o más.",
            prioridad=5,
        ),
        Casuistica(
            casuistica_id="CAS-ESTABLE",
            titulo="El recibo no varió",
            descripcion=(
                "El total es el mismo que el mes anterior. La respuesta correcta es decirlo "
                "sin rodeos y ofrecer el detalle, no buscar una causa que no existe."
            ),
            causas=[],
            modalidad=None,
            signo="0",
            guion=["confirmar_que_no_hubo_variacion", "ofrecer_detalle", "siguiente_paso"],
            senales=["me cobraron lo mismo", "está igual que el mes pasado"],
            conceptos=[],
            advertencia="No fabrique una explicación de variación cuando la diferencia es cero.",
            prioridad=10,
        ),
        Casuistica(
            casuistica_id="CAS-SIN-CAUSA-ATRIBUIDA",
            titulo="Variación sin causa atribuible",
            descripcion=(
                "El recibo varió pero no hay ningún movimiento en el historial que lo "
                "explique. No se improvisa una causa: se reconoce la variación, se muestra "
                "el detalle de lo que cambió y se ofrece un asesor con el contexto cargado."
            ),
            causas=[],
            modalidad=None,
            signo="+",
            guion=[
                "monto_total_y_diferencia",
                "lo_que_cambio_sin_afirmar_causa",
                "ofrecer_asesor_con_contexto",
            ],
            senales=["nadie me sabe explicar", "quiero hablar con una persona"],
            conceptos=[],
            advertencia=(
                "Prohibido afirmar una causa probable. Sin movimiento que la respalde, se deriva."
            ),
            prioridad=1,
        ),
    ]


#: Casuísticas incluidas en el paquete: cubren los ocho escenarios obligatorios de la
#: sección 8 en ambas modalidades de renta, más el escenario compuesto y el de
#: variación no atribuible (que termina en derivación).
CASUISTICAS_SEMILLA: list[Casuistica] = _semilla()


class CorpusCompleto(BaseModel):
    """Los tres corpus ya cargados. Es lo que consume :class:`Recuperador`."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    conceptos: list[ConceptoCatalogo] = Field(default_factory=list)
    faqs: list[Faq] = Field(default_factory=list)
    casuisticas: list[Casuistica] = Field(default_factory=list)
    origen: str = ""

    @cached_property
    def indice_catalogo(self) -> IndiceCatalogo:
        """Índice por clave del catálogo."""
        return IndiceCatalogo(self.conceptos)

    @cached_property
    def indice_casuisticas(self) -> IndiceCasuisticas:
        """Índice por firma causal de las casuísticas."""
        return IndiceCasuisticas(self.casuisticas)

    def documentos(self, corpus: CorpusRag | None = None) -> list[DocumentoCorpus]:
        """Documentos indexables de uno o de los tres corpus."""
        por_corpus: dict[CorpusRag, list[DocumentoCorpus]] = {
            CorpusRag.CATALOGO: documentos_de_catalogo(self.conceptos),
            CorpusRag.FAQ: documentos_de_faqs(self.faqs),
            CorpusRag.CASUISTICA: documentos_de_casuisticas(self.casuisticas),
        }
        if corpus is not None:
            return por_corpus[corpus]
        return [documento for lista in por_corpus.values() for documento in lista]

    def resumen(self) -> dict[str, int]:
        """Conteo por corpus, para el log de arranque y la CLI de indexado."""
        return {
            str(CorpusRag.CATALOGO): len(self.conceptos),
            str(CorpusRag.FAQ): len(self.faqs),
            str(CorpusRag.CASUISTICA): len(self.casuisticas),
        }


def cargar_corpus(
    ruta: str | Path | None = None, reglas: ConfiguracionReglas | None = None
) -> CorpusCompleto:
    """Carga los tres corpus de una vez desde el directorio de corpus."""
    directorio = Path(ruta) if ruta is not None else ruta_corpus_por_defecto()
    conceptos = cargar_catalogo(directorio, reglas=reglas)
    return CorpusCompleto(
        conceptos=conceptos,
        faqs=cargar_faqs(directorio, conceptos=conceptos),
        casuisticas=cargar_casuisticas(directorio),
        origen=str(directorio),
    )
