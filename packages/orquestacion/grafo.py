"""El grafo del turno: las mismas ramas de ``POST /v1/explicar``, hechas explícitas.

Qué es y qué no es
------------------
Es una **capa de orquestación**. El grafo decide qué función se llama y en qué orden;
las funciones son las que ya estaban. Ni una fórmula, ni un umbral, ni una cifra nacen
aquí: el motor determinístico sigue siendo el único que calcula y el verificador
numérico sigue siendo el único que autoriza a decir un número.

El diagrama, que es literalmente el del endpoint actual:

.. code-block:: text

    START → clasificar ─┬─ (no explica recibo) ─────────────► responder_intencion → END
                        └─ (EXPLICAR_RECIBO) → construir_hechos
                                                 ├─ invariante roto ──► derivar → END
                                                 └─ recuperar_contexto → generar
                                                        → verificar_y_armar
                                                              ├─ FAIL ──► derivar → END
                                                              └─ PASS ─────────────► END

La prioridad de la primera bifurcación
--------------------------------------
El orden **es la política**, no un detalle de implementación:

1. ``SOSPECHOSA`` manda sobre todo. Una frase que intenta manipular al asistente no se
   trata como consulta aunque mencione el recibo, y **no se envía al modelo**.
2. ``REGULATORIA`` — baja, portabilidad, reclamo formal. Deriva sin explicar, aunque la
   frase también pregunte por el recibo.
3. ``PEDIR_HUMANO`` — se le da, sin regatear.
4. El resto (``VACIO``, ``SALUDO``, ``CONSULTA_CONCEPTO``, ``FUERA_DE_DOMINIO``) se
   responde conversacionalmente, sin abrir la facturación.
5. Solo ``EXPLICAR_RECIBO`` construye el ``FactSet``.

Esa prioridad ya la aplica :func:`packages.facts_engine.intencion.clasificar_intencion`.
Aquí se **vuelve a escribir explícita** en :data:`PRIORIDAD_DE_RUTA` porque una arista
condicional que dependa de un orden implícito es una arista que nadie puede auditar.

Persistencia
------------
Se compila con el *checkpointer* de :mod:`packages.orquestacion.checkpointer`, que
indexa por ``thread_id`` = ``conversation_id``. Eso es lo que permite que la
conversación —historial de turnos, histéresis de derivación— sobreviva al reinicio del
proceso, que hoy la borra entera.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Literal

from packages.facts_engine.intencion import Intencion
from packages.orquestacion.checkpointer import Checkpointer, obtener_checkpointer
from packages.orquestacion.estado import (
    CORTE_INVARIANTE_ROTO,
    EstadoTurno,
    Servicios,
)
from packages.orquestacion.nodos import (
    clasificar,
    construir_hechos,
    derivar,
    generar,
    recuperar_contexto,
    responder_intencion,
    verificar_y_armar,
)
from packages.orquestacion.telemetria_externa import apagar_telemetria_externa

apagar_telemetria_externa()

from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402

__all__ = [
    "NOMBRE_GRAFO",
    "PRIORIDAD_DE_RUTA",
    "VAR_DURABILIDAD",
    "cerrar_grafo",
    "compilar_grafo",
    "construir_grafo",
    "durabilidad",
    "ejecutar_turno",
    "estado_del_checkpointer",
    "obtener_grafo",
    "ruta_por_intencion",
    "ruta_por_invariante",
    "ruta_por_verificacion",
]

_LOG = logging.getLogger(__name__)

#: Nombre del grafo compilado. Aparece en las trazas de LangGraph.
NOMBRE_GRAFO = "turno_recibo_claro"

#: Variable de entorno que fija cuándo se escribe el *checkpoint*.
#: ``sync`` (por defecto) escribe antes de ejecutar el paso siguiente: si el proceso
#: muere a mitad del turno, el estado hasta ahí queda en disco y se puede inspeccionar.
#: ``async`` escribe en segundo plano —más rápido, menos determinista— y ``exit`` solo
#: al terminar el turno.
VAR_DURABILIDAD = "CHECKPOINT_DURABILITY"
_DURABILIDADES = ("sync", "async", "exit")

#: Destino de la primera arista condicional, en **orden de prioridad**. Escrito así, en
#: vez de con un ``if intencion.explica_recibo``, porque la política tiene que poder
#: leerse de un vistazo y probarse elemento a elemento.
PRIORIDAD_DE_RUTA: tuple[tuple[Intencion, str], ...] = (
    (Intencion.SOSPECHOSA, "responder_intencion"),
    (Intencion.REGULATORIA, "responder_intencion"),
    # PEDIR_HUMANO por delante de DISPUTA_CARGO: las dos derivan, así que el cliente
    # acaba con una persona en ambos casos y lo único que cambia es el motivo que queda
    # registrado. Cuando alguien **pide** expresamente un asesor, ese es el motivo
    # honesto; inferir «disputa» sobre una petición explícita sería contarle al asesor
    # algo que el cliente no dijo.
    (Intencion.PEDIR_HUMANO, "responder_intencion"),
    (Intencion.DISPUTA_CARGO, "responder_intencion"),
    (Intencion.VACIO, "responder_intencion"),
    (Intencion.SALUDO, "responder_intencion"),
    (Intencion.PAGAR, "responder_intencion"),
    (Intencion.CONSUMO, "responder_intencion"),
    (Intencion.CONSULTA_CONCEPTO, "responder_intencion"),
    (Intencion.FUERA_DE_DOMINIO, "responder_intencion"),
    (Intencion.EXPLICAR_RECIBO, "construir_hechos"),
)


def durabilidad() -> str:
    """Modo de escritura del *checkpoint*, leído del entorno con respaldo ``sync``."""
    valor = (os.environ.get(VAR_DURABILIDAD) or "").strip().lower()
    if valor in _DURABILIDADES:
        return valor
    if valor:
        _LOG.warning(
            "%s=%r no es %s; se usa 'sync'", VAR_DURABILIDAD, valor, "|".join(_DURABILIDADES)
        )
    return "sync"


# --------------------------------------------------------------------------- #
# Aristas condicionales
# --------------------------------------------------------------------------- #
def ruta_por_intencion(estado: EstadoTurno) -> Literal["responder_intencion", "construir_hechos"]:
    """¿Corresponde explicar el recibo, o hay que responder sin abrir la facturación?

    Recorre :data:`PRIORIDAD_DE_RUTA` en orden. El respaldo final —``explica_recibo``—
    mantiene el invariante aunque mañana se añada una intención nueva y alguien olvide
    apuntarla en la tabla: **solo ``EXPLICAR_RECIBO`` toca el recibo**.
    """
    intencion = estado.get("intencion")
    if intencion is None:  # pragma: no cover - `clasificar` siempre la deja puesta
        return "responder_intencion"
    for candidata, destino in PRIORIDAD_DE_RUTA:
        if intencion.intencion is candidata:
            return destino  # type: ignore[return-value]
    return "construir_hechos" if intencion.explica_recibo else "responder_intencion"


def ruta_por_invariante(estado: EstadoTurno) -> Literal["derivar", "recuperar_contexto"]:
    """El invariante manda: si ``|residual_cent| > 1`` no se explica, se deriva.

    Nunca una "explicación aproximada": si la suma de las variaciones por concepto no
    reproduce la diferencia entre totales, cualquier número que se diera sería una
    conjetura con aspecto de dato.
    """
    return "derivar" if estado.get("corte") == CORTE_INVARIANTE_ROTO else "recuperar_contexto"


def ruta_por_verificacion(estado: EstadoTurno) -> Literal["derivar", "__end__"]:
    """Si el verificador no pudo anclar todo el texto, la respuesta no sale."""
    return "derivar" if estado.get("corte") else END  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Construcción
# --------------------------------------------------------------------------- #
def construir_grafo() -> StateGraph:
    """Arma el ``StateGraph`` del turno, sin compilar.

    Se devuelve sin compilar para que las pruebas puedan compilarlo con el
    *checkpointer* que les convenga (en memoria, típicamente) sin tocar el del proceso.
    """
    grafo: StateGraph = StateGraph(EstadoTurno, context_schema=Servicios)

    grafo.add_node("clasificar", clasificar)
    grafo.add_node("responder_intencion", responder_intencion)
    grafo.add_node("construir_hechos", construir_hechos)
    grafo.add_node("recuperar_contexto", recuperar_contexto)
    grafo.add_node("generar", generar)
    grafo.add_node("verificar_y_armar", verificar_y_armar)
    grafo.add_node("derivar", derivar)

    grafo.add_edge(START, "clasificar")
    grafo.add_conditional_edges(
        "clasificar",
        ruta_por_intencion,
        {"responder_intencion": "responder_intencion", "construir_hechos": "construir_hechos"},
    )
    grafo.add_edge("responder_intencion", END)
    grafo.add_conditional_edges(
        "construir_hechos",
        ruta_por_invariante,
        {"derivar": "derivar", "recuperar_contexto": "recuperar_contexto"},
    )
    grafo.add_edge("recuperar_contexto", "generar")
    grafo.add_edge("generar", "verificar_y_armar")
    grafo.add_conditional_edges(
        "verificar_y_armar",
        ruta_por_verificacion,
        {"derivar": "derivar", END: END},
    )
    grafo.add_edge("derivar", END)
    return grafo


def compilar_grafo(checkpointer: Any | None = None) -> CompiledStateGraph:
    """Compila el grafo con el *checkpointer* indicado (o el del proceso).

    Args:
        checkpointer: un ``BaseCheckpointSaver`` ya construido. Si es ``None`` se usa el
            del proceso, que degrada solo a memoria si el disco no acompaña.
    """
    saver = checkpointer if checkpointer is not None else obtener_checkpointer().saver
    return construir_grafo().compile(checkpointer=saver, name=NOMBRE_GRAFO)


@lru_cache(maxsize=1)
def obtener_grafo() -> CompiledStateGraph:
    """Grafo compilado del proceso. Se construye una sola vez, como el resto de deps."""
    return compilar_grafo()


def cerrar_grafo() -> None:
    """Olvida el grafo compilado. Lo usan las pruebas y el apagado ordenado."""
    obtener_grafo.cache_clear()


# --------------------------------------------------------------------------- #
# Ejecución de un turno
# --------------------------------------------------------------------------- #
def ejecutar_turno(
    estado_inicial: EstadoTurno,
    thread_id: str | None = None,
    servicios: Servicios | None = None,
    *,
    grafo: CompiledStateGraph | None = None,
) -> EstadoTurno:
    """Ejecuta un turno completo y devuelve el estado final.

    Args:
        estado_inicial: el estado que produce
            :func:`packages.orquestacion.estado.estado_inicial`.
        thread_id: hilo del *checkpointer*. Si se omite, el ``conversation_id``, que es
            lo correcto: un hilo por conversación.
        servicios: dependencias vivas. Si se omiten, se toman los singletons del
            proceso, de modo que el grafo se pueda ejecutar fuera de una petición HTTP.
        grafo: grafo compilado alternativo (pruebas).

    Returns:
        El :class:`~packages.orquestacion.estado.EstadoTurno` final. La respuesta lista
        para el cliente —ya redactada según el nivel— está en la clave ``respuesta``.

    Raises:
        ErrorApi: los errores de negocio del ACL (404/503/422) se **propagan** tal cual,
            igual que hoy hacen desde el endpoint.
    """
    compilado = grafo if grafo is not None else obtener_grafo()
    hilo = thread_id or estado_inicial["conversation_id"]
    contexto = servicios if servicios is not None else Servicios.desde_dependencias()
    salida = compilado.invoke(
        estado_inicial,
        {"configurable": {"thread_id": hilo}},
        context=contexto,
        durability=durabilidad(),  # type: ignore[arg-type]
    )
    return EstadoTurno(**salida)


def estado_del_checkpointer() -> dict[str, object]:
    """Diagnóstico del almacén de *checkpoints* para el log de arranque."""
    checkpointer: Checkpointer = obtener_checkpointer()
    estado = dict(checkpointer.estado())
    estado["durabilidad"] = durabilidad()
    estado["grafo"] = NOMBRE_GRAFO
    return estado
