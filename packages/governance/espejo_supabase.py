"""Espejo de la bitácora de auditoría en Supabase.

Qué es y qué no es
------------------
Un **espejo**, no la fuente. La evidencia sigue siendo el fichero JSONL: se escribe con
``fsync`` en el mismo acto en que se calcula el hash, y la cadena se verifica sobre él. Lo
que se copia aquí es lo mismo, para que el equipo y el jurado puedan mirar la trazabilidad
en Supabase sin pedir un fichero a nadie.

Que sea un espejo tiene una consecuencia que conviene decir en voz alta: **un fallo de red
no puede tumbar un turno**. Si la copia falla, el turno ya está registrado donde importa y
el cliente ya tiene su respuesta. Se anota el fallo y se sigue.

Por qué se vuelca por turno y no por evento
-------------------------------------------
Un turno emite entre cuatro y ocho eventos. Con la base en ``ca-central-1``, un `INSERT`
por evento añadiría medio segundo a cada respuesta —más que el propio modelo en algunos
turnos— y por nada: la cadena ya está cerrada en disco. Se acumulan en memoria y se
insertan de una vez al cerrar el turno, un viaje por conversación.

La ordenación no se pierde
--------------------------
Cada evento lleva su ``indice`` y su ``hash_prev``, así que el orden se reconstruye desde
los datos y no depende de en qué orden llegaran las filas. Por eso el volcado puede ser un
único `executemany` sin transacción explícita.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

__all__ = ["EspejoSupabase", "espejo_por_defecto"]

_LOG = logging.getLogger(__name__)

_INSERCION = """
INSERT INTO auditoria_evento
    (cadena, indice, trace_id, etapa, ts, actor, cuenta_ref,
     acting_on_behalf_of, nivel, payload, canonico, hash_prev, hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

_TELEMETRIA = """
INSERT INTO telemetria_turno
    (trace_id, conversation_id, ocurrido_en, canal, intencion, verificacion_numerica,
     aserciones_totales, aserciones_no_ancladas, derivada, motivo_derivacion,
     latencia_ms, modelo, payload)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT DO NOTHING
"""


class EspejoSupabase:
    """Acumula eventos de auditoría y los vuelca a ``auditoria_evento``."""

    def __init__(self, cadena: str, dsn: str | None = None) -> None:
        """Prepara el espejo. No conecta todavía: se conecta al primer volcado.

        Args:
            cadena: identificador de la cadena de hashes, normalmente el nombre del
                fichero JSONL. Permite que convivan varias cadenas en la misma tabla sin
                que sus índices choquen.
            dsn: cadena de conexión. Por defecto ``SUPABASE_DB_URL``.
        """
        self.cadena = cadena
        self._dsn = (dsn or os.getenv("SUPABASE_DB_URL") or "").strip()
        self._pendientes: list[tuple[Any, ...]] = []
        # Un solo aviso por proceso: si la base no está, repetirlo en cada turno llenaría
        # el log de ruido sin añadir información.
        self._aviso_dado = False

    @property
    def activo(self) -> bool:
        """¿Hay a dónde copiar? Sin DSN el espejo es un no-op silencioso."""
        return bool(self._dsn)

    def encolar(self, evento: Any) -> None:
        """Guarda un evento para el próximo volcado. Nunca lanza."""
        if not self.activo:
            return
        try:
            self._pendientes.append(
                (
                    self.cadena,
                    evento.indice,
                    evento.trace_id,
                    str(evento.etapa),
                    evento.ts,
                    evento.actor,
                    evento.cuenta_ref,
                    evento.acting_on_behalf_of,
                    str(evento.nivel) if evento.nivel is not None else None,
                    json.dumps(evento.payload, ensure_ascii=False, default=str),
                    # EXACTAMENTE lo que Python hasheó. La tabla lleva un CHECK que
                    # recalcula `sha256(hash_prev || canonico)` y rechaza la fila si no
                    # cuadra: PostgreSQL verifica la cadena por su cuenta, sin fiarse de
                    # nosotros. Guardar aquí la línea JSONL —que incluye el propio hash—
                    # hacía fallar ese CHECK, y con razón.
                    evento.json_canonico(),
                    evento.hash_previo,
                    evento.hash,
                )
            )
        except Exception as exc:  # pragma: no cover - defensa, no camino normal
            _LOG.debug("auditoría: no se pudo encolar el evento (%s)", type(exc).__name__)

    def _telemetria(self) -> tuple[Any, ...] | None:
        """Deriva la fila de métricas del turno a partir de los eventos ya encolados.

        Las tres métricas que evalúa el desafío —exactitud de recuperación, tasa de
        alucinación y precisión del hand-off— salen de datos que ya viajan en la cadena:
        ``VERIFY`` trae el veredicto y las aserciones sin anclar, ``ROUTE`` si se derivó y
        por qué, ``LLM_CALL`` el modelo y la latencia. Recogerlos aquí evita añadir una
        llamada de telemetría en cada router, que es donde se olvidaría al añadir el
        cuarto canal.

        Devuelve ``None`` si el turno no llegó a verificarse: una fila de métricas sin
        veredicto contaría como un turno más en el denominador y hundiría la tasa sin
        que nadie hubiera respondido nada.
        """
        verify = next((f for f in self._pendientes if f[3] == "VERIFY"), None)
        if verify is None:
            return None

        # Se funden los payloads de todo el turno en orden. Un turno emite varios ROUTE
        # —uno por intención, otro por derivación— y quedarse con «el ROUTE» perdería uno
        # de los dos. Fundir también aísla esta función de en qué etapa concreta decida
        # el orquestador anotar cada dato: si el campo está en la cadena, aquí llega.
        datos: dict[str, Any] = {}
        for fila in self._pendientes:
            try:
                cargado = json.loads(fila[9])
            except Exception:
                continue
            if isinstance(cargado, dict):
                datos.update({k: v for k, v in cargado.items() if v is not None})

        aserciones = datos.get("aserciones")
        return (
            verify[2],                                   # trace_id
            datos.get("conversation_id"),
            verify[4],                                   # ts del VERIFY
            datos.get("canal"),
            datos.get("intencion"),
            datos.get("veredicto"),
            len(aserciones) if isinstance(aserciones, list) else datos.get("aserciones_totales"),
            datos.get("aserciones_no_ancladas"),
            bool(datos.get("derivada") or datos.get("motivo_derivacion")),
            datos.get("motivo_derivacion"),
            datos.get("latencia_ms"),
            datos.get("modelo"),
            json.dumps({"cadena": self.cadena}, ensure_ascii=False),
        )

    def volcar(self) -> int:
        """Inserta lo acumulado y vacía la cola. Devuelve cuántas filas se copiaron.

        La cola se vacía **pase lo que pase**: reintentar indefinidamente una cadena que
        la base rechaza haría crecer la memoria del proceso sin límite, y la evidencia
        buena ya está en el fichero.
        """
        if not self._pendientes:
            return 0
        metricas = self._telemetria()
        filas, self._pendientes = self._pendientes, []
        if not self.activo:
            return 0
        try:
            import psycopg
        except ImportError:  # pragma: no cover - psycopg es dependencia declarada
            return 0
        try:
            # prepare_threshold=None: el pooler de Supabase reutiliza sesiones y las
            # sentencias preparadas chocan entre conexiones ("_pg3_0 already exists").
            with psycopg.connect(
                self._dsn, connect_timeout=10, autocommit=True, prepare_threshold=None
            ) as conexion:
                conexion.cursor().executemany(_INSERCION, filas)
                if metricas is not None:
                    conexion.execute(_TELEMETRIA, metricas)
        except Exception as exc:
            if not self._aviso_dado:
                _LOG.warning(
                    "auditoría: no se pudo espejar en Supabase (%s); el JSONL sigue siendo "
                    "la evidencia",
                    type(exc).__name__,
                )
                self._aviso_dado = True
            return 0
        return len(filas)


def espejo_por_defecto(cadena: str) -> EspejoSupabase:
    """El espejo que usa :class:`RegistroAuditoria` si no le dan otro."""
    return EspejoSupabase(cadena)
