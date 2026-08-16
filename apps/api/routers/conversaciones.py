"""Historial durable de conversaciones de BillSense."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import ConversacionesDep
from apps.api.security import Identidad, cuenta_autorizada, requiere_nivel
from packages.core_domain.enums import NivelAseguramiento

router = APIRouter(prefix="/v1/conversaciones", tags=["conversaciones"])
_LOG = logging.getLogger(__name__)


class NuevaConversacion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    periodo: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    canal: str = "APP"


class ResumenConversacion(BaseModel):
    conversation_id: uuid.UUID
    titulo: str
    canal: str
    periodo: str | None = None
    creada_en: datetime
    actualizada_en: datetime
    mensajes: int = 0


class MensajeConversacion(BaseModel):
    mensaje_id: uuid.UUID
    rol: str
    contenido: str
    bloques: list[dict[str, Any]] | None = None
    trace_id: str | None = None
    creado_en: datetime


class DetalleConversacion(BaseModel):
    conversation_id: uuid.UUID
    titulo: str
    canal: str
    periodo: str | None = None
    creada_en: datetime
    actualizada_en: datetime
    mensajes: list[MensajeConversacion]


def _fallo_base(error: Exception) -> HTTPException:
    _LOG.warning("historial de conversaciones no disponible: %s", error)
    return HTTPException(status_code=503, detail="historial de conversaciones no disponible")


@router.post("", response_model=ResumenConversacion, status_code=201)
def crear_conversacion(
    peticion: NuevaConversacion,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    almacen: ConversacionesDep,
) -> ResumenConversacion:
    cuenta = cuenta_autorizada(identidad, None)
    try:
        fila = almacen.crear(uuid.uuid4(), cuenta, canal=peticion.canal, periodo=peticion.periodo)
    except Exception as error:
        raise _fallo_base(error) from error
    return ResumenConversacion(**fila, mensajes=0)


@router.get("", response_model=list[ResumenConversacion])
def listar_conversaciones(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    almacen: ConversacionesDep,
    limite: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[ResumenConversacion]:
    cuenta = cuenta_autorizada(identidad, None)
    try:
        return [ResumenConversacion(**fila) for fila in almacen.listar(cuenta, limite=limite)]
    except Exception as error:
        raise _fallo_base(error) from error


@router.get("/{conversation_id}", response_model=DetalleConversacion)
def obtener_conversacion(
    conversation_id: uuid.UUID,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    almacen: ConversacionesDep,
) -> DetalleConversacion:
    cuenta = cuenta_autorizada(identidad, None)
    try:
        fila = almacen.obtener(conversation_id, cuenta)
    except Exception as error:
        raise _fallo_base(error) from error
    if fila is None:
        raise HTTPException(status_code=404, detail="conversación no encontrada")
    return DetalleConversacion(**fila)
