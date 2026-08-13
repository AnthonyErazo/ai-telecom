"""Configuración de la API leída del entorno (sección 12 de la especificación).

Toda la configuración entra por variables de entorno o por el fichero ``.env``. No hay
constantes de despliegue repartidas por el código: si un valor cambia entre la demo y
producción, está aquí.

Las claves de la sección 12 se leen con su nombre literal (``ENTORNO``, ``LLM_MODE``,
``JWT_SECRET``…). Las variables propias de la capa de integración —las URLs de los
sistemas externos y las rutas de datos— llevan prefijo explícito y valores por defecto
que hacen funcionar la demo sin ningún ``.env``:

=========================  ==========================================================
Variable                   Para qué
=========================  ==========================================================
``MODO_ALMACENAMIENTO``    ``memoria`` | ``postgres`` | ``auto``. Ver más abajo.
``ORQUESTADOR``            ``grafo`` | ``directo``. Ver más abajo.
``BRAINYBILL_BASE_URL``    URL del mock o del BrainyBill real. Vacía ⇒ lectura local.
``AMDOCS_BASE_URL``        URL del mock o del Amdocs real. Vacía ⇒ lectura local.
``DATOS_SINTETICOS``       Raíz del dataset local (``data/sintetico``).
``HTTP_TIMEOUT_S``         Timeout de las llamadas a los sistemas externos.
``JWT_TTL_MIN``            Vida de los tokens que emite ``POST /dev/token``.
``CORS_ORIGENES``          Lista separada por comas; en ``dev`` se abre a ``*``.
``LOG_TERMINAL``           Imprime el resumen auditado de cada turno en la terminal.
=========================  ==========================================================

Orquestación: el grafo es la vía normal y la anterior sigue viva
----------------------------------------------------------------
``ORQUESTADOR`` decide quién conduce ``POST /v1/explicar``:

============  ===============================================================
Valor         Comportamiento
============  ===============================================================
``grafo``     (por defecto) ``packages.orquestacion`` — LangGraph con
              *checkpointer* persistente. Es el camino con el que se demuestra.
``directo``   La función lineal de ``apps/api/routers/explicar.py``, la que
              estaba verde antes de introducir LangGraph. No importa nada de
              LangGraph.
============  ===============================================================

Las dos vías producen la **misma** ``RespuestaCanalAgnostica`` y la **misma**
bitácora; eso lo comprueba ``tests/unit/test_grafo.py`` turno a turno. La doble vía
existe por una razón operativa concreta: si el día de la demo el grafo fallara, se
conmuta con una variable de entorno en vez de con un despliegue.

Un valor mal escrito cae a ``directo``, no al valor por defecto. Quien escribe
``ORQUESTADOR`` está normalmente intentando **salir** del grafo, y darle el grafo por
una errata sería lo contrario de lo que pidió; ``directo`` es además el camino que
lleva verde desde antes de que LangGraph existiera.

Almacenamiento: PostgreSQL nunca es obligatorio
-----------------------------------------------
``MODO_ALMACENAMIENTO`` es el único interruptor que decide si el proceso intenta
hablar con PostgreSQL. Lo único que persiste allí es el **índice vectorial del RAG**:
el dataset se lee del disco y la bitácora de auditoría es un JSONL local, así que sin
base de datos la API responde exactamente lo mismo.

============  ==============================================================
Valor         Comportamiento
============  ==============================================================
``memoria``   No se toca PostgreSQL aunque ``DATABASE_URL`` esté definida.
``postgres``  Se exige ``DATABASE_URL``; si no responde, se degrada avisando.
``auto``      (por defecto) PostgreSQL solo si ``DATABASE_URL`` trae valor.
============  ==============================================================

``auto`` es lo que hace que ``uvicorn apps.api.main:app`` funcione en una laptop
limpia —sin ``.env``, sin Docker y sin red— y que el mismo código use pgvector
dentro de ``docker compose``, donde el servicio ``api`` sí recibe ``DATABASE_URL``.
Por eso ``DATABASE_URL`` viene **vacía** por defecto: un DSN por defecto apuntando a
``db:5432`` (un host que solo existe dentro de la red de Docker) haría que cada
arranque local pagara el timeout de conexión antes de degradar.

``extra="ignore"``: el ``.env`` del proyecto lleva claves que consumen otros paquetes
(``EMBED_DIM``, ``GEMINI_EMBED_MODEL``…) y que aquí no se declaran. Que aparezcan no
puede tumbar el arranque de la API.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "ALMACENAMIENTO_AUTO",
    "ALMACENAMIENTO_MEMORIA",
    "ALMACENAMIENTO_POSTGRES",
    "ENTORNO_DESARROLLO",
    "MODOS_ALMACENAMIENTO",
    "ORQUESTADORES",
    "ORQUESTADOR_DIRECTO",
    "ORQUESTADOR_GRAFO",
    "Ajustes",
    "limpiar_cache_ajustes",
    "obtener_ajustes",
    "raiz_proyecto",
]

_LOG = logging.getLogger(__name__)

#: Valor de ``ENTORNO`` que habilita el router ``/dev`` y el CORS abierto.
ENTORNO_DESARROLLO = "dev"

#: Modos de ``MODO_ALMACENAMIENTO``.
ALMACENAMIENTO_MEMORIA = "memoria"
ALMACENAMIENTO_POSTGRES = "postgres"
ALMACENAMIENTO_AUTO = "auto"

#: Los tres valores admitidos, en el orden en que se documentan.
MODOS_ALMACENAMIENTO = (ALMACENAMIENTO_MEMORIA, ALMACENAMIENTO_POSTGRES, ALMACENAMIENTO_AUTO)

#: Valores de ``ORQUESTADOR``. ``grafo`` conduce el turno con
#: :mod:`packages.orquestacion` (LangGraph + *checkpointer*); ``directo`` usa la función
#: lineal del router, que es el respaldo de un solo interruptor si el grafo fallara.
ORQUESTADOR_GRAFO = "grafo"
ORQUESTADOR_DIRECTO = "directo"

#: Los dos valores admitidos, en el orden en que se documentan.
ORQUESTADORES = (ORQUESTADOR_GRAFO, ORQUESTADOR_DIRECTO)


def raiz_proyecto() -> Path:
    """Raíz del repositorio (``apps/api/settings.py`` → dos niveles arriba)."""
    return Path(__file__).resolve().parents[2]


def _volcar_env_al_proceso() -> None:
    """Copia ``.env`` a ``os.environ``, que es donde media aplicación lo busca.

    ``Ajustes`` declara ``env_file`` y pydantic-settings lee el fichero, pero lo carga
    **solo dentro del objeto de ajustes**: no toca el entorno del proceso. Y hay tres
    consumidores que no pasan por ``Ajustes`` y usan ``os.getenv`` directamente:

    * ``ORIGEN_RECIBOS`` en :func:`apps.api.acl.crear_repositorio`, que decide si los
      recibos salen de Supabase o del disco.
    * ``SUPABASE_DB_URL`` en :class:`apps.api.transporte_supabase.TransporteSupabase`.
    * ``DATABASE_URL`` en :func:`packages.retriever.vectorial.dsn_configurado`.

    El efecto era desconcertante: se ponía ``ORIGEN_RECIBOS=supabase`` en el ``.env``, el
    fichero se leía sin error, y el servicio seguía sirviendo el dataset sintético — con
    lo que las cuentas reales respondían «la cuenta no existe». Volcarlo aquí, en el
    módulo que importa todo lo demás, hace que el ``.env`` signifique lo mismo para los
    dos mecanismos.

    ``load_dotenv`` **no pisa** lo que ya venga del entorno: una variable exportada a mano
    o inyectada por Docker sigue mandando sobre el fichero, que es el orden correcto.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - viene con pydantic-settings
        _LOG.debug("python-dotenv no disponible: no se vuelca .env al entorno")
        return
    raiz = raiz_proyecto()
    for nombre in (".env", ".env.local"):
        load_dotenv(raiz / nombre, override=False)


