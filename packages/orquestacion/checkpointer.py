"""Persistencia del estado de conversación: ``SqliteSaver`` con degradación a memoria.

Qué resuelve
------------
``MemoriaConversaciones`` (``apps/api/deps.py``) guarda el historial de turnos, la
histéresis de derivación y las explicaciones citables **en RAM**, detrás de un
``@lru_cache(maxsize=1)``. Al reiniciar el proceso se pierde todo: ``GET
/v1/evidencia`` devuelve 404, el score de incomprensión pierde las señales ``s3``
(repregunta) y ``s6`` (turnos sin progreso), y una conversación ya derivada puede
volver a entrar al flujo normal porque ``fue_derivada`` vuelve a ser ``False``.

Este módulo aporta el almacén que faltaba: un fichero SQLite por proceso, indexado por
``thread_id`` —que es el ``conversation_id``—, escrito por el *checkpointer* de
LangGraph. No sustituye a ``MemoriaConversaciones``: los mapas por ``explicacion_id`` y
por ``context_ref`` siguen donde están, porque el *checkpointer* indexa por hilo y no
por explicación.

Decisiones y por qué
--------------------
* **Conexión propia con vida de proceso, no ``from_conn_string``.** Ese *helper* es un
  gestor de contexto que **cierra la conexión al salir del ``with``**: sirve para un
  guion, no para un servidor. Aquí se abre una conexión con
  ``check_same_thread=False`` y se cierra en el apagado ordenado.
* **Es seguro desde el endpoint síncrono de FastAPI.** ``explicar_recibo`` es ``def``,
  así que FastAPI lo ejecuta en el *threadpool* de anyio: cada petición cae en un hilo
  distinto. ``SqliteSaver`` toma un ``threading.Lock`` propio alrededor de cada
  operación y por eso admite ``check_same_thread=False``. El aviso del paquete sobre
  "no escalar a muchos hilos" es de **rendimiento** —el cerrojo serializa las
  escrituras—, no de corrección.
* **Si la ruta no se puede abrir, se degrada a memoria y se avisa. Nunca revienta.**
  Un disco lleno, un volumen de solo lectura o un directorio sin permisos no pueden
  tumbar la explicación de un recibo: el motor determinístico y el verificador siguen
  funcionando igual, solo se pierde la persistencia entre reinicios. Es la misma
  política que ya aplica el retriever cuando no hay pgvector.
* **Lista blanca de tipos al deserializar.** El serializador de LangGraph importa y
  construye la clase que diga el propio *checkpoint*; si alguien pudiera escribir en el
  fichero, podría provocar la carga de clases arbitrarias. Aquí se restringe a los
  tipos declarados en los módulos de dominio de ``recibo-claro`` (más los seguros que
  la propia biblioteca permite: ``datetime``, ``UUID``, ``set``…).

Configuración
-------------
=====================  ======================================================
Variable               Para qué
=====================  ======================================================
``CHECKPOINT_PATH``    Ruta del fichero SQLite. Relativa ⇒ desde la raíz del
                       repositorio. Por defecto ``data/checkpoints/turnos.sqlite``.
                       El valor especial ``:memory:`` fuerza el almacén en
                       memoria sin tocar el disco (útil en pruebas).
=====================  ======================================================
"""

from __future__ import annotations

import importlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from inspect import isclass
from pathlib import Path

from packages.orquestacion.telemetria_externa import apagar_telemetria_externa

# El apagado de la telemetría de terceros va **antes** de importar nada de LangGraph:
# `langsmith` cachea la lectura del entorno, así que el orden es parte del contrato.
apagar_telemetria_externa()

from langgraph.checkpoint.base import BaseCheckpointSaver  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

__all__ = [
    "MODULOS_DEL_DOMINIO",
    "RUTA_POR_DEFECTO",
    "VALOR_EN_MEMORIA",
    "VAR_RUTA_CHECKPOINT",
    "Checkpointer",
    "abrir_checkpointer",
    "cerrar_checkpointer",
    "obtener_checkpointer",
    "ruta_checkpoint",
    "serializador_del_dominio",
    "tipos_permitidos",
]

_LOG = logging.getLogger(__name__)

#: Variable de entorno que fija la ruta del fichero de *checkpoints*.
VAR_RUTA_CHECKPOINT = "CHECKPOINT_PATH"

