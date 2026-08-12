"""Calcula los embeddings del corpus RAG y los escribe EN LA MISMA FILA de Supabase.

Por qué existe este script (y por qué no se usa ``packages/retriever/indexar.py``)
---------------------------------------------------------------------------------
La CLI ``indexar.py`` persiste en una tabla propia, ``rag_documento``, que se crea a sí
misma. El esquema del proyecto (``db/esquema.sql``) guarda el vector **pegado a la fila**
del dato: ``faq.embedding``, ``casuistica.embedding``, ``vocabulario_peruano.embedding``,
y los índices HNSW ``ix_faq_emb_hnsw`` e ``ix_casuistica_emb_hnsw`` cuelgan de esas
columnas. Eran dos diseños de persistencia que no se hablaban: ``to_regclass`` confirmó
que ``rag_documento`` no existe en Supabase y que ambas vistas de salud contaban cero
vectorizados. Este script elige el lado del esquema, y lo hace a propósito:

* **Una sola verdad.** Movistar tendrá su facturación en un relacional. Un almacén
  vectorial aparte obliga a sincronizar dos copias del mismo documento y a explicar qué
  pasa cuando divergen. Con pgvector el vector es una columna más: se borra la FAQ y se
  borra su vector, en la misma transacción, sin trabajo de sincronización.
* **Se filtra en SQL antes de medir distancias.** ``WHERE activo`` y el filtro por
  ``modelo_embedding`` viajan en la misma consulta que el ``<=>``. Un almacén separado
  obliga a recuperar de más y filtrar después, que es justo lo que estropea el recall.

Por qué es reanudable y no un bucle a pelo
------------------------------------------
El nivel gratuito de Gemini impone **tres** límites a los embeddings, y los tres se
tocaron mientras se escribía esto:

* 100 peticiones y 30.000 tokens por **minuto** — se liberan solos.
* **1.000 textos por DÍA y por modelo** (``EmbedContentRequestsPerDayPerProjectPerModel``)
  — no se libera hasta el día siguiente. El corpus son 662 documentos, así que cabe una
  vez al día y ni una prueba de más. Este es el límite que de verdad manda aquí, y el que
  agotó ``gemini-embedding-001`` antes de terminar.

Los tres devuelven el mismo 429 y el mismo ``retryDelay`` de ~50 segundos, que en el
caso diario es simplemente mentira. El diseño lo asume:

1. **El progreso se guarda tras cada lote** (``COMMIT`` por lote). Si el proceso muere,
   lo ya vectorizado sigue en la base.
2. **Lo pendiente se deduce de la propia base**, no de un fichero de estado.
   Lo pendiente son las filas cuyo ``modelo_embedding`` no coincide con la firma del
   embedder actual. Eso da idempotencia gratis (volver a lanzarlo no gasta ni una
   llamada) y, de paso, resuelve el cambio de modelo: cambiar ``GEMINI_EMBED_MODEL``
   cambia la firma y el script reindexa solo lo que quedó obsoleto.

Lo que este script NO hace
--------------------------
No vectoriza el catálogo de conceptos (``v_concepto_real``). No es un olvido: esa vista
no tiene columna de embedding y el catálogo se recupera por clave exacta
(``lookup_clave``), no por similitud. Meterlo aquí sería inventar una tabla para un
corpus que no se busca por vector.

Uso::

    python scripts/vectorizar_corpus.py                  # vectoriza lo pendiente
    python scripts/vectorizar_corpus.py --tabla faq      # solo una tabla
    python scripts/vectorizar_corpus.py --verificar      # solo cuenta, no gasta cuota
    python scripts/vectorizar_corpus.py --consulta "por que me llego mas caro el recibo"

Códigos de salida: ``0`` todo vectorizado, ``2`` quedó trabajo pendiente (relanzar),
``1`` error de configuración.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Final, Sequence

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

_LOG = logging.getLogger("vectorizar")

# --------------------------------------------------------------------------- #
# Parámetros de cuota
# --------------------------------------------------------------------------- #
#: Tokens por minuto que nos permitimos. El límite del nivel gratuito son 30.000; se
#: deja margen porque la estimación de tokens es aproximada. En la práctica **este no
#: fue nunca el límite que saltó**: el 429 que paró las dos primeras ejecuciones era la
#: cuota diaria de peticiones (ver :func:`_es_cuota_diaria`). Se mantiene el control por
#: minuto igualmente, porque con la cuota diaria fresca sí es el siguiente muro.
TPM_POR_DEFECTO: Final = 20_000

#: Peticiones por minuto que nos permitimos (límite real 100).
RPM_POR_DEFECTO: Final = 80

#: Textos por petición. Pequeño a propósito: si un lote falla, se pierde poco trabajo y
#: el reintento cuesta poca cuota. Con lotes grandes, un 429 tira 100 documentos.
LOTE_POR_DEFECTO: Final = 16

#: Caracteres por token asumidos al estimar el coste de un texto en español. Es una
#: cota **pesimista** (el ratio real ronda 4): preferimos ir sobrados y no comernos el
#: 429, porque el 429 cuesta 60 segundos y la sobreestimación cuesta unos pocos.
CARACTERES_POR_TOKEN: Final = 3.2

#: Reintentos ante un 429 antes de rendirse y salir dejando el trabajo a medias.
#: Son muchos a propósito: el castigo observado dura minutos, y rendirse pronto obliga a
#: relanzar a mano cada pocos minutos. Con el retroceso de abajo, agotarlos supone haber
#: esperado más de 20 minutos, que ya es evidencia de que la cuota no se va a liberar.
REINTENTOS_MAX: Final = 8

#: Espera mínima tras un 429, en segundos. El ``retryDelay`` que anuncia el servidor se
#: quedó corto en la práctica (decía 50 s y el bloqueo duró 12 minutos), así que se usa
#: como suelo y no como verdad.
ESPERA_MINIMA_429: Final = 75.0

#: Tope de la espera entre reintentos.
ESPERA_MAXIMA_429: Final = 360.0

_SALIDA_OK: Final = 0
_SALIDA_ERROR: Final = 1
_SALIDA_PENDIENTE: Final = 2


class CuotaAgotada(RuntimeError):
    """La cuota del API no se liberó tras agotar los reintentos.

    Es un error tipado y no un ``return`` porque tiene que **cortar la ejecución
    entera**, no solo la tabla en curso: la cuota pertenece a la clave del API y es
    común a los tres destinos. Lo ya escrito está comprometido en la base, así que
    abortar no pierde trabajo, solo deja de gastar tiempo.
    """


# --------------------------------------------------------------------------- #
# Descripción de las tablas vectorizables
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DestinoVectorial:
    """Una tabla de Supabase que guarda vectores, y de dónde sale su texto.

    Se describe como dato y no como tres funciones copiadas porque las tres tablas
    hacen exactamente lo mismo con nombres distintos: leer una clave, calcular un
    vector, escribirlo en la misma fila. Lo único que cambia es de dónde sale el texto.
    """

    tabla: str
    clave: str
    #: Devuelve ``{clave: texto_a_vectorizar}``. Se resuelve tarde (es una llamada a
    #: Supabase o al corpus) para que ``--verificar`` no pague ese coste.
    textos: Callable[[], dict[str, str]]
    #: ``True`` si la tabla tiene columna ``activo``: no se gasta cuota en filas que el
    #: recuperador no va a leer nunca.
    filtra_activo: bool = True
    descripcion: str = ""


@lru_cache(maxsize=1)
def _corpus_completo() -> Any:
    """Carga el corpus una sola vez por proceso.

    Sin la caché, cada destino volvía a bajarse el catálogo, las 400 FAQs y las 22
    casuísticas de Supabase: tres viajes idénticos y unos quince segundos regalados en
    cada relanzamiento, que con esta cuota son muchos relanzamientos.
    """
    from packages.retriever.corpus import cargar_corpus

    return cargar_corpus()


def _textos_del_corpus(corpus_pedido: str) -> dict[str, str]:
    """Texto indexable de ``faq`` o ``casuistica``, **construido por el propio corpus**.

    Es la decisión importante del script: no se arma aquí un ``pregunta || respuesta``
    a mano. Se llama a ``cargar_corpus()`` y se usa ``DocumentoCorpus.texto_indexable()``,
    que es literalmente el texto que el recuperador entrega a BM25. Si el vector se
    calculase sobre un texto distinto del que indexa BM25, las dos ramas del híbrido
    estarían buscando documentos diferentes y el RRF fusionaría cosas que no se
    corresponden. Un segundo mapeo aquí se desincronizaría en cuanto alguien añadiese un
    campo al corpus; este no puede, porque no existe.

    El ``doc_id`` del corpus es ``"<prefijo>:<clave>"`` (p. ej. ``faq:EXT-PAY-002``), así
    que la clave primaria de la tabla se recupera cortando por el primer ``:``.
    """
    from packages.core_domain.enums import CorpusRag

    cual = CorpusRag(corpus_pedido)
    corpus = _corpus_completo()
    resultado: dict[str, str] = {}
    for documento in corpus.documentos(cual):
        _, _, clave = documento.doc_id.partition(":")
        if clave:
            resultado[clave] = documento.texto_indexable()
    return resultado


def _textos_de_vocabulario() -> dict[str, str]:
    """Texto indexable del vocabulario peruano, armado aquí y no en ``corpus.py``.

    ``vocabulario_peruano`` no es un corpus del recuperador: lo consume
    ``packages/facts_engine/jerga.py`` para normalizar la jerga del cliente antes de
    clasificar la intención. Por eso no tiene una clase ``DocumentoCorpus`` de la que
    tomar prestado el texto y hay que componerlo aquí.

    Se concatenan el término, sus variantes, el significado y la nota. Las variantes son
    la parte que más importa: son las formas reales en que un cliente peruano escribe lo
    mismo ("ya cancele", "cancelé"), y son exactamente lo que una búsqueda por
    similitud debe capturar y una por palabra clave no captura.
    """
    filas = _consultar(
        """
        SELECT termino, coalesce(significa, ''), coalesce(nota, ''),
               coalesce(variantes, '{}'::text[])
        FROM vocabulario_peruano
        ORDER BY termino
        """
    )
    textos: dict[str, str] = {}
    for termino, significa, nota, variantes in filas:
        partes = [termino, *(variantes or []), significa, nota]
        textos[termino] = "\n".join(parte for parte in partes if parte).strip()
    return textos


DESTINOS: Final[tuple[DestinoVectorial, ...]] = (
    DestinoVectorial(
        tabla="faq",
        clave="faq_id",
        textos=lambda: _textos_del_corpus("faq"),
        descripcion="preguntas frecuentes (pregunta + variantes + respuesta)",
    ),
    DestinoVectorial(
        tabla="casuistica",
        clave="casuistica_id",
        textos=lambda: _textos_del_corpus("casuistica"),
        descripcion="guiones narrativos por firma causal",
    ),
    DestinoVectorial(
        tabla="vocabulario_peruano",
        clave="termino",
        textos=_textos_de_vocabulario,
        filtra_activo=False,
        descripcion="jerga peruana (término + variantes + significado)",
    ),
)


# --------------------------------------------------------------------------- #
# Conexión
# --------------------------------------------------------------------------- #
def cargar_entorno() -> None:
    """Carga ``.env`` porque las credenciales viven en el fichero, no en el entorno.

    Se hace explícito y al principio: la mitad de los fallos de este proyecto han sido
    procesos que arrancaban sin credenciales y **degradaban en silencio** a memoria o a
    BM25 puro. Aquí no se degrada: si falta el DSN, se sale con un error.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependencia de desarrollo
        _LOG.warning("python-dotenv no está instalado; se usan las variables del entorno")
        return
    load_dotenv(_RAIZ / ".env")


