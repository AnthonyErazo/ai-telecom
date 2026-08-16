from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from apps.api.routers.conversaciones import (
    NuevaConversacion,
    crear_conversacion,
    listar_conversaciones,
    obtener_conversacion,
)
from apps.api.routers.explicar import _periodo_mencionado
from apps.api.security import Identidad
from packages.core_domain.enums import Canal, NivelAseguramiento


def _identidad(cuenta: str = "C-CHAT-01") -> Identidad:
    return Identidad(
        sub=cuenta,
        acr=NivelAseguramiento.LOA2,
        amr=["pwd", "app"],
        exp=datetime.now(UTC) + timedelta(hours=1),
        canal=Canal.APP,
    )


class _AlmacenFalso:
    def __init__(self) -> None:
        self.filas: dict[uuid.UUID, dict] = {}

    def crear(self, conversation_id, cuenta_ref, *, canal, periodo):
        ahora = datetime.now(UTC)
        fila = {
            "conversation_id": conversation_id,
            "cuenta_ref": cuenta_ref,
            "titulo": "Nueva conversación",
            "canal": canal,
            "periodo": periodo,
            "creada_en": ahora,
            "actualizada_en": ahora,
        }
        self.filas[conversation_id] = fila
        return fila

    def listar(self, cuenta_ref, *, limite):
        return [
            {**fila, "mensajes": 0}
            for fila in self.filas.values()
            if fila["cuenta_ref"] == cuenta_ref
        ][:limite]

    def obtener(self, conversation_id, cuenta_ref):
        fila = self.filas.get(conversation_id)
        if fila is None or fila["cuenta_ref"] != cuenta_ref:
            return None
        return {**fila, "mensajes": []}


def test_mes_explicito_del_texto_define_el_ciclo_sin_consultar_el_llm() -> None:
    repo = SimpleNamespace(cargar=lambda cuenta: (_ for _ in ()).throw(AssertionError(cuenta)))
    assert _periodo_mencionado("Explícame junio de 2026", None, repo, "C-1") == "2026-06"
    assert _periodo_mencionado("Revisa 2026/7", None, repo, "C-1") == "2026-07"


def test_mes_sin_anio_usa_la_referencia_mas_reciente() -> None:
    repo = SimpleNamespace(cargar=lambda cuenta: SimpleNamespace(periodo="2026-07"))
    assert _periodo_mencionado("Qué pasó en marzo", None, repo, "C-1") == "2026-03"
    assert _periodo_mencionado("Qué pasó en diciembre", None, repo, "C-1") == "2025-12"


def test_nuevo_chat_se_lista_y_se_recupera_solo_para_su_cuenta() -> None:
    almacen = _AlmacenFalso()
    identidad = _identidad()
    creado = crear_conversacion(
        NuevaConversacion(periodo="2026-07", canal="APP"), identidad, almacen
    )

    listado = listar_conversaciones(identidad, almacen, limite=30)
    detalle = obtener_conversacion(creado.conversation_id, identidad, almacen)

    assert [fila.conversation_id for fila in listado] == [creado.conversation_id]
    assert detalle.periodo == "2026-07"
    assert detalle.mensajes == []

