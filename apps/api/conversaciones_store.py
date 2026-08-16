"""Persistencia PostgreSQL/Supabase del historial de BillSense."""

from __future__ import annotations

import json
import uuid
from typing import Any

from psycopg.rows import dict_row

_NAMESPACE_MENSAJE = uuid.UUID("f7d812ad-b1d7-4b0e-94cd-11dc5b84a183")


class AlmacenConversaciones:
    """Guarda y recupera conversaciones sin mezclar cuentas."""

    def __init__(self, dsn: str | None) -> None:
        self._dsn = (dsn or "").strip()

    @property
    def activo(self) -> bool:
        return bool(self._dsn)

    def _conexion(self):
        if not self.activo:
            raise RuntimeError("SUPABASE_DB_URL no está configurada")
        import psycopg

        return psycopg.connect(
            self._dsn,
            connect_timeout=10,
            autocommit=True,
            prepare_threshold=None,
            row_factory=dict_row,
        )

    def crear(
        self,
        conversation_id: uuid.UUID,
        cuenta_ref: str,
        *,
        canal: str = "APP",
        periodo: str | None = None,
        titulo: str = "Nueva conversación",
    ) -> dict[str, Any]:
        titulo = (titulo.strip() or "Nueva conversación")[:120]
        with self._conexion() as conexion:
            fila = conexion.execute(
                """
                INSERT INTO chat_conversacion
                    (conversation_id, cuenta_ref, canal, titulo, periodo)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                   SET actualizada_en = now(),
                       periodo = COALESCE(EXCLUDED.periodo, chat_conversacion.periodo),
                       titulo = CASE
                           WHEN chat_conversacion.titulo = 'Nueva conversación'
                           THEN EXCLUDED.titulo ELSE chat_conversacion.titulo END
                 WHERE chat_conversacion.cuenta_ref = EXCLUDED.cuenta_ref
                RETURNING conversation_id, cuenta_ref, canal, titulo, periodo,
                          creada_en, actualizada_en
                """,
                (conversation_id, cuenta_ref, canal, titulo, periodo),
            ).fetchone()
        if fila is None:
            raise PermissionError("la conversación pertenece a otra cuenta")
        return dict(fila)

    def guardar_intercambio(
        self,
        *,
        conversation_id: uuid.UUID,
        cuenta_ref: str,
        canal: str,
        periodo: str | None,
        utterance: str,
        trace_id: str,
        respuesta_texto: str,
        bloques: list[dict[str, Any]],
    ) -> None:
        """Inserta cliente y asistente en una transacción idempotente."""
        if not self.activo:
            return
        titulo = " ".join(utterance.strip().split())[:120] or "Nueva conversación"
        cliente_id = uuid.uuid5(_NAMESPACE_MENSAJE, f"{conversation_id}|{trace_id}|cliente")
        asistente_id = uuid.uuid5(_NAMESPACE_MENSAJE, f"{conversation_id}|{trace_id}|asistente")
        with self._conexion() as conexion, conexion.transaction():
            conversacion = conexion.execute(
                """
                INSERT INTO chat_conversacion
                    (conversation_id, cuenta_ref, canal, titulo, periodo)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                   SET actualizada_en = now(),
                       periodo = COALESCE(EXCLUDED.periodo, chat_conversacion.periodo),
                       titulo = CASE
                           WHEN chat_conversacion.titulo = 'Nueva conversación'
                           THEN EXCLUDED.titulo ELSE chat_conversacion.titulo END
                 WHERE chat_conversacion.cuenta_ref = EXCLUDED.cuenta_ref
                RETURNING conversation_id
                """,
                (conversation_id, cuenta_ref, canal, titulo, periodo),
            ).fetchone()
            if conversacion is None:
                raise PermissionError("la conversación pertenece a otra cuenta")
            for mensaje_id, rol, contenido, bloques_json in (
                (cliente_id, "cliente", utterance, None),
                (asistente_id, "asistente", respuesta_texto, json.dumps(bloques, ensure_ascii=False)),
            ):
                conexion.execute(
                    """
                    INSERT INTO chat_mensaje
                        (mensaje_id, conversation_id, rol, contenido, bloques, trace_id)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (mensaje_id) DO NOTHING
                    """,
                    (mensaje_id, conversation_id, rol, contenido, bloques_json, trace_id),
                )

    def listar(self, cuenta_ref: str, *, limite: int = 30) -> list[dict[str, Any]]:
        with self._conexion() as conexion:
            filas = conexion.execute(
                """
                SELECT c.conversation_id, c.titulo, c.canal, c.periodo,
                       c.creada_en, c.actualizada_en,
                       count(m.mensaje_id)::int AS mensajes
                  FROM chat_conversacion c
                  LEFT JOIN chat_mensaje m USING (conversation_id)
                 WHERE c.cuenta_ref = %s
                 GROUP BY c.conversation_id
                 ORDER BY c.actualizada_en DESC
                 LIMIT %s
                """,
                (cuenta_ref, limite),
            ).fetchall()
        return [dict(fila) for fila in filas]

    def obtener(self, conversation_id: uuid.UUID, cuenta_ref: str) -> dict[str, Any] | None:
        with self._conexion() as conexion:
            conversacion = conexion.execute(
                """
                SELECT conversation_id, titulo, canal, periodo, creada_en, actualizada_en
                  FROM chat_conversacion
                 WHERE conversation_id = %s AND cuenta_ref = %s
                """,
                (conversation_id, cuenta_ref),
            ).fetchone()
            if conversacion is None:
                return None
            mensajes = conexion.execute(
                """
                SELECT mensaje_id, rol, contenido, bloques, trace_id, creado_en
                  FROM chat_mensaje
                 WHERE conversation_id = %s
                 ORDER BY creado_en, mensaje_id
                """,
                (conversation_id,),
            ).fetchall()
        return {**dict(conversacion), "mensajes": [dict(fila) for fila in mensajes]}

    def turnos(self, conversation_id: uuid.UUID, cuenta_ref: str, *, limite: int = 20) -> list[dict[str, Any]]:
        detalle = self.obtener(conversation_id, cuenta_ref)
        if detalle is None:
            return []
        return list(detalle["mensajes"][-limite:])