def dsn_supabase() -> str:
    """DSN de Supabase, leyendo ``SUPABASE_DB_URL`` y, si no, ``DATABASE_URL``.

    El proyecto arrastra **dos nombres para la misma base**: el ``.env`` define
    ``SUPABASE_DB_URL`` y ``packages/retriever/vectorial.py`` lee ``DATABASE_URL``. Esa
    discrepancia es la causa de que el índice vectorial nunca se conectase y se
    construyese en memoria. Aquí se aceptan las dos, con preferencia por la que existe
    de verdad en el ``.env``, para que este script funcione hoy sin esperar a la
    unificación de nombres.
    """
    for variable in ("SUPABASE_DB_URL", "DATABASE_URL"):
        valor = (os.getenv(variable) or "").strip()
        if valor:
            return valor
    raise SystemExit(
        "no hay DSN: defina SUPABASE_DB_URL (o DATABASE_URL) en .env antes de vectorizar"
    )


def _conectar() -> Any:
    """Abre una conexión psycopg 3 a Supabase con timeout explícito y **sin preparar**.

    ``prepare_threshold=None`` desactiva las sentencias preparadas automáticas de
    psycopg 3, y no es una manía: el DSN de Supabase apunta al *pooler* en modo
    transacción, que multiplexa varias conexiones de cliente sobre las mismas conexiones
    de servidor. psycopg nombra sus sentencias preparadas (``_pg3_0``, ``_pg3_1``…) y da
    por hecho que ese nombre le pertenece; al reutilizarse la conexión de servidor, el
    nombre ya existe y salta ``DuplicatePreparedStatement``. Pasó en la primera ejecución
    real, justo al escribir el segundo lote de FAQs.

    El coste es un plan de consulta por ejecución. Es irrelevante aquí: escribimos unos
    cientos de filas y el cuello de botella es la cuota de embeddings, no el planificador.
    """
    import psycopg

    return psycopg.connect(dsn_supabase(), connect_timeout=20, prepare_threshold=None)


