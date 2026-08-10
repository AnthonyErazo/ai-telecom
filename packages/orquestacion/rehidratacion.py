"""Recuperar un turno ya respondido desde el *checkpointer*, tras reiniciar el proceso.

El agujero que tapa este módulo
-------------------------------
El *checkpointer* de :mod:`packages.orquestacion.checkpointer` escribe el estado
completo del turno en SQLite, indexado por ``thread_id`` —que es el
``conversation_id``—. Comprobado: el estado sobrevive a un ``taskkill /F`` y el
``FactSet`` vuelve como ``FactSet``, con el mismo ``sha256`` y con los importes todavía
en ``int``.

Pero hasta aquí eso era **escritura sin lectura**: ``GET /v1/evidencia/{id}`` pregunta
por ``explicacion_id`` a :class:`~apps.api.deps.MemoriaConversaciones`, que vive en RAM
detrás de un ``lru_cache``. Al reiniciar el proceso el mapa está vacío y la respuesta
era ``404`` aunque el turno entero estuviera en disco a un palmo.

Este módulo es el índice que faltaba: dado un ``explicacion_id``, encuentra el
*checkpoint* cuyo estado lleva ese ``trace_id`` y reconstruye el
:class:`~apps.api.deps.RegistroExplicacion`.

Por qué hay que **buscar** en vez de consultar
----------------------------------------------
El *checkpointer* indexa por hilo y ``explicacion_id`` no es el hilo: una conversación
(un hilo) contiene muchos turnos (muchas explicaciones). No hay índice inverso, así que
se recorren los *checkpoints* de más nuevo a más viejo hasta dar con el ``trace_id``
buscado. Es un barrido, y por eso está **acotado** por :data:`LIMITE_BUSQUEDA`: se paga
solo cuando la memoria en RAM ya falló, es decir, después de un reinicio.

Consecuencia honesta del acotamiento: se recuperan los turnos **recientes**, no todos
los de la historia. Un turno lo bastante viejo como para caerse del límite sigue
devolviendo ``404``, que es el mismo comportamiento de antes y el que el propio mensaje
de error ya explica ("vuelva a pedir la explicación para regenerarla"). Un índice
inverso de verdad —``explicacion_id → thread_id`` en una tabla— es la solución
definitiva y está anotada como trabajo pendiente; esto entrega el caso que importa en la
demostración sin inventarse un esquema nuevo.

Garantías
---------
* **Nunca lanza.** Si el *checkpointer* no está, si el fichero está corrupto o si el
  estado no tiene la forma esperada, se devuelve ``None`` y quien llama responde el
  ``404`` de siempre. Esta función solo puede convertir un ``404`` en un ``200``, jamás
  al revés.
* **No relaja ninguna autorización.** Se rellena ``cuenta_ref`` con la cuenta que quedó
  grabada en el estado, así que la comprobación de propiedad que hace el endpoint
  —``registro.cuenta_ref != identidad.cuenta_ref`` ⇒ ``403``— sigue funcionando igual
  sobre un registro rehidratado que sobre uno vivo.
"""

from __future__ import annotations

import logging
from typing import Any

from packages.orquestacion.checkpointer import obtener_checkpointer

__all__ = ["LIMITE_BUSQUEDA", "explicacion_persistida", "rehidratar_explicacion"]

_LOG = logging.getLogger(__name__)

#: Cuántos *checkpoints* se inspeccionan como mucho al buscar una explicación. Cada
#: turno deja uno por nodo (cinco en el camino feliz), así que este límite cubre del
#: orden de las últimas cuarenta explicaciones: de sobra para la demostración y
#: suficientemente bajo como para que el barrido no se note.
LIMITE_BUSQUEDA = 200


def _valores(tupla: Any) -> dict[str, Any]:
    """``channel_values`` del *checkpoint*, o un diccionario vacío si no los hubiera."""
    punto = getattr(tupla, "checkpoint", None) or {}
    valores = punto.get("channel_values") if isinstance(punto, dict) else None
    return valores if isinstance(valores, dict) else {}