#: Ruta por defecto, relativa a la raíz del repositorio. Vive en ``data/`` junto a la
#: bitácora y la telemetría, que ya está en ``.gitignore``: el estado de conversación
#: de un cliente no se versiona.
RUTA_POR_DEFECTO = Path("data") / "checkpoints" / "turnos.sqlite"

#: Valor de ``CHECKPOINT_PATH`` que pide explícitamente el almacén en memoria.
VALOR_EN_MEMORIA = ":memory:"

#: Módulos cuyos tipos puede reconstruir el *checkpointer* al leer. Es una lista
#: blanca: todo lo que no esté aquí (ni en la lista de tipos seguros de LangGraph) se
#: devuelve como diccionario en lugar de instanciarse.
MODULOS_DEL_DOMINIO: tuple[str, ...] = (
    "packages.core_domain.enums",
    "packages.core_domain.esquemas.auditoria",
    "packages.core_domain.esquemas.factset",
    "packages.core_domain.esquemas.movimiento",
    "packages.core_domain.esquemas.recibo",
    "packages.core_domain.esquemas.respuesta",
    "packages.facts_engine.confianza",
    "packages.facts_engine.intencion",
    "packages.governance.telemetria",
    "packages.llm_layer.generador",
    "packages.llm_layer.providers.base",
    "packages.llm_layer.verificador",
    "packages.retriever.corpus",
    "packages.retriever.hibrido",
)


def raiz_repositorio() -> Path:
    """Raíz del repositorio (``packages/orquestacion/checkpointer.py`` → tres arriba)."""
    return Path(__file__).resolve().parents[2]


def ruta_checkpoint(ruta: str | Path | None = None) -> Path | None:
    """Resuelve la ruta del fichero de *checkpoints*.

    Args:
        ruta: ruta explícita; si es ``None`` se lee ``CHECKPOINT_PATH`` y, si tampoco
            está, se usa :data:`RUTA_POR_DEFECTO`.

    Returns:
        La ruta absoluta del fichero, o ``None`` si se pidió el almacén en memoria.
    """
    bruta = str(ruta) if ruta is not None else (os.environ.get(VAR_RUTA_CHECKPOINT) or "").strip()
    if bruta == VALOR_EN_MEMORIA:
        return None
    destino = Path(bruta) if bruta else RUTA_POR_DEFECTO
    return destino if destino.is_absolute() else raiz_repositorio() / destino


@lru_cache(maxsize=1)
def tipos_permitidos() -> tuple[tuple[str, str], ...]:
    """Pares ``(módulo, clase)`` que el *checkpointer* puede reconstruir al leer.

    Se derivan de :data:`MODULOS_DEL_DOMINIO` en lugar de escribirse a mano para que
    añadir un modelo nuevo al dominio no obligue a acordarse de esta lista. Solo se
    toman las clases **definidas** en cada módulo, no las que él a su vez importa: así
    la lista blanca no crece por la puerta de atrás.
    """
    permitidos: set[tuple[str, str]] = set()
    for nombre in MODULOS_DEL_DOMINIO:
        try:
            modulo = importlib.import_module(nombre)
        # Un módulo que se moviera de sitio no puede impedir arrancar: se pierde su
        # entrada en la lista blanca y se avisa.
        except Exception as error:
            _LOG.warning("no se pudo inspeccionar %s para la lista blanca: %s", nombre, error)
            continue
        for objeto in vars(modulo).values():
            if isclass(objeto) and getattr(objeto, "__module__", None) == nombre:
                permitidos.add((nombre, objeto.__name__))
    return tuple(sorted(permitidos))