def _consultar(sql: str, parametros: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
    """Ejecuta una consulta de lectura y devuelve todas las filas."""
    with _conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute(sql, parametros)
        return cursor.fetchall()


# --------------------------------------------------------------------------- #
# Control de cuota
# --------------------------------------------------------------------------- #
def estimar_tokens(textos: Sequence[str]) -> int:
    """Estimación pesimista de los tokens de un lote.

    No se pide el conteo real al API porque contar tokens es **otra llamada** que también
    consume cuota de peticiones: gastaríamos parte del presupuesto en medir el
    presupuesto. Con una cota superior basta, porque el error solo nos hace ir más lentos.
    """
    caracteres = sum(len(texto) for texto in textos)
    return int(caracteres / CARACTERES_POR_TOKEN) + len(textos)


@dataclass
class ControlDeCuota:
    """Ventana deslizante de un minuto sobre peticiones y tokens.

    Espera **antes** de gastar, no después de que el servidor diga que no. Reaccionar al
    429 tiene dos costes: la petición perdida y el hecho de que el servidor puede
    penalizar más de lo que penalizaría la espera voluntaria. Con la ventana deslizante,
    el caso normal es que el script nunca vea un 429; el reintento con retroceso
    exponencial existe solo para lo que no controlamos (que otro proceso comparta la
    misma clave, por ejemplo).
    """

    tokens_por_minuto: int = TPM_POR_DEFECTO
    peticiones_por_minuto: int = RPM_POR_DEFECTO
    #: Cada evento es ``(instante, tokens, peticiones)``. Se guardan las dos magnitudes
    #: en la misma cola porque comparten ventana: purgarlas por separado obligaría a
    #: mantener dos estructuras sincronizadas para medir el mismo minuto.
    _eventos: deque[tuple[float, int, int]] = field(default_factory=deque, repr=False)

    def _purgar(self, ahora: float) -> None:
        while self._eventos and ahora - self._eventos[0][0] >= 60.0:
            self._eventos.popleft()

    def reservar(self, tokens: int, peticiones: int = 1) -> float:
        """Duerme lo necesario para que quepan ``tokens`` y ``peticiones`` mas.

        ``peticiones`` no siempre es 1: hay modelos que no agrupan y un «lote» de 16
        textos son 16 llamadas de verdad. Contarlo como una sola dispararia el limite
        por minuto sin que este control se enterase.

        Returns:
            Segundos dormidos (0 si no hizo falta), para poder informarlo.
        """
        dormido = 0.0
        while True:
            ahora = time.monotonic()
            self._purgar(ahora)
            usados_tokens = sum(evento[1] for evento in self._eventos)
            usadas_peticiones = sum(evento[2] for evento in self._eventos)
            cabe_tokens = usados_tokens + tokens <= self.tokens_por_minuto
            cabe_peticion = usadas_peticiones + peticiones <= self.peticiones_por_minuto
            # `not self._eventos` deja pasar un lote que por si solo excede el
            # presupuesto: si no, esperariamos para siempre a que se libere una cuota
            # que nunca alcanzara. Ese lote si vera el 429, y para eso esta el reintento.
            if (cabe_tokens and cabe_peticion) or not self._eventos:
                self._eventos.append((ahora, tokens, peticiones))
                return dormido
            espera = 60.0 - (ahora - self._eventos[0][0]) + 0.5
            _LOG.info(
                "cuota: %d/%d tokens y %d/%d peticiones en la ventana; se esperan %.1f s",
                usados_tokens,
                self.tokens_por_minuto,
                usadas_peticiones,
                self.peticiones_por_minuto,
                espera,
            )
            time.sleep(espera)
            dormido += espera

    def penalizar(self) -> None:
        """Baja el presupuesto tras un 429 por minuto y da la ventana por consumida.

        Decrecimiento multiplicativo, como el control de congestion de TCP y por la
        misma razon: no sabemos cual es el limite real —la cifra publicada no coincidio
        con lo observado— asi que en vez de discutir con el servidor se reduce el ritmo
        hasta que deje de quejarse. Tambien se marca la ventana como llena, para que la
        siguiente reserva espere de verdad en lugar de reintentar al instante sobre un
        presupuesto que acabamos de comprobar que era optimista.
        """
        anterior = self.tokens_por_minuto
        self.tokens_por_minuto = max(3_000, int(anterior * 0.6))
        self.peticiones_por_minuto = max(10, int(self.peticiones_por_minuto * 0.6))
        self._eventos.clear()
        self._eventos.append(
            (time.monotonic(), self.tokens_por_minuto, self.peticiones_por_minuto)
        )
        _LOG.warning(
            "cuota: se baja el presupuesto a %d tokens/minuto (era %d) y %d peticiones/minuto",
            self.tokens_por_minuto,
            anterior,
            self.peticiones_por_minuto,
        )


def _segundos_de_reintento(mensaje: str) -> float | None:
    """Extrae el ``retryDelay`` que devuelve Gemini en el error 429, si viene.

    Se prefiere el valor del servidor al nuestro: él sabe cuándo se libera la cuota y
    nosotros solo la estimamos.
    """
    encontrado = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", mensaje)
    return float(encontrado.group(1)) if encontrado else None


def _es_cuota(mensaje: str) -> bool:
    """¿El error es agotamiento de cuota y no un fallo real?"""
    texto = mensaje.upper()
    return "429" in texto or "RESOURCE_EXHAUSTED" in texto or "QUOTA" in texto


def _es_cuota_diaria(mensaje: str) -> bool:
    """¿El 429 es de la cuota **diaria**, en cuyo caso esperar no sirve de nada?

    Esta distinción costó veinte minutos de esperas inútiles y merece explicación. Gemini
    devuelve el mismo código 429 para dos cosas muy distintas, y encima acompaña ambas de
    un ``retryDelay`` de ~50 segundos:

    * ``…RequestsPerMinute…`` — se libera sola en un minuto. Esperar es correcto.
    * ``EmbedContentRequestsPerDayPerProjectPerModel-FreeTier``, límite 1000 — se libera
      a medianoche. Esperar 50 segundos, o 20 minutos, no cambia nada.

    El ``retryDelay`` que anuncia el servidor es, en el segundo caso, sencillamente
    falso. Por eso no se le hace caso y se mira el ``quotaId``: si la cuota es diaria se
    aborta al primer intento y se dice qué hacer (mañana, u otro modelo), en vez de
    fingir que se está progresando.

    Detalle que importa para planificar: la cuota diaria cuenta **textos, no peticiones**.
    Un ``batchEmbedContents`` con 16 contenidos gasta 16 unidades de las 1000. Agrupar en
    lotes ahorra latencia, no cuota.
    """
    texto = mensaje.upper()
    return "PERDAY" in texto or "PER DAY" in texto or "REQUESTSPERDAY" in texto


# --------------------------------------------------------------------------- #
# Vectorización
# --------------------------------------------------------------------------- #
def _pendientes(
    conexion: Any, destino: DestinoVectorial, firma: str, dimension: int, forzar: bool
) -> list[str]:
    """Claves que faltan por vectorizar, **preguntándoselo a la base**.

    El estado de avance no se guarda en ningún fichero: es una consulta. Una fila está
    hecha si tiene vector Y su ``modelo_embedding`` coincide con la firma del embedder
    actual Y la dimensión es la esperada. De ahí salen tres propiedades a la vez:
    reanudar tras un corte, no gastar cuota al relanzar, y reindexar solo lo que quedó
    obsoleto cuando se cambia de modelo (los vectores de dos modelos no son comparables:
    mezclarlos daría resultados silenciosamente malos).
    """
    condiciones = ["true" if not destino.filtra_activo else "activo"]
    if not forzar:
        condiciones.append(
            "(embedding IS NULL OR modelo_embedding IS DISTINCT FROM %s "
            "OR dim_embedding IS DISTINCT FROM %s)"
        )
    sql = (
        f"SELECT {destino.clave} FROM {destino.tabla} "
        f"WHERE {' AND '.join(condiciones)} ORDER BY {destino.clave}"
    )
    parametros: tuple[Any, ...] = () if forzar else (firma, dimension)
    with conexion.cursor() as cursor:
        cursor.execute(sql, parametros)
        return [fila[0] for fila in cursor.fetchall()]


def _literal_vector(vector: Sequence[float]) -> str:
    """Serializa el vector al literal de pgvector: ``[0.1,0.2,…]``."""
    return "[" + ",".join(f"{componente:.8f}" for componente in vector) + "]"


def _escribir(
    conexion: Any,
    destino: DestinoVectorial,
    firma: str,
    dimension: int,
    lote: list[tuple[str, list[float]]],
) -> None:
    """Escribe un lote de vectores y hace ``COMMIT`` inmediatamente.

    El ``COMMIT`` por lote (y no uno al final) es lo que hace reanudable el proceso: lo
    que ya costó cuota queda guardado aunque el siguiente lote reviente. La alternativa
    —una transacción única— convertiría cualquier corte en «vuelve a pagar todo».
    """
    sql = (
        f"UPDATE {destino.tabla} SET embedding = %s::vector, modelo_embedding = %s, "
        f"dim_embedding = %s, actualizado_en = now() WHERE {destino.clave} = %s"
    )
    with conexion.cursor() as cursor:
        cursor.executemany(
            sql,
            [(_literal_vector(vector), firma, dimension, clave) for clave, vector in lote],
        )
    conexion.commit()


def vectorizar_destino(
    conexion: Any,
    destino: DestinoVectorial,
    embedder: Any,
    cuota: ControlDeCuota,
    *,
    tam_lote: int,
    forzar: bool,
    limite: int | None,
) -> dict[str, int]:
    """Vectoriza lo pendiente de una tabla. Devuelve el recuento de lo ocurrido."""
    from packages.retriever.vectorial import ErrorEmbedder

    firma = embedder.firma_modelo()
    dimension = embedder.dimension
    claves = _pendientes(conexion, destino, firma, dimension, forzar)
    if limite is not None:
        claves = claves[:limite]
    resumen = {"pendientes": len(claves), "escritos": 0, "sin_texto": 0, "fallidos": 0}
    if not claves:
        _LOG.info("%s: nada pendiente (firma %s)", destino.tabla, firma)
        return resumen

    # Se dice el gasto ANTES de gastarlo. La cuota diaria son 1000 textos por modelo y
    # se consume aunque el proceso muera después: saber cuánto va a costar es lo que
    # permite decidir si conviene lanzarlo entero o por partes con --limite.
    _LOG.info(
        "%s: %d filas pendientes — %s (gastará %d de las ~1000 unidades diarias del modelo)",
        destino.tabla,
        len(claves),
        destino.descripcion,
        len(claves),
    )
    textos = destino.textos()

    utiles = [(clave, textos[clave]) for clave in claves if textos.get(clave, "").strip()]
    resumen["sin_texto"] = len(claves) - len(utiles)
    if resumen["sin_texto"]:
        # No se inventa texto para vectorizar. Una fila sin contenido recuperable se
        # queda sin vector y se dice cuántas son, que es lo que permite arreglarlas.
        _LOG.warning(
            "%s: %d filas sin texto en el corpus; se dejan sin vector",
            destino.tabla,
            resumen["sin_texto"],
        )

    for inicio in range(0, len(utiles), tam_lote):
        trozo = utiles[inicio : inicio + tam_lote]
        contenidos = [texto for _, texto in trozo]
        tokens = estimar_tokens(contenidos)

        # Si el modelo no agrupa (``lote_maximo == 1``), este «lote» son N llamadas
        # reales y así hay que reservarlas: contarlas como una sola es la manera de
        # pasarse del límite por minuto creyendo que se va sobrado.
        peticiones = len(trozo) if getattr(embedder, "lote_maximo", None) == 1 else 1

        vectores: list[list[float]] | None = None
        for intento in range(1, REINTENTOS_MAX + 1):
            cuota.reservar(tokens, peticiones)
            try:
                vectores = embedder.incrustar(contenidos)
                break
            except ErrorEmbedder as error:
                mensaje = str(error)
                if not _es_cuota(mensaje):
                    _LOG.error("%s: fallo no recuperable: %s", destino.tabla, mensaje)
                    resumen["fallidos"] += len(trozo)
                    break
                if intento == 1:
                    # El mensaje entero, una vez y sin recortar. Los límites de Google
                    # cambian sin avisar y el propio error dice cuál se ha tocado; sin
                    # esta línea, ajustar el ritmo sería adivinar.
                    _LOG.warning("%s: 429 literal del servidor: %s", destino.tabla, mensaje)
                if _es_cuota_diaria(mensaje):
                    raise CuotaAgotada(
                        "cuota DIARIA de embeddings agotada para el modelo "
                        f"{embedder.firma_modelo()}: esperar no la libera. Opciones: "
                        "relanzar mañana, o cambiar GEMINI_EMBED_MODEL a otro modelo "
                        "(la cuota es por modelo y cada uno tiene su propio cupo diario)."
                    ) from error
                cuota.penalizar()
                sugerida = _segundos_de_reintento(mensaje) or 0.0
                espera = min(
                    max(sugerida, ESPERA_MINIMA_429 * intento), ESPERA_MAXIMA_429
                )
                _LOG.warning(
                    "%s: cuota agotada (intento %d/%d); se esperan %.0f s",
                    destino.tabla,
                    intento,
                    REINTENTOS_MAX,
                    espera,
                )
                time.sleep(espera + 1.0)
        if vectores is None:
            resumen["fallidos"] += len(trozo)
            # La cuota es de la clave del API, no de la tabla: si aquí no entra nada,
            # tampoco va a entrar en la siguiente tabla. Se aborta la ejecución entera en
            # vez de quemar otra tanda de reintentos por cada destino que quede, que es
            # lo que hacía la primera versión y multiplicaba por tres la espera inútil.
            raise CuotaAgotada(
                f"{destino.tabla}: la cuota no se liberó tras {REINTENTOS_MAX} intentos"
            )

        _escribir(
            conexion,
            destino,
            firma,
            dimension,
            [(clave, vector) for (clave, _), vector in zip(trozo, vectores, strict=True)],
        )
        resumen["escritos"] += len(trozo)
        _LOG.info(
            "%s: %d/%d escritos (~%d tokens en el lote)",
            destino.tabla,
            resumen["escritos"],
            len(utiles),
            tokens,
        )
    return resumen


# --------------------------------------------------------------------------- #
# Verificación
# --------------------------------------------------------------------------- #
def verificar() -> list[tuple[str, int, int, int, str | None]]:
    """Cuenta filas y vectores por tabla, preguntando a Supabase.

    Se cuenta con ``count(embedding)`` y no con una variable del script porque lo que
    importa no es lo que el script cree haber escrito, sino lo que la base tiene.
    """
    filas: list[tuple[str, int, int, int, str | None]] = []
    for destino in DESTINOS:
        resultado = _consultar(
            f"""
            SELECT count(*), count(embedding), count(DISTINCT modelo_embedding),
                   max(modelo_embedding)
            FROM {destino.tabla}
            """
        )[0]
        filas.append((destino.tabla, *resultado))  # type: ignore[arg-type]
    return filas


def _incrustar_consulta(embedder: Any, consulta: str) -> list[float]:
    """Vectoriza la **pregunta**, no un documento.

    ``gemini-embedding-001`` es asimétrico: pide ``RETRIEVAL_QUERY`` para la consulta y
    ``RETRIEVAL_DOCUMENT`` para lo indexado. Usar el mismo tipo en ambos lados degrada la
    recuperación de forma silenciosa —sigue devolviendo resultados, solo que peores—, y
    ese es el tipo de fallo que más caro sale. Con el embedder simulado el parámetro no
    aplica y se llama al método normal.
    """
    incrustar = getattr(embedder, "incrustar", None)
    if embedder.nombre == "gemini":
        return embedder.incrustar([consulta], tipo_tarea="RETRIEVAL_QUERY")[0]
    return incrustar([consulta])[0]  # type: ignore[misc]


def _bm25_de_produccion(consulta: str, k: int) -> list[tuple[float, str, str]]:
    """Puntúa la consulta con el MISMO motor BM25 que usa el recuperador en producción.

    Por qué se añadió, y por qué la versión anterior de esta comparación era tramposa:
    antes el lado léxico se simulaba con ``plainto_tsquery('spanish', …)``, que une los
    lexemas con **AND**. Con la consulta real del cliente, Postgres la convierte en
    ``'lleg' & 'mas' & 'car' & 'recib'``; ninguna FAQ contiene el lexema ``car`` (nadie
    escribe "caro" en un catálogo de atención), así que la conjunción devolvía **cero
    documentos** aunque 181 FAQs contengan ``recib``. El vector ganaba la comparación por
    descalificación del rival, no por mérito, y esa es exactamente la clase de medición
    que este proyecto no puede permitirse: la tesis se defiende con cifras honestas o no
    se defiende.

    ``IndiceBM25`` (Okapi, ``packages/retriever/bm25.py``) puntúa por solapamiento
    parcial, que es lo que de verdad corre en producción y lo que hay que batir. Con él,
    la misma consulta sí devuelve resultados —y muy buenos—, de modo que la comparación
    mide la diferencia entre dos recuperadores y no entre un recuperador y un error de
    sintaxis SQL.
    """
    from packages.retriever.bm25 import IndiceBM25

    indice = IndiceBM25(_corpus_completo().documentos())
    return [
        (resultado.puntaje, resultado.documento.doc_id, resultado.documento.titulo or "")
        for resultado in indice.buscar(consulta, k=k)
    ]


def consulta_de_humo(embedder: Any, consulta: str, k: int = 5) -> None:
    """Compara, sobre la misma pregunta, lo que recupera el vector y lo que recupera BM25.

    Es la prueba de que el índice sirve para algo. Se enseñan las dos listas juntas
    porque el argumento del proyecto no es «el vector es mejor», sino que **recuperan
    cosas distintas**: BM25 acierta cuando el cliente usa las palabras del catálogo, y el
    vector acierta cuando dice "me llegó más caro" y el documento dice "incremento de
    facturación". Si las dos listas fuesen idénticas, la mitad de la arquitectura sobraría.

    Se imprimen **tres** listas y no dos: el vectorial de pgvector, el BM25 real de
    producción y, al final, el ``plainto_tsquery`` de Postgres. La tercera se conserva a
    propósito aunque salga vacía, porque su vacío es informativo: enseña que el índice de
    texto completo de la base exige todos los términos y por eso no puede ser el juez de
    esta comparación.
    """
    vector = _literal_vector(_incrustar_consulta(embedder, consulta))
    firma = embedder.firma_modelo()

    print(f"\nCONSULTA: {consulta!r}")
    print(f"firma del modelo: {firma}\n")

    print("--- VECTORIAL (distancia coseno, pgvector <=>) ---")
    filas = _consultar(
        """
        SELECT faq_id, left(pregunta, 90), (embedding <=> %s::vector) AS distancia
        FROM faq
        WHERE activo AND embedding IS NOT NULL AND modelo_embedding = %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (vector, firma, vector, k),
    )
    for posicion, (faq_id, pregunta, distancia) in enumerate(filas, start=1):
        print(f"{posicion}. [{distancia:.4f}] {faq_id}  {pregunta}")

    print("\n--- BM25 REAL de producción (Okapi, packages/retriever/bm25.py) ---")
    for posicion, (puntaje, doc_id, titulo) in enumerate(_bm25_de_produccion(consulta, k), 1):
        print(f"{posicion}. [{puntaje:.4f}] {doc_id}  {titulo[:70]}")

    print("\n--- plainto_tsquery de Postgres (AND de todos los lexemas; se enseña para")
    print("    documentar por qué NO sirve como referencia, no como resultado) ---")
    filas = _consultar(
        """
        SELECT faq_id, left(pregunta, 90),
               ts_rank_cd(fts, plainto_tsquery('spanish', %s)) AS puntaje
        FROM faq
        WHERE activo AND fts @@ plainto_tsquery('spanish', %s)
        ORDER BY puntaje DESC
        LIMIT %s
        """,
        (consulta, consulta, k),
    )
    if not filas:
        print("(sin resultados léxicos: ninguna palabra de la pregunta está en el índice)")
    for posicion, (faq_id, pregunta, puntaje) in enumerate(filas, start=1):
        print(f"{posicion}. [{puntaje:.4f}] {faq_id}  {pregunta}")

    print("\n--- CASUÍSTICA (vectorial) ---")
    filas = _consultar(
        """
        SELECT casuistica_id, left(titulo, 90), (embedding <=> %s::vector) AS distancia
        FROM casuistica
        WHERE activo AND embedding IS NOT NULL AND modelo_embedding = %s
        ORDER BY embedding <=> %s::vector
        LIMIT 3
        """,
        (vector, firma, vector),
    )
    for posicion, (casuistica_id, titulo, distancia) in enumerate(filas, start=1):
        print(f"{posicion}. [{distancia:.4f}] {casuistica_id}  {titulo}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        prog="vectorizar_corpus",
        description="Calcula embeddings del corpus y los escribe en las columnas de Supabase.",
    )
    analizador.add_argument(
        "--tabla",
        action="append",
        choices=[destino.tabla for destino in DESTINOS],
        help="limita a una tabla (repetible); por defecto, las tres",
    )
    analizador.add_argument("--lote", type=int, default=LOTE_POR_DEFECTO, help="textos por petición")
    analizador.add_argument("--tpm", type=int, default=TPM_POR_DEFECTO, help="tokens/minuto")
    analizador.add_argument("--rpm", type=int, default=RPM_POR_DEFECTO, help="peticiones/minuto")
    analizador.add_argument("--limite", type=int, default=None, help="máximo de filas por tabla")
    analizador.add_argument(
        "--forzar", action="store_true", help="revectoriza todo, aunque ya tenga vector"
    )
    analizador.add_argument(
        "--verificar", action="store_true", help="solo cuenta lo vectorizado; no gasta cuota"
    )
    analizador.add_argument("--consulta", help="lanza una búsqueda de prueba y compara con BM25")
    analizador.add_argument("-v", "--verboso", action="store_true")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    opciones = _argumentos(argv)
    logging.basicConfig(
        level=logging.DEBUG if opciones.verboso else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cargar_entorno()

    from packages.retriever.vectorial import crear_embedder

    if opciones.verificar or opciones.consulta:
        for tabla, total, vectorizados, modelos, firma in verificar():
            print(
                f"{tabla:22} filas={total:6}  vectorizados={vectorizados:6}  "
                f"modelos={modelos}  firma={firma}"
            )
        if opciones.consulta:
            consulta_de_humo(crear_embedder(), opciones.consulta)
        return _SALIDA_OK

    embedder = crear_embedder()
    if embedder.nombre != "gemini":
        # Vectorizar con el embedder simulado y guardarlo en Supabase sería peor que no
        # tener vectores: la métrica de Retrieval Accuracy saldría medida sobre ruido con
        # forma de vector y nadie sabría que no es real.
        _LOG.error(
            "EMBED_MODE no resuelve a Gemini (embedder=%s). No se escriben vectores "
            "simulados en Supabase: revise GEMINI_API_KEY y GEMINI_EMBED_MODEL.",
            embedder.nombre,
        )
        return _SALIDA_ERROR

    _LOG.info("embedder: %s (dimensión %d)", embedder.firma_modelo(), embedder.dimension)
    cuota = ControlDeCuota(tokens_por_minuto=opciones.tpm, peticiones_por_minuto=opciones.rpm)
    elegidas = set(opciones.tabla or [destino.tabla for destino in DESTINOS])

    cortado = False
    with _conectar() as conexion:
        try:
            for destino in DESTINOS:
                if destino.tabla not in elegidas:
                    continue
                resumen = vectorizar_destino(
                    conexion,
                    destino,
                    embedder,
                    cuota,
                    tam_lote=opciones.lote,
                    forzar=opciones.forzar,
                    limite=opciones.limite,
                )
                _LOG.info("%s: %s", destino.tabla, resumen)
        except CuotaAgotada as error:
            _LOG.error("%s — se para aquí; relance el script cuando la cuota se libere", error)
            cortado = True

    print("\nESTADO EN SUPABASE tras la ejecución:")
    faltan = 0
    for tabla, total, vectorizados, modelos, firma in verificar():
        print(
            f"  {tabla:22} filas={total:6}  vectorizados={vectorizados:6}  "
            f"modelos={modelos}  firma={firma}"
        )
        faltan += total - vectorizados
    if cortado or faltan:
        print(f"\nQuedan {faltan} filas sin vector: relance el script para continuar.")
        return _SALIDA_PENDIENTE
    return _SALIDA_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
