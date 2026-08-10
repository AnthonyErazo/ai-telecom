"""Embeddings e índice vectorial (pgvector) con degradación limpia a memoria.

Qué se vectoriza y qué no
-------------------------
Aquí solo entran catálogo, FAQs y casuísticas. **El recibo no.** Un importe no se
recupera por similitud coseno: se lee de una tabla. Esa frontera es el corazón de la
arquitectura y por eso este módulo no sabe siquiera qué es un recibo.

Embedders
---------
``Embedder`` es la interfaz; hay dos implementaciones:

* :class:`MockEmbedder` — determinístico, sin red. No es ruido con forma de vector:
  aplica el *hashing trick* (proyección aleatoria fija del saco de palabras) sobre el
  mismo tokenizador español que usa BM25, así que el coseno resultante mide solapamiento
  léxico real. Sirve para tests y para la demo reproducible.
* :class:`GeminiEmbedder` — SDK oficial ``google-genai``. El identificador del modelo se
  lee de ``GEMINI_EMBED_MODEL``; **no se hardcodea** porque los ids de Google cambian y
  deben verificarse en su documentación vigente.

**Cambiar de modelo obliga a reindexar.** Los vectores de dos modelos distintos no son
comparables entre sí. Por eso cada fila del índice guarda :meth:`Embedder.firma_modelo`
y toda búsqueda filtra por ella: una mezcla de modelos daría resultados silenciosamente
malos, y aquí falla ruidosamente o no ocurre.

Persistencia
------------
``IndiceVectorial`` persiste en PostgreSQL con pgvector y busca por distancia coseno
(``<=>``) con filtro por metadatos. Si la base no está disponible —no hay Docker, no hay
extensión, no hay credenciales— **degrada a un índice en memoria** con la misma
semántica, deja un aviso en el log y sigue funcionando. Un retriever que se cae porque
falta una base de datos convierte un problema de infraestructura en una caída de
producto.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Final

from packages.core_domain.enums import CorpusRag
from packages.retriever.bm25 import tokenizar
from packages.retriever.corpus import DocumentoCorpus, Procedencia, ResultadoBusqueda

__all__ = [
    "DIMENSION_POR_DEFECTO",
    "TABLA_POR_DEFECTO",
    "Embedder",
    "ErrorEmbedder",
    "GeminiEmbedder",
    "IndiceVectorial",
    "MockEmbedder",
    "coseno",
    "crear_embedder",
    "dimension_configurada",
    "dsn_configurado",
    "normalizar_dsn",
    "normalizar_vector",
]

_LOG = logging.getLogger(__name__)

#: Dimensión por defecto de los vectores (``EMBED_DIM`` en el entorno).
DIMENSION_POR_DEFECTO: Final = 768

#: Tabla de pgvector. La DDL se crea de forma idempotente desde este módulo, de modo
#: que el retriever funciona con o sin la migración ``db/migraciones/002_rag.sql``.
TABLA_POR_DEFECTO: Final = "rag_documento"

#: Timeout de la llamada de embeddings, en segundos.
_TIMEOUT_POR_DEFECTO: Final = 10.0

#: Timeout de conexión a PostgreSQL, en segundos. Corto a propósito: si la base no
#: está, se degrada a memoria en vez de bloquear el arranque del servicio.
_TIMEOUT_CONEXION_S: Final = 3


class ErrorEmbedder(RuntimeError):
    """Fallo al obtener embeddings de un proveedor remoto."""


def dimension_configurada() -> int:
    """Dimensión de los vectores según ``EMBED_DIM`` (por defecto 768)."""
    crudo = (os.getenv("EMBED_DIM") or "").strip()
    if not crudo:
        return DIMENSION_POR_DEFECTO
    try:
        valor = int(crudo)
    except ValueError:
        _LOG.warning("EMBED_DIM=%r no es un entero; se usa %d", crudo, DIMENSION_POR_DEFECTO)
        return DIMENSION_POR_DEFECTO
    if valor <= 0:
        _LOG.warning("EMBED_DIM=%d no es válido; se usa %d", valor, DIMENSION_POR_DEFECTO)
        return DIMENSION_POR_DEFECTO
    return valor


def normalizar_dsn(dsn: str | None) -> str | None:
    """Reescribe el DSN al driver psycopg 3.

    El ``.env`` trae ``postgresql://…`` y SQLAlchemy lo interpretaría como psycopg2, que
    no es la dependencia declarada. Se normaliza aquí, en un único sitio, para que dé
    igual si el DSN viene del entorno, de la CLI o de un test.
    """
    limpio = (dsn or "").strip()
    if not limpio:
        return None
    for prefijo in ("postgresql://", "postgres://"):
        if limpio.startswith(prefijo):
            return "postgresql+psycopg://" + limpio[len(prefijo) :]
    return limpio


def dsn_configurado() -> str | None:
    """DSN de PostgreSQL desde ``DATABASE_URL``, ya normalizado."""
    return normalizar_dsn(os.getenv("DATABASE_URL"))


# --------------------------------------------------------------------------- #
# Álgebra mínima
# --------------------------------------------------------------------------- #
def normalizar_vector(vector: Sequence[float]) -> list[float]:
    """Devuelve el vector con norma 1 (o el vector nulo si la norma es 0).

    Con vectores normalizados el coseno es el producto escalar, y la distancia coseno
    de pgvector queda en ``[0, 2]``: la búsqueda en memoria y la de la base ordenan
    igual, que es justo lo que se necesita para que degradar no cambie resultados.
    """
    norma = math.sqrt(sum(componente * componente for componente in vector))
    if norma == 0.0:
        return [0.0] * len(vector)
    return [componente / norma for componente in vector]


def coseno(primero: Sequence[float], segundo: Sequence[float]) -> float:
    """Similitud coseno entre dos vectores, en ``[-1, 1]``."""
    if len(primero) != len(segundo):
        raise ValueError(f"dimensiones distintas: {len(primero)} vs {len(segundo)}")
    producto = sum(a * b for a, b in zip(primero, segundo, strict=True))
    norma_a = math.sqrt(sum(a * a for a in primero))
    norma_b = math.sqrt(sum(b * b for b in segundo))
    if norma_a == 0.0 or norma_b == 0.0:
        return 0.0
    return producto / (norma_a * norma_b)


# --------------------------------------------------------------------------- #
# Interfaz de embeddings
# --------------------------------------------------------------------------- #
class Embedder(ABC):
    """Interfaz de generación de embeddings.

    Contrato: :meth:`incrustar` devuelve un vector por texto, todos de la misma
    ``dimension`` y **normalizados**. La normalización es parte del contrato para que el
    índice pueda tratar el coseno como producto escalar sin volver a medir normas.
    """

    #: Nombre corto del proveedor, para logs y auditoría.
    nombre: str = "abstracto"

    def __init__(self, dimension: int | None = None) -> None:
        self.dimension = int(dimension or dimension_configurada())
        if self.dimension <= 0:
            raise ValueError(f"dimensión inválida: {self.dimension}")

    @abstractmethod
    def incrustar(self, textos: Sequence[str]) -> list[list[float]]:
        """Vectoriza un lote de textos, preservando el orden."""

    def incrustar_uno(self, texto: str) -> list[float]:
        """Vectoriza un único texto."""
        return self.incrustar([texto])[0]

    def firma_modelo(self) -> str:
        """Identidad del espacio vectorial: ``nombre:modelo:dimension``.

        Es la clave que impide mezclar vectores de modelos distintos en el índice y
        el aviso operativo de que cambiar de modelo obliga a reindexar.
        """
        return f"{self.nombre}:{self.dimension}"

    def __repr__(self) -> str:  # pragma: no cover - conveniencia de depuración
        return f"<{type(self).__name__} {self.firma_modelo()}>"


class MockEmbedder(Embedder):
    """Embedder determinístico sin red, para tests y para la demo reproducible.

    Cada término del texto se proyecta a un puñado de posiciones del vector mediante
    ``sha256`` (con signo también derivado del hash) y se acumula con peso
    sublineal ``1 + ln(tf)``; al final se normaliza. Es el *hashing trick* de toda la
    vida: el vector es función pura del texto —misma entrada, mismos bytes, siempre— y
    además conserva similitud léxica útil, de modo que los tests del híbrido prueban
    algo real y no un empate artificial.

    Se indexan también los prefijos de cuatro letras de cada término, lo que da algo de
    tolerancia morfológica ("reconexión" / "reconectar") sin necesidad de stemmer.
    """

    nombre = "mock"

    #: Posiciones del vector que activa cada término.
    _REPETICIONES: Final = 3

    def __init__(self, dimension: int | None = None, *, semilla: str = "recibo-claro") -> None:
        super().__init__(dimension)
        self._semilla = semilla

    def _posiciones(self, termino: str) -> list[tuple[int, float]]:
        """Índices y signos que le corresponden a un término."""
        digest = hashlib.sha256(f"{self._semilla}|{termino}".encode()).digest()
        posiciones: list[tuple[int, float]] = []
        for repeticion in range(self._REPETICIONES):
            bloque = digest[repeticion * 8 : repeticion * 8 + 8]
            (entero,) = struct.unpack(">Q", bloque)
            indice = entero % self.dimension
            signo = 1.0 if (entero >> 63) & 1 else -1.0
            posiciones.append((indice, signo))
        return posiciones

    def incrustar(self, textos: Sequence[str]) -> list[list[float]]:
        """Vectoriza por proyección hash del saco de palabras. Nunca falla ni sale a red."""
        vectores: list[list[float]] = []
        for texto in textos:
            vector = [0.0] * self.dimension
            terminos = tokenizar(texto)
            frecuencias: dict[str, int] = {}
            for termino in terminos:
                frecuencias[termino] = frecuencias.get(termino, 0) + 1
                prefijo = termino[:4]
                if prefijo != termino:
                    clave = f"#{prefijo}"
                    frecuencias[clave] = frecuencias.get(clave, 0) + 1
            for termino, frecuencia in frecuencias.items():
                peso = 1.0 + math.log(frecuencia)
                # Los prefijos pesan menos: son señal de apoyo, no el término real.
                if termino.startswith("#"):
                    peso *= 0.35
                for indice, signo in self._posiciones(termino):
                    vector[indice] += signo * peso
            vectores.append(normalizar_vector(vector))
        return vectores


class GeminiEmbedder(Embedder):
    """Embedder sobre la API de Google Gemini (SDK ``google-genai``).

    Lee del entorno:

    * ``GEMINI_API_KEY`` — obligatoria.
    * ``GEMINI_EMBED_MODEL`` — obligatoria y **sin valor por defecto en el código**: el
      identificador vigente debe verificarse en la documentación de Google. Fijarlo aquí
      sería garantizar que el proyecto se rompe cuando Google lo renombre.
    * ``EMBED_DIM`` — dimensión pedida al modelo.

    Cualquier fallo (falta de clave, timeout, respuesta inesperada) se convierte en
    :class:`ErrorEmbedder`, que el híbrido captura para degradar a BM25 puro.
    """

    nombre = "gemini"

    def __init__(
        self,
        dimension: int | None = None,
        *,
        modelo: str | None = None,
        api_key: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(dimension)
        self.modelo = (modelo or os.getenv("GEMINI_EMBED_MODEL") or "").strip()
        if not self.modelo:
            raise ErrorEmbedder(
                "GEMINI_EMBED_MODEL no está definido: fije el id del modelo de embeddings "
                "vigente según la documentación de Google (no se hardcodea en el código)"
            )
        self._api_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        if not self._api_key:
            raise ErrorEmbedder("GEMINI_API_KEY no está definida")
        self._timeout_ms = int((timeout_s or _TIMEOUT_POR_DEFECTO) * 1000)
        self._cliente: Any | None = None

    def firma_modelo(self) -> str:
        """``gemini:<modelo>:<dimension>`` — cambia si cambia el modelo, y obliga a reindexar."""
        return f"{self.nombre}:{self.modelo}:{self.dimension}"

    def _obtener_cliente(self) -> Any:
        """Instancia perezosa del cliente: importar el SDK no debe ser un efecto de importar el módulo."""
        if self._cliente is None:
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError as error:  # pragma: no cover - depende del entorno
                raise ErrorEmbedder(
                    "el SDK google-genai no está instalado; use LLM_MODE=mock o instale la dependencia"
                ) from error
            self._cliente = genai.Client(
                api_key=self._api_key, http_options={"timeout": self._timeout_ms}
            )
        return self._cliente

    @staticmethod
    def _extraer_vectores(respuesta: Any) -> list[list[float]]:
        """Normaliza la respuesta del SDK a una lista de listas de float.

        El SDK ha cambiado de forma entre versiones (objetos con ``.values``, dicts con
        ``"values"``, listas directas). Se aceptan las tres en vez de acoplarse a una.
        """
        crudos = getattr(respuesta, "embeddings", None)
        if crudos is None and isinstance(respuesta, dict):
            crudos = respuesta.get("embeddings")
        if crudos is None:
            raise ErrorEmbedder(f"respuesta de embeddings sin campo 'embeddings': {type(respuesta)}")

        vectores: list[list[float]] = []
        for elemento in crudos:
            valores = getattr(elemento, "values", None)
            if valores is None and isinstance(elemento, dict):
                valores = elemento.get("values")
            if valores is None and isinstance(elemento, (list, tuple)):
                valores = elemento
            if valores is None:
                raise ErrorEmbedder("elemento de embedding sin vector de valores")
            vectores.append([float(componente) for componente in valores])
        return vectores

    def incrustar(self, textos: Sequence[str]) -> list[list[float]]:
        """Vectoriza el lote con la API de Gemini y normaliza los vectores."""
        if not textos:
            return []
        cliente = self._obtener_cliente()
        try:
            respuesta = cliente.models.embed_content(
                model=self.modelo,
                contents=list(textos),
                config={
                    "output_dimensionality": self.dimension,
                    "task_type": "RETRIEVAL_DOCUMENT",
                },
            )
        except Exception as error:  # la causa exacta la aporta el SDK
            raise ErrorEmbedder(f"fallo al pedir embeddings a Gemini: {error}") from error

        vectores = self._extraer_vectores(respuesta)
        if len(vectores) != len(textos):
            raise ErrorEmbedder(
                f"Gemini devolvió {len(vectores)} vectores para {len(textos)} textos"
            )
        for vector in vectores:
            if len(vector) != self.dimension:
                raise ErrorEmbedder(
                    f"dimensión inesperada: se pidió {self.dimension} y llegó {len(vector)}; "
                    "si cambió el modelo debe reindexar el corpus"
                )
        return [normalizar_vector(vector) for vector in vectores]


def crear_embedder(modo: str | None = None, dimension: int | None = None) -> Embedder:
    """Fábrica de embedders por configuración.

    Args:
        modo: ``"mock"`` o ``"gemini"``. Por defecto ``EMBED_MODE`` y, si no está,
            ``LLM_MODE`` (mismo interruptor que la capa generativa).
        dimension: sobrescribe ``EMBED_DIM``.

    Returns:
        El embedder pedido. Si se pide ``gemini`` y no hay credenciales, se registra el
        motivo y se devuelve :class:`MockEmbedder`: la demo nunca se queda sin retriever.
    """
    elegido = (modo or os.getenv("EMBED_MODE") or os.getenv("LLM_MODE") or "mock").strip().lower()
    if elegido == "gemini":
        try:
            return GeminiEmbedder(dimension)
        except ErrorEmbedder as error:
            _LOG.warning("no se pudo crear GeminiEmbedder (%s); se usa MockEmbedder", error)
            return MockEmbedder(dimension)
    if elegido not in {"mock", ""}:
        _LOG.warning("EMBED_MODE=%r desconocido; se usa MockEmbedder", elegido)
    return MockEmbedder(dimension)


# --------------------------------------------------------------------------- #
# Índice vectorial
# --------------------------------------------------------------------------- #
def _hash_texto(texto: str) -> str:
    """Huella del contenido indexado: permite saltarse lo que no cambió."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _literal_array(valores: Sequence[str]) -> str:
    """Serializa una lista de cadenas como literal de ``text[]`` de PostgreSQL."""
    escapados = [valor.replace("\\", "\\\\").replace('"', '\\"') for valor in valores]
    return "{" + ",".join(f'"{valor}"' for valor in escapados) + "}"


