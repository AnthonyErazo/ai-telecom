"""Apagado explícito de la telemetría de terceros (LangSmith) — **efecto al importar**.

Por qué existe este módulo
--------------------------
``langgraph`` arrastra ``langchain-core``, y ``langchain-core`` arrastra ``langsmith``
como dependencia transitiva. ``langsmith`` es MIT, así que su presencia no compromete
la cesión de propiedad intelectual de la sección 9 de las BASES; lo que sí sería
inaceptable es que **el contenido de un turno saliera del proceso**. El texto que
maneja este sistema incluye el ``utterance`` del cliente y las cifras de su recibo:
mandarlo a un servicio SaaS de terceros sería una fuga de datos de facturación, y ni
Integratel ni Movistar la han autorizado.

El interruptor real está en ``langsmith.utils.tracing_is_enabled()``, que resuelve
``TRACING_V2`` con respaldo en ``TRACING`` sobre los espacios de nombres ``LANGSMITH``
y ``LANGCHAIN``, y compara contra la cadena ``"true"``. Por defecto —sin variables— el
trazado ya está apagado, pero **no basta con confiar en el defecto**: un ``.env``
heredado, una variable de CI o la máquina de un compañero pueden encenderlo sin que
nadie lo note. Por eso aquí se **fuerza** el valor, no se usa ``setdefault``.

``langsmith.utils.get_env_var`` está decorado con ``functools.lru_cache``, de modo que
si alguien ya llamó a la función antes de que se fijaran las variables, el valor viejo
quedaría cacheado. Para que el orden de importación deje de importar, este módulo
**invalida esa caché** después de escribir el entorno.

Uso: no hay que llamar a nada. Importar el módulo apaga la telemetría. Aun así se
expone :func:`apagar_telemetria_externa` para poder invocarla explícitamente (y para
que se lea en el código del que la usa que el apagado es deliberado).
"""

from __future__ import annotations

import logging
import os
import sys

__all__ = [
    "VARIABLES_APAGADO",
    "VARIABLES_VACIADAS",
    "apagar_telemetria_externa",
    "telemetria_externa_activa",
]

_LOG = logging.getLogger(__name__)

#: Interruptores que se fuerzan a ``false``. Los cuatro primeros son los que consulta
#: ``tracing_is_enabled()``; los dos últimos evitan el exportador OTel y el hilo de
#: envío en segundo plano de los *callbacks*.
VARIABLES_APAGADO: dict[str, str] = {
    "LANGSMITH_TRACING": "false",
    "LANGSMITH_TRACING_V2": "false",
    "LANGCHAIN_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
    "LANGSMITH_OTEL_ENABLED": "false",
    "LANGCHAIN_CALLBACKS_BACKGROUND": "false",
}

#: Credenciales y destinos que se vacían: sin clave y sin endpoint no hay a dónde
#: mandar nada aunque alguien encendiera el trazado más adelante.
VARIABLES_VACIADAS: tuple[str, ...] = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_ENDPOINT",
)


def _invalidar_cache_langsmith() -> None:
    """Vacía la caché de ``langsmith.utils.get_env_var`` si el módulo ya está cargado.

    Sin esto, apagar la telemetría **después** de que algo haya consultado el entorno
    no tendría efecto: el valor viejo seguiría cacheado. Con esto, el orden de
    importación deja de ser una trampa.
    """
    modulo = sys.modules.get("langsmith.utils")
    if modulo is None:
        return
    for nombre in ("get_env_var", "get_tracer_project", "tracing_is_enabled"):
        limpiar = getattr(getattr(modulo, nombre, None), "cache_clear", None)
        if callable(limpiar):
            limpiar()


def apagar_telemetria_externa() -> dict[str, str]:
    """Fuerza el apagado del trazado hacia LangSmith y devuelve lo que dejó escrito.

    Se **sobrescribe** el valor previo a propósito: el objetivo no es "poner un
    defecto sensato" sino garantizar que ninguna configuración heredada pueda encender
    el envío de conversaciones de clientes a un servicio externo.

    Returns:
        El mapa ``variable -> valor`` efectivamente escrito en ``os.environ``.
    """
    escrito: dict[str, str] = {}
    for nombre, valor in VARIABLES_APAGADO.items():
        os.environ[nombre] = valor
        escrito[nombre] = valor
    for nombre in VARIABLES_VACIADAS:
        os.environ[nombre] = ""
        escrito[nombre] = ""
    _invalidar_cache_langsmith()
    return escrito


def telemetria_externa_activa() -> bool:
    """``True`` si LangSmith considera que el trazado está encendido.

    Pregunta a la propia biblioteca en vez de reinterpretar las variables: si el día de
    mañana ``langsmith`` cambia el nombre del interruptor, esta comprobación se entera y
    la nuestra no. Si ``langsmith`` no está instalado, no hay telemetría que apagar.
    """
    try:  # pragma: no cover - depende de la instalación
        from langsmith.utils import tracing_is_enabled
    # Sin `langsmith` instalado no hay telemetría que apagar, y eso es una respuesta
    # correcta, no un error.
    except Exception:
        return False
    try:
        return bool(tracing_is_enabled())
    # Una API de terceros que cambiara de forma no puede tumbar el turno.
    except Exception as error:
        _LOG.warning("no se pudo consultar el estado del trazado de LangSmith: %s", error)
        return False


# El apagado ocurre **al importar**: cualquier módulo de este paquete que vaya a tocar
# LangGraph importa esto primero, y así no existe una ventana en la que el trazado
# pudiera estar encendido.
apagar_telemetria_externa()