def serializador_del_dominio() -> JsonPlusSerializer:
    """Serializador restringido a los tipos de ``recibo-claro``.

    Sin esta restricción, LangGraph avisa —y en versiones futuras bloqueará— cada vez
    que reconstruye un tipo "no registrado"; y, sobre todo, un fichero de *checkpoints*
    manipulado podría hacer que el proceso importe y construya clases arbitrarias.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=tipos_permitidos())


@dataclass(slots=True)
class Checkpointer:
    """El *saver* elegido y el diagnóstico de por qué se eligió.

    El diagnóstico no es decorativo: es lo que se escribe en el log de arranque y lo
    que permite responder de un vistazo a "¿esta demo está persistiendo o no?".
    """

    saver: BaseCheckpointSaver
    #: Ruta del fichero, o ``":memory:"`` si el almacén no toca el disco.
    ruta: str
    #: ``True`` si el estado sobrevive al reinicio del proceso.
    persistente: bool
    #: Explicación legible de la elección (o de la degradación).
    motivo: str
    #: Conexión SQLite viva, para poder cerrarla en el apagado ordenado.
    conexion: sqlite3.Connection | None = None

    def estado(self) -> dict[str, object]:
        """Vista para el log de arranque y para ``/salud/preparacion``."""
        return {
            "tipo": type(self.saver).__name__,
            "ruta": self.ruta,
            "persistente": self.persistente,
            "motivo": self.motivo,
        }

    def cerrar(self) -> None:
        """Cierra la conexión SQLite si la hay. Idempotente y silencioso."""
        if self.conexion is None:
            return
        try:
            self.conexion.close()
        except Exception as error:
            _LOG.warning("error cerrando la conexión de checkpoints: %s", error)
        finally:
            self.conexion = None


def _en_memoria(motivo: str) -> Checkpointer:
    """Almacén en memoria: el grafo funciona igual, pero sin persistencia."""
    return Checkpointer(
        saver=InMemorySaver(serde=serializador_del_dominio()),
        ruta=VALOR_EN_MEMORIA,
        persistente=False,
        motivo=motivo,
    )


def abrir_checkpointer(ruta: str | Path | None = None) -> Checkpointer:
    """Abre el *checkpointer* SQLite y, si no se puede, degrada a memoria avisando.

    **Nunca lanza.** Perder la persistencia degrada la experiencia (el historial de la
    conversación no sobrevive al reinicio) pero no la corrección: las cifras salen del
    ``FactSet`` y el verificador sigue anclándolas igual.

    Args:
        ruta: ruta explícita del fichero; ``None`` para resolverla por entorno.

    Returns:
        El :class:`Checkpointer` elegido, con su diagnóstico.
    """
    destino = ruta_checkpoint(ruta)
    if destino is None:
        return _en_memoria(f"{VAR_RUTA_CHECKPOINT}={VALOR_EN_MEMORIA}: almacén en memoria")

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` es correcto aquí: `SqliteSaver` protege cada
        # operación con un `threading.Lock` propio, y el endpoint síncrono de FastAPI
        # corre en el threadpool, es decir, en un hilo distinto por petición.
        conexion = sqlite3.connect(str(destino), check_same_thread=False)
        saver = SqliteSaver(conexion, serde=serializador_del_dominio())
        # `setup()` es idempotente y el propio `cursor()` lo llamaría solo; hacerlo al
        # arrancar evita pagar la creación de tablas en la primera petición del cliente.
        saver.setup()
    # Se captura todo a propósito: disco lleno, permisos, volumen de solo lectura,
    # fichero corrupto… ninguna de esas cosas puede impedir explicar un recibo.
    except Exception as error:
        _LOG.error(
            "no se pudo abrir el fichero de checkpoints %s (%s); "
            "la conversación NO persistirá entre reinicios. "
            "Defina %s a una ruta escribible para recuperar la persistencia.",
            destino,
            error,
            VAR_RUTA_CHECKPOINT,
        )
        return _en_memoria(f"no se pudo abrir {destino}: {error}")

    _LOG.info("checkpoints de conversación en %s", destino)
    return Checkpointer(
        saver=saver,
        ruta=str(destino),
        persistente=True,
        motivo=f"SQLite en {destino}",
        conexion=conexion,
    )


@lru_cache(maxsize=1)
def obtener_checkpointer() -> Checkpointer:
    """*Checkpointer* del proceso, construido una sola vez.

    Sigue el mismo patrón que el resto de dependencias de ``apps/api/deps.py``: un
    ``lru_cache`` sin argumentos. Quien añada este singleton al ciclo de vida de la API
    debe registrar :func:`cerrar_checkpointer` en ``cerrar_recursos()``, o el apagado
    dejará una conexión SQLite huérfana.
    """
    return abrir_checkpointer()


def cerrar_checkpointer() -> None:
    """Cierra la conexión y limpia la caché. Apagado ordenado y pruebas."""
    try:
        obtener_checkpointer().cerrar()
    # El apagado nunca puede lanzar: dejaría el resto de recursos sin cerrar.
    except Exception as error:
        _LOG.warning("error cerrando el checkpointer: %s", error)
    obtener_checkpointer.cache_clear()