def _literal_vector(vector: Sequence[float]) -> str:
    """Serializa un vector como literal de pgvector: ``[0.1,0.2,...]``."""
    return "[" + ",".join(f"{componente:.8f}" for componente in vector) + "]"


class _Entrada:
    """Fila del índice en memoria."""

    __slots__ = ("documento", "hash_texto", "vector")

    def __init__(self, documento: DocumentoCorpus, vector: list[float], hash_texto: str) -> None:
        self.documento = documento
        self.vector = vector
        self.hash_texto = hash_texto


class IndiceVectorial:
    """Índice de similitud coseno sobre pgvector, con respaldo en memoria.

    Uso::

        indice = IndiceVectorial(MockEmbedder())
        indice.indexar(corpus.documentos())
        indice.buscar("por qué me vino más caro", k=5, conceptos=["RENTA_PLAN_MOVIL"])

    :meth:`indexar` es **idempotente**: compara el hash del texto de cada documento con
    el ya almacenado y solo vectoriza lo nuevo o lo modificado. Ejecutar la CLI de
    indexado dos veces seguidas no gasta una sola llamada de embeddings ni cambia una
    sola fila.
    """

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        dsn: str | None = None,
        tabla: str = TABLA_POR_DEFECTO,
        forzar_memoria: bool = False,
        motivo_memoria: str | None = None,
    ) -> None:
        self.embedder = embedder or crear_embedder()
        self.tabla = tabla if tabla.isidentifier() else TABLA_POR_DEFECTO
        self._memoria: dict[str, _Entrada] = {}
        self._motor: Any | None = None
        self._motivo_degradacion: str | None = None

        if forzar_memoria:
            # No es una degradación: es lo que se pidió. Por eso INFO y no WARNING —
            # un aviso alarmante ante una elección deliberada enseña a ignorar el log.
            # ``motivo_memoria`` deja que quien decidió explique **por qué** en la misma
            # frase que verá quien lea `/salud/preparacion`.
            self._motivo_degradacion = motivo_memoria or "modo memoria solicitado explícitamente"
            _LOG.info(
                "índice vectorial EN MEMORIA (%s): no se abre ninguna conexión",
                self._motivo_degradacion,
            )
            return
        self._conectar(dsn if dsn is not None else dsn_configurado())

    # ------------------------------------------------------------------ #
    # Conexión y esquema
    # ------------------------------------------------------------------ #
    def _conectar(self, dsn: str | None) -> None:
        """Intenta abrir la base y preparar el esquema. Nunca propaga la excepción."""
        normalizado = normalizar_dsn(dsn)
        if not normalizado:
            self._degradar("DATABASE_URL no está definida")
            return
        try:
            from sqlalchemy import create_engine

            # Timeout corto: si la base no responde, se degrada en segundos. Un
            # arranque colgado esperando a PostgreSQL es peor que uno sin vectores.
            conexion_args = (
                {"connect_timeout": _TIMEOUT_CONEXION_S} if "postgresql" in normalizado else {}
            )
            motor = create_engine(
                normalizado, pool_pre_ping=True, future=True, connect_args=conexion_args
            )
            self._asegurar_esquema(motor)
        except Exception as error:  # cualquier fallo degrada, no rompe
            self._degradar(f"{type(error).__name__}: {error}")
            return
        self._motor = motor
        self._motivo_degradacion = None
        _LOG.info("índice vectorial sobre pgvector, tabla %s", self.tabla)

    def _degradar(self, motivo: str) -> None:
        """Registra el aviso y conmuta a índice en memoria."""
        self._motor = None
        self._motivo_degradacion = motivo
        _LOG.warning(
            "pgvector no disponible (%s); el índice vectorial funciona EN MEMORIA "
            "(sin persistencia entre procesos)",
            motivo,
        )

    def _asegurar_esquema(self, motor: Any) -> None:
        """Crea extensión, tabla e índices de forma idempotente."""
        from sqlalchemy import text

        dimension = int(self.embedder.dimension)
        sentencias = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            f"""
            CREATE TABLE IF NOT EXISTS {self.tabla} (
                doc_id          TEXT        NOT NULL,
                modelo          TEXT        NOT NULL,
                corpus          TEXT        NOT NULL,
                titulo          TEXT        NOT NULL DEFAULT '',
                texto           TEXT        NOT NULL,
                conceptos       TEXT[]      NOT NULL DEFAULT '{{}}',
                causas          TEXT[]      NOT NULL DEFAULT '{{}}',
                firma_causal    TEXT,
                metadatos       JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
                hash_texto      TEXT        NOT NULL,
                dimension       INTEGER     NOT NULL,
                embedding       VECTOR({dimension}) NOT NULL,
                actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (doc_id, modelo)
            )
            """,
            f"CREATE INDEX IF NOT EXISTS ix_{self.tabla}_corpus ON {self.tabla} (corpus)",
            f"CREATE INDEX IF NOT EXISTS ix_{self.tabla}_firma ON {self.tabla} (firma_causal)",
            f"CREATE INDEX IF NOT EXISTS ix_{self.tabla}_conceptos ON {self.tabla} USING GIN (conceptos)",
        ]
        with motor.begin() as conexion:
            for sentencia in sentencias:
                conexion.execute(text(sentencia))
        self._verificar_dimension(motor, dimension)

    def _verificar_dimension(self, motor: Any, dimension: int) -> None:
        """Comprueba que la tabla existente tiene la dimensión que produce el embedder.

        ``CREATE TABLE IF NOT EXISTS`` no altera una tabla que ya existe: si alguien
        cambia ``EMBED_DIM`` sin migrar, los INSERT fallarían con un error de pgvector
        difícil de leer. Se detecta aquí y se dice qué hacer.
        """
        from sqlalchemy import text

        try:
            with motor.connect() as conexion:
                actual = conexion.execute(
                    text(
                        "SELECT atttypmod FROM pg_attribute "
                        "WHERE attrelid = CAST(:tabla AS regclass) AND attname = 'embedding'"
                    ),
                    {"tabla": self.tabla},
                ).scalar()
        except Exception as error:  # la comprobación es informativa, no puede romper
            _LOG.debug("no se pudo verificar la dimensión de %s: %s", self.tabla, error)
            return

        if actual is not None and actual > 0 and int(actual) != dimension:
            raise RuntimeError(
                f"la tabla {self.tabla} guarda vectores de {int(actual)} dimensiones y el "
                f"embedder produce {dimension}. Cambiar de modelo o de EMBED_DIM obliga a "
                f"reindexar: elimine la tabla (o migre la columna) y vuelva a ejecutar "
                f"`python -m packages.retriever.indexar`."
            )

    @property
    def disponible_bd(self) -> bool:
        """Verdadero si el índice está respaldado por PostgreSQL."""
        return self._motor is not None

    @property
    def motivo_degradacion(self) -> str | None:
        """Por qué se está usando el índice en memoria (``None`` si no se degradó)."""
        return self._motivo_degradacion

    @property
    def modelo(self) -> str:
        """Firma del espacio vectorial en uso."""
        return self.embedder.firma_modelo()

    # ------------------------------------------------------------------ #
    # Indexado
    # ------------------------------------------------------------------ #
    def _hashes_existentes(self, doc_ids: Sequence[str]) -> dict[str, str]:
        """``doc_id -> hash_texto`` de lo ya indexado con este modelo."""
        if self._motor is None:
            return {
                doc_id: entrada.hash_texto
                for doc_id, entrada in self._memoria.items()
                if doc_id in set(doc_ids)
            }
        from sqlalchemy import text

        consulta = text(
            f"SELECT doc_id, hash_texto FROM {self.tabla} "  # tabla validada como identificador en el constructor
            "WHERE modelo = :modelo AND doc_id = ANY(CAST(:ids AS text[]))"
        )
        with self._motor.connect() as conexion:
            filas = conexion.execute(
                consulta, {"modelo": self.modelo, "ids": _literal_array(list(doc_ids))}
            ).fetchall()
        return {fila[0]: fila[1] for fila in filas}

    def indexar(
        self, documentos: Sequence[DocumentoCorpus], *, forzar: bool = False, lote: int = 32
    ) -> dict[str, int]:
        """Indexa (o reindexa) documentos. **Idempotente.**

        Args:
            documentos: documentos a indexar.
            forzar: revectoriza aunque el hash del texto no haya cambiado.
            lote: tamaño de lote para las llamadas al embedder.

        Returns:
            ``{"total", "vectorizados", "sin_cambios"}``.
        """
        if not documentos:
            return {"total": 0, "vectorizados": 0, "sin_cambios": 0}

        hashes = {} if forzar else self._hashes_existentes([d.doc_id for d in documentos])
        pendientes = [
            documento
            for documento in documentos
            if hashes.get(documento.doc_id) != _hash_texto(documento.texto_indexable())
        ]
        if not pendientes:
            _LOG.info("índice vectorial al día: %d documentos sin cambios", len(documentos))
            return {"total": len(documentos), "vectorizados": 0, "sin_cambios": len(documentos)}

        for inicio in range(0, len(pendientes), max(1, lote)):
            grupo = pendientes[inicio : inicio + max(1, lote)]
            vectores = self.embedder.incrustar([documento.texto_indexable() for documento in grupo])
            self._guardar(grupo, vectores)

        _LOG.info(
            "índice vectorial: %d vectorizados, %d sin cambios (modelo %s)",
            len(pendientes),
            len(documentos) - len(pendientes),
            self.modelo,
        )
        return {
            "total": len(documentos),
            "vectorizados": len(pendientes),
            "sin_cambios": len(documentos) - len(pendientes),
        }

    def _guardar(self, documentos: Sequence[DocumentoCorpus], vectores: Sequence[list[float]]) -> None:
        """Escribe un lote en la base o en memoria (upsert por ``doc_id`` + ``modelo``)."""
        if self._motor is None:
            for documento, vector in zip(documentos, vectores, strict=True):
                self._memoria[documento.doc_id] = _Entrada(
                    documento, vector, _hash_texto(documento.texto_indexable())
                )
            return

        import json

        from sqlalchemy import text

        sentencia = text(
            f"""
            INSERT INTO {self.tabla} (
                doc_id, modelo, corpus, titulo, texto, conceptos, causas, firma_causal,
                metadatos, hash_texto, dimension, embedding, actualizado_en
            ) VALUES (
                :doc_id, :modelo, :corpus, :titulo, :texto,
                CAST(:conceptos AS text[]), CAST(:causas AS text[]), :firma_causal,
                CAST(:metadatos AS jsonb), :hash_texto, :dimension,
                CAST(:embedding AS vector), now()
            )
            ON CONFLICT (doc_id, modelo) DO UPDATE SET
                corpus = EXCLUDED.corpus,
                titulo = EXCLUDED.titulo,
                texto = EXCLUDED.texto,
                conceptos = EXCLUDED.conceptos,
                causas = EXCLUDED.causas,
                firma_causal = EXCLUDED.firma_causal,
                metadatos = EXCLUDED.metadatos,
                hash_texto = EXCLUDED.hash_texto,
                dimension = EXCLUDED.dimension,
                embedding = EXCLUDED.embedding,
                actualizado_en = now()
            """
        )
        parametros = [
            {
                "doc_id": documento.doc_id,
                "modelo": self.modelo,
                "corpus": str(documento.corpus),
                "titulo": documento.titulo,
                "texto": documento.texto,
                "conceptos": _literal_array(documento.conceptos),
                "causas": _literal_array([str(causa) for causa in documento.causas]),
                "firma_causal": documento.firma_causal,
                "metadatos": json.dumps(documento.metadatos, ensure_ascii=False, sort_keys=True),
                "hash_texto": _hash_texto(documento.texto_indexable()),
                "dimension": self.embedder.dimension,
                "embedding": _literal_vector(vector),
            }
            for documento, vector in zip(documentos, vectores, strict=True)
        ]
        with self._motor.begin() as conexion:
            conexion.execute(sentencia, parametros)

    # ------------------------------------------------------------------ #
    # Búsqueda
    # ------------------------------------------------------------------ #
    def buscar(
        self,
        consulta: str,
        k: int = 5,
        *,
        corpus: CorpusRag | None = None,
        conceptos: Sequence[str] | None = None,
        firma_causal: str | None = None,
        incluir_transversales: bool = True,
        umbral: float = 0.0,
    ) -> list[ResultadoBusqueda]:
        """Busca por similitud coseno con filtro por metadatos.

        Args:
            consulta: texto a vectorizar.
            k: número máximo de resultados.
            corpus: restringe a un corpus (``faq``, ``casuistica``, ``concepto_catalogo``).
            conceptos: solo documentos que hablen de esos ``concepto_id``.
            firma_causal: solo documentos con esa firma exacta (casuísticas).
            incluir_transversales: conserva los documentos sin conceptos asociados.
            umbral: similitud mínima para devolver un resultado.

        Returns:
            Resultados ordenados por similitud descendente. Si el embedder falla, se
            propaga :class:`ErrorEmbedder` para que el híbrido decida degradar.
        """
        vector = self.embedder.incrustar_uno(consulta)
        if self._motor is None:
            return self._buscar_memoria(
                vector, k, corpus, conceptos, firma_causal, incluir_transversales, umbral
            )
        try:
            return self._buscar_bd(
                vector, k, corpus, conceptos, firma_causal, incluir_transversales, umbral
            )
        except Exception as error:  # la BD puede caerse en caliente
            self._degradar(f"fallo en consulta: {type(error).__name__}: {error}")
            return self._buscar_memoria(
                vector, k, corpus, conceptos, firma_causal, incluir_transversales, umbral
            )

    def _buscar_bd(
        self,
        vector: list[float],
        k: int,
        corpus: CorpusRag | None,
        conceptos: Sequence[str] | None,
        firma: str | None,
        incluir_transversales: bool,
        umbral: float,
    ) -> list[ResultadoBusqueda]:
        """Consulta pgvector con ``<=>`` (distancia coseno)."""
        import json

        from sqlalchemy import text

        assert self._motor is not None
        sentencia = text(
            f"""
            SELECT doc_id, corpus, titulo, texto, conceptos, causas, firma_causal, metadatos,
                   1 - (embedding <=> CAST(:vector AS vector)) AS similitud
            FROM {self.tabla}
            WHERE modelo = :modelo
              AND (CAST(:corpus AS text) IS NULL OR corpus = CAST(:corpus AS text))
              AND (CAST(:firma AS text) IS NULL OR firma_causal = CAST(:firma AS text))
              AND (
                    CAST(:sin_filtro AS boolean)
                 OR conceptos && CAST(:conceptos AS text[])
                 OR (CAST(:transversales AS boolean) AND cardinality(conceptos) = 0)
              )
            ORDER BY embedding <=> CAST(:vector AS vector), doc_id
            LIMIT CAST(:k AS integer)
            """
        )
        parametros = {
            "vector": _literal_vector(vector),
            "modelo": self.modelo,
            "corpus": str(corpus) if corpus is not None else None,
            "firma": firma,
            "sin_filtro": not conceptos,
            "conceptos": _literal_array(list(conceptos or [])),
            "transversales": incluir_transversales,
            "k": int(k),
        }
        with self._motor.connect() as conexion:
            filas = conexion.execute(sentencia, parametros).fetchall()

        resultados: list[ResultadoBusqueda] = []
        for posicion, fila in enumerate(filas, start=1):
            similitud = float(fila.similitud)
            if similitud < umbral:
                continue
            metadatos = fila.metadatos
            if isinstance(metadatos, str):
                metadatos = json.loads(metadatos)
            documento = DocumentoCorpus(
                doc_id=fila.doc_id,
                corpus=CorpusRag(fila.corpus),
                titulo=fila.titulo,
                texto=fila.texto,
                conceptos=list(fila.conceptos or []),
                causas=list(fila.causas or []),
                firma_causal=fila.firma_causal,
                metadatos=metadatos or {},
            )
            resultados.append(
                ResultadoBusqueda(
                    documento=documento,
                    puntaje=similitud,
                    procedencia=Procedencia.VECTORIAL,
                    posicion=posicion,
                    detalle={"coseno": similitud},
                )
            )
        return resultados

    def _buscar_memoria(
        self,
        vector: list[float],
        k: int,
        corpus: CorpusRag | None,
        conceptos: Sequence[str] | None,
        firma: str | None,
        incluir_transversales: bool,
        umbral: float,
    ) -> list[ResultadoBusqueda]:
        """Misma semántica de filtro y orden que :meth:`_buscar_bd`, sin base de datos."""
        filtro = set(conceptos) if conceptos else None
        candidatos: list[tuple[float, DocumentoCorpus]] = []
        for entrada in self._memoria.values():
            documento = entrada.documento
            if corpus is not None and documento.corpus is not corpus:
                continue
            if firma is not None and documento.firma_causal != firma:
                continue
            if filtro is not None:
                if documento.conceptos:
                    if not filtro.intersection(documento.conceptos):
                        continue
                elif not incluir_transversales:
                    continue
            similitud = coseno(vector, entrada.vector)
            if similitud < umbral:
                continue
            candidatos.append((similitud, documento))

        candidatos.sort(key=lambda par: (-par[0], par[1].doc_id))
        return [
            ResultadoBusqueda(
                documento=documento,
                puntaje=similitud,
                procedencia=Procedencia.VECTORIAL,
                posicion=posicion,
                detalle={"coseno": similitud},
            )
            for posicion, (similitud, documento) in enumerate(candidatos[:k], start=1)
        ]

    # ------------------------------------------------------------------ #
    # Mantenimiento
    # ------------------------------------------------------------------ #
    def contar(self, corpus: CorpusRag | None = None) -> int:
        """Documentos indexados con el modelo actual."""
        if self._motor is None:
            return sum(
                1
                for entrada in self._memoria.values()
                if corpus is None or entrada.documento.corpus is corpus
            )
        from sqlalchemy import text

        consulta = text(
            f"SELECT count(*) FROM {self.tabla} "  # tabla validada como identificador en el constructor
            "WHERE modelo = :modelo "
            "AND (CAST(:corpus AS text) IS NULL OR corpus = CAST(:corpus AS text))"
        )
        with self._motor.connect() as conexion:
            return int(
                conexion.execute(
                    consulta,
                    {"modelo": self.modelo, "corpus": str(corpus) if corpus else None},
                ).scalar_one()
            )

    def limpiar(self, corpus: CorpusRag | None = None) -> int:
        """Borra lo indexado con el modelo actual. Devuelve las filas eliminadas."""
        if self._motor is None:
            objetivo = [
                doc_id
                for doc_id, entrada in self._memoria.items()
                if corpus is None or entrada.documento.corpus is corpus
            ]
            for doc_id in objetivo:
                del self._memoria[doc_id]
            return len(objetivo)
        from sqlalchemy import text

        sentencia = text(
            f"DELETE FROM {self.tabla} "  # tabla validada como identificador en el constructor
            "WHERE modelo = :modelo "
            "AND (CAST(:corpus AS text) IS NULL OR corpus = CAST(:corpus AS text))"
        )
        with self._motor.begin() as conexion:
            resultado = conexion.execute(
                sentencia, {"modelo": self.modelo, "corpus": str(corpus) if corpus else None}
            )
        return int(resultado.rowcount or 0)

    def estado(self) -> dict[str, Any]:
        """Resumen para logs, la CLI de indexado y el evento ``RETRIEVE``."""
        return {
            "respaldo": "pgvector" if self.disponible_bd else "memoria",
            "modelo": self.modelo,
            "dimension": self.embedder.dimension,
            "documentos": self.contar(),
            "motivo_degradacion": self._motivo_degradacion,
        }