def explicacion_persistida(
    explicacion_id: str,
    *,
    saver: Any | None = None,
    limite: int = LIMITE_BUSQUEDA,
) -> Any | None:
    """Reconstruye desde disco el registro de una explicación ya entregada.

    Args:
        explicacion_id: el ``trace_id`` del turno, que es lo que publica la respuesta en
            ``telemetria.explicacion_id`` y en la cabecera ``X-Trace-Id``.
        saver: *checkpointer* alternativo (pruebas). Por defecto, el del proceso.
        limite: cuántos *checkpoints* inspeccionar como mucho.

    Returns:
        Un :class:`~apps.api.deps.RegistroExplicacion` equivalente al que había en
        memoria antes del reinicio, o ``None`` si no se encontró. **Nunca lanza.**
    """
    if not explicacion_id:
        return None
    # Import diferido: `apps.api.deps` importa la capa de orquestación en su ciclo de
    # vida, así que a nivel de módulo esto sería un ciclo.
    from apps.api.deps import RegistroExplicacion

    try:
        almacen = saver if saver is not None else obtener_checkpointer().saver
        for tupla in almacen.list(None, limit=limite):
            valores = _valores(tupla)
            if valores.get("trace_id") != explicacion_id:
                continue
            factset = valores.get("factset")
            respuesta = valores.get("respuesta")
            if factset is None or respuesta is None:
                # Es un checkpoint intermedio del mismo turno (el estado aún no tenía
                # respuesta). Se sigue buscando: el definitivo está más adelante.
                continue
            contexto = valores.get("contexto_recuperado")
            incomprension = valores.get("incomprension")
            registro = RegistroExplicacion(
                explicacion_id=explicacion_id,
                trace_id=explicacion_id,
                conversation_id=str(valores.get("conversation_id") or ""),
                cuenta_ref=str(valores.get("cuenta") or ""),
                periodo=factset.periodo_actual,
                factset=factset,
                respuesta=respuesta,
                evidencia=list(contexto.items_evidencia()) if contexto is not None else [],
                utterance=str(valores.get("utterance") or ""),
                canal=str(valores.get("canal") or "APP"),
                contexto_rag=dict(contexto.resumen_auditoria()) if contexto is not None else {},
                score_incomprension=(
                    round(incomprension.U, 4) if incomprension is not None else None
                ),
                derivada=bool(getattr(valores.get("derivacion"), "requerida", False)),
            )
            _LOG.info(
                "explicación %s rehidratada desde el checkpointer (hilo %s)",
                explicacion_id,
                registro.conversation_id,
            )
            return registro
    # Se captura todo a propósito: esto es un camino de recuperación. Si falla, quien
    # llama responde el 404 que ya respondía, no un 500 nuevo.
    except Exception as error:
        _LOG.warning(
            "no se pudo rehidratar la explicación %s desde el checkpointer: %s",
            explicacion_id,
            error,
        )
    return None


def rehidratar_explicacion(memoria: Any, explicacion_id: str) -> Any | None:
    """Busca la explicación en disco y, si la encuentra, la devuelve a la memoria viva.

    Reponerla en :class:`~apps.api.deps.MemoriaConversaciones` hace que el barrido se
    pague **una sola vez** por explicación: la siguiente consulta —y la que haga
    ``POST /v1/derivacion`` sobre el mismo turno— ya la encuentran en RAM.

    Args:
        memoria: la memoria de turno del proceso.
        explicacion_id: el ``trace_id`` del turno.

    Returns:
        El registro rehidratado, o ``None``. **Nunca lanza.**
    """
    registro = explicacion_persistida(explicacion_id)
    if registro is None:
        return None
    try:
        memoria.guardar_explicacion(registro)
    except Exception as error:  # pragma: no cover - la memoria es un dict con cerrojo
        _LOG.warning("no se pudo reponer %s en la memoria viva: %s", explicacion_id, error)
    return registro