_volcar_env_al_proceso()


def _destino_sin_credenciales(dsn: str | None) -> str:
    """``host:puerto/base`` de un DSN, sin usuario ni contraseña.

    El destino se escribe en el log de arranque y en ``/salud/preparacion``; la
    contraseña de la base no tiene por qué aparecer en ninguno de los dos.
    """
    if not dsn:
        return "memoria"
    resto = dsn.split("://", 1)[-1]
    return resto.rsplit("@", 1)[-1] or "postgres"


class Ajustes(BaseSettings):
    """Configuración completa del servicio.

    Se instancia una sola vez por proceso mediante :func:`obtener_ajustes`.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Sección 12: entorno y almacenamiento -------------------------------- #
    entorno: str = Field(default="dev", alias="ENTORNO")
    #: Vacía por defecto: la demo no exige PostgreSQL. El ``docker-compose`` la fija.
    database_url: str = Field(default="", alias="DATABASE_URL")
    #: DSN de Supabase. Lo usan el transporte de recibos y los corpus; se declara aquí
    #: además para que :meth:`dsn_postgres` pueda caer en él cuando ``DATABASE_URL`` esté
    #: vacía, que es el caso normal en este proyecto.
    supabase_db_url: str = Field(default="", alias="SUPABASE_DB_URL")
    modo_almacenamiento: str = Field(default=ALMACENAMIENTO_AUTO, alias="MODO_ALMACENAMIENTO")

    # --- Orquestación del turno ---------------------------------------------- #
    #: Quién conduce ``POST /v1/explicar``. Ver la tabla del encabezado del módulo.
    orquestador: str = Field(default=ORQUESTADOR_GRAFO, alias="ORQUESTADOR")

    # --- Sección 12: capa generativa ---------------------------------------- #
    llm_mode: str = Field(default="mock", alias="LLM_MODE")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="", alias="GEMINI_MODEL")
    gemini_live_model: str = Field(
        default="gemini-3.1-flash-live-preview", alias="GEMINI_LIVE_MODEL"
    )
    gemini_live_voice: str = Field(default="Kore", alias="GEMINI_LIVE_VOICE")
    gemini_live_session_min: int = Field(default=30, alias="GEMINI_LIVE_SESSION_MIN", ge=5, le=30)
    llm_timeout_s: float = Field(default=4.0, alias="LLM_TIMEOUT_S", ge=0.1, le=60.0)
    verificador_estricto: bool = Field(default=True, alias="VERIFICADOR_ESTRICTO")

    # --- Sección 12: reglas de negocio y determinismo ------------------------ #
    demo_seed: int = Field(default=20260804, alias="DEMO_SEED")
    rules_version: str = Field(default="1.0.0", alias="RULES_VERSION")
    rules_path: str = Field(default="", alias="RULES_PATH")

    # --- Sección 12: seguridad ----------------------------------------------- #
    jwt_secret: str = Field(default="solo-desarrollo-cambiar", alias="JWT_SECRET")
    jwt_algoritmo: str = Field(default="HS256", alias="JWT_ALGORITMO")
    jwt_emisor: str = Field(default="recibo-claro", alias="JWT_EMISOR")
    jwt_audiencia: str = Field(default="recibo-claro-api", alias="JWT_AUDIENCIA")
    jwt_ttl_min: int = Field(default=60, alias="JWT_TTL_MIN", ge=1, le=1440)

    # --- Integración con los sistemas de Movistar ---------------------------- #
    brainybill_base_url: str = Field(default="", alias="BRAINYBILL_BASE_URL")
    amdocs_base_url: str = Field(default="", alias="AMDOCS_BASE_URL")
    http_timeout_s: float = Field(default=5.0, alias="HTTP_TIMEOUT_S", ge=0.1, le=120.0)
    datos_sinteticos: str = Field(default="data/sintetico", alias="DATOS_SINTETICOS")
    ciclos_brainybill: int = Field(default=6, alias="CICLOS_BRAINYBILL", ge=2, le=24)

    # --- Gobernanza y telemetría --------------------------------------------- #
    audit_log_path: str = Field(default="", alias="AUDIT_LOG_PATH")
    telemetria_path: str = Field(default="", alias="TELEMETRIA_PATH")
    ventana_silencio_s: int = Field(default=1800, alias="VENTANA_SILENCIO_S", ge=60)
    log_terminal: bool = Field(default=True, alias="LOG_TERMINAL")

    # --- Servidor ------------------------------------------------------------ #
    cors_origenes: str = Field(default="", alias="CORS_ORIGENES")
    api_prefijo: str = Field(default="/v1", alias="API_PREFIJO")

    @field_validator("entorno", "llm_mode", "modo_almacenamiento", "orquestador", mode="before")
    @classmethod
    def _normalizar(cls, valor: object) -> object:
        """Recorta y baja a minúsculas los valores libres del entorno."""
        return valor.strip().lower() if isinstance(valor, str) else valor

    @field_validator("orquestador")
    @classmethod
    def _orquestador_conocido(cls, valor: str) -> str:
        """Distingue «no lo especifiqué» de «especifiqué algo que no entiendo».

        * **Vacío** ⇒ el valor por defecto (``grafo``). Es la convención del resto del
          ``.env.example``: ``AUDIT_LOG_PATH=`` o ``CHECKPOINT_PATH=`` también
          significan "usa el valor por defecto", y sería una trampa que esta clave
          fuera la única donde dejar la línea en blanco cambia el comportamiento.
        * **Escrito pero desconocido** ⇒ ``directo``, con un error en el log. Quien
          teclea ``ORQUESTADOR`` a mano casi siempre está intentando *salir* del grafo
          porque algo va mal; devolverle el grafo por una errata sería justo lo
          contrario de lo que pidió, y ``directo`` es el camino que lleva verde desde
          antes de que existiera la capa de orquestación.
        """
        if not valor:
            return ORQUESTADOR_GRAFO
        if valor not in ORQUESTADORES:
            _LOG.error(
                "ORQUESTADOR=%r no es uno de %s; se usa %r (la vía sin LangGraph)",
                valor,
                "|".join(ORQUESTADORES),
                ORQUESTADOR_DIRECTO,
            )
            return ORQUESTADOR_DIRECTO
        return valor

    @field_validator("modo_almacenamiento")
    @classmethod
    def _modo_conocido(cls, valor: str) -> str:
        """Un valor mal escrito avisa y cae a ``memoria``, que no exige infraestructura.

        No se lanza: dejar la API sin arrancar por una errata en una variable de entorno
        es peor que arrancar degradado diciéndolo en el log.
        """
        if valor not in MODOS_ALMACENAMIENTO:
            _LOG.warning(
                "MODO_ALMACENAMIENTO=%r no es uno de %s; se usa %r",
                valor,
                "|".join(MODOS_ALMACENAMIENTO),
                ALMACENAMIENTO_MEMORIA,
            )
            return ALMACENAMIENTO_MEMORIA
        return valor

    # --- Derivados ----------------------------------------------------------- #
    @property
    def es_desarrollo(self) -> bool:
        """``True`` si el router ``/dev`` y el CORS abierto están permitidos."""
        return self.entorno == ENTORNO_DESARROLLO

    @property
    def usa_grafo(self) -> bool:
        """``True`` si ``POST /v1/explicar`` debe delegar en la capa de orquestación."""
        return self.orquestador == ORQUESTADOR_GRAFO

    @property
    def dsn_postgres(self) -> str | None:
        """DSN a usar, o ``None`` si este proceso no debe tocar PostgreSQL.

        Es el **único** sitio donde se decide. Con ``memoria`` devuelve ``None`` aunque
        ``DATABASE_URL`` venga heredada del entorno: quien pide memoria no quiere pagar
        ni el timeout de conexión.

        Con ``DATABASE_URL`` vacía se cae en ``SUPABASE_DB_URL``, que apunta a la misma
        base. Exigir que el mismo destino se declarara con dos nombres distintos tenía un
        precio que no se veía: sin DSN el índice vectorial degrada a memoria, y un índice
        en memoria **no encuentra los vectores ya calculados**, así que le pedía a Gemini
        el corpus entero —cientos de documentos, uno por petición porque el modelo de
        embeddings no admite lotes— en cada arranque del proceso, contra una cuota de mil
        al día. El síntoma era un arranque de varios minutos y la cuota agotada a media
        tarde; la causa, un nombre de variable.
        """
        if self.modo_almacenamiento == ALMACENAMIENTO_MEMORIA:
            return None
        return self.database_url.strip() or self.supabase_db_url.strip() or None

    @property
    def usa_postgres(self) -> bool:
        """``True`` si el índice vectorial debe intentar persistir en pgvector."""
        return self.dsn_postgres is not None

    def almacenamiento(self) -> dict[str, object]:
        """Diagnóstico legible del almacenamiento, **sin credenciales**.

        Lo consumen el log de arranque y ``GET /salud/preparacion``: la pregunta
        «¿esto necesita una base de datos?» tiene que responderse de un vistazo.

        ``previsto`` es lo que este proceso **va a intentar**, no lo que consiguió: si
        se pidió PostgreSQL y no respondió, el resultado real está en
        ``rag.vectorial.respaldo`` de ``/salud/preparacion``.
        """
        dsn = self.dsn_postgres
        if dsn is not None:
            motivo = f"MODO_ALMACENAMIENTO={self.modo_almacenamiento} con DATABASE_URL definida"
        elif self.modo_almacenamiento == ALMACENAMIENTO_MEMORIA:
            motivo = "MODO_ALMACENAMIENTO=memoria"
        elif self.modo_almacenamiento == ALMACENAMIENTO_POSTGRES:
            motivo = "MODO_ALMACENAMIENTO=postgres pero DATABASE_URL está vacía"
        else:
            motivo = "DATABASE_URL no está definida"
        return {
            "modo": self.modo_almacenamiento,
            "previsto": "postgres" if dsn is not None else "memoria",
            "dsn_definido": bool(self.database_url.strip()),
            "destino": _destino_sin_credenciales(dsn),
            "motivo": motivo,
        }

    @property
    def ruta_datos(self) -> Path:
        """Ruta absoluta del dataset local (``data/sintetico`` por defecto)."""
        ruta = Path(self.datos_sinteticos)
        return ruta if ruta.is_absolute() else raiz_proyecto() / ruta

    @property
    def origenes_cors(self) -> list[str]:
        """Orígenes permitidos: ``*`` en desarrollo, lista explícita en el resto."""
        declarados = [origen.strip() for origen in self.cors_origenes.split(",") if origen.strip()]
        if declarados:
            return declarados
        return ["*"] if self.es_desarrollo else []

    def resumen(self) -> dict[str, object]:
        """Vista de arranque **sin secretos**: se escribe en el log de inicio.

        ``brainybill`` declara ``supabase:cargo_facturado`` cuando ``ORIGEN_RECIBOS`` lo
        pide, porque decir ``archivo:data/sintetico`` mientras el ACL sirve el dataset
        real es peor que no decir nada: es la primera línea que se mira cuando algo va
        mal, y mandaba a buscar el problema al sitio equivocado.
        """
        recibos_en_supabase = (
            os.environ.get("ORIGEN_RECIBOS", "").strip().lower() == "supabase"
        )
        if recibos_en_supabase:
            return {
                **self._resumen_base(),
                "brainybill": "supabase:cargo_facturado",
                # Amdocs sigue en disco a propósito: el dataset del desafío no trae
                # órdenes de CRM. Lo dice `crear_repositorio`.
                "amdocs": f"archivo:{self.ruta_datos}",
            }
        return self._resumen_base()

    def _resumen_base(self) -> dict[str, object]:
        """El resumen con los orígenes deducidos de la configuración HTTP/disco."""
        return {
            "entorno": self.entorno,
            "llm_mode": self.llm_mode,
            "orquestador": self.orquestador,
            "almacenamiento": f"{self.modo_almacenamiento}→{self.almacenamiento()['previsto']}",
            "rules_version": self.rules_version,
            "verificador_estricto": self.verificador_estricto,
            "brainybill": self.brainybill_base_url or f"archivo:{self.ruta_datos}",
            "amdocs": self.amdocs_base_url or f"archivo:{self.ruta_datos}",
            "jwt_secreto_por_defecto": self.jwt_secret == "solo-desarrollo-cambiar",
            "cors": self.origenes_cors,
        }


@lru_cache(maxsize=1)
def obtener_ajustes() -> Ajustes:
    """Devuelve la configuración del proceso (cacheada).

    Es la dependencia que inyecta FastAPI: una sola lectura del entorno por proceso.
    """
    ajustes = Ajustes()  # type: ignore[call-arg]
    if not ajustes.es_desarrollo and ajustes.jwt_secret == "solo-desarrollo-cambiar":
        _LOG.error(
            "JWT_SECRET tiene el valor de desarrollo con ENTORNO=%s: "
            "cualquiera puede firmar un token. Cámbielo antes de exponer la API.",
            ajustes.entorno,
        )
    return ajustes


def limpiar_cache_ajustes() -> None:
    """Invalida la caché de configuración (tests que manipulan el entorno)."""
    obtener_ajustes.cache_clear()
