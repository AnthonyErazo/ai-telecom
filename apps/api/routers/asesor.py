"""Sala compartida: el asesor humano entra a la **misma** conversación.

Por qué existe este módulo
--------------------------
Hasta ahora la derivación era un **expediente en una cola**: el sistema preparaba el
resumen, las causas y la evidencia, marcaba la conversación como derivada y ahí
terminaba su papel. El asesor recogía el caso *en otro sitio*, y el cliente se quedaba
mirando un chat que ya no le hablaba.

La ficha pide *«derivar a un asesor humano con contexto»* y que los asesores reciban
*«derivaciones más filtradas y contextualizadas»* — y eso lo cumplía el expediente. Pero
la experiencia natural, la que el cliente espera de cualquier chat moderno, es otra: que
**una persona entre a la conversación que ya está abierta**, con todo lo dicho delante.

Las tres reglas que ordenan este módulo
---------------------------------------
1. **El verificador numérico NO se aplica a lo que escribe el asesor.** La garantía de
   cero cifras sin respaldo cubre a la máquina, que no puede responder de sus palabras.
   Una persona sí responde de las suyas. Aplicarle el verificador sería fingir que el
   sistema puede avalar a un humano, y bloquearle un mensaje correcto porque cita un
   dato que no está en el ``FactSet`` sería incorrecto y humillante. Lo que sí se hace
   es **dejar constancia nominal**: cada turno de asesor va a la bitácora con su
   identificador, de modo que quede claro quién dijo qué.

2. **El asistente no se apaga: pasa a copiloto.** Cuando hay un asesor dentro, la IA
   deja de hablarle al cliente y queda a disposición del asesor. Es la diferencia entre
   una IA que estorba y una que sabe quitarse de en medio sin dejar de ayudar.

3. **Un asesor por sala.** El segundo que intente entrar recibe un conflicto. Dos
   personas escribiendo a la vez al mismo cliente es peor experiencia que una cola.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import AuditoriaDep, MemoriaDep, nuevo_trace_id
from apps.api.errores import ErrorApi
from apps.api.security import Identidad, requiere_nivel
from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento
from packages.facts_engine.confianza import Turno

__all__ = ["EstadoSala", "MensajeAsesor", "TurnoPublicado", "router"]

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/asesor", tags=["asesor"])

#: Máximo de caracteres de un mensaje de asesor. No es una restricción de negocio: es
#: una defensa elemental contra un cuerpo de petición desmedido.
MAX_MENSAJE = 2000


# --------------------------------------------------------------------------- #
# Contratos
# --------------------------------------------------------------------------- #
class MensajeAsesor(BaseModel):
    """Lo que el asesor escribe al cliente dentro de la conversación."""

    model_config = ConfigDict(extra="forbid")

    texto: str = Field(min_length=1, max_length=MAX_MENSAJE)


class TurnoPublicado(BaseModel):
    """Un turno tal y como lo ve la consola del asesor o la del cliente."""

    model_config = ConfigDict(extra="forbid")

    rol: str
    texto: str
    ts: datetime | None = None
    autor: str | None = Field(
        default=None,
        description="Identificador del asesor, solo en los turnos de rol asesor",
    )


class EstadoSala(BaseModel):
    """Estado de la conversación: quién la atiende y qué se ha dicho."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    modo: str = Field(description="AUTONOMA cuando responde la IA, ASISTIDA con asesor dentro")
    asesor: str | None = None
    derivada: bool = False
    turnos: list[TurnoPublicado] = Field(default_factory=list)
    resumen_asesor: str | None = None
    cuenta_id: str | None = None


class ElementoCola(BaseModel):
    """Una derivación esperando a que alguien la recoja."""

    model_config = ConfigDict(extra="forbid")

    context_ref: str
    conversation_id: str
    cuenta_id: str | None = None
    motivo_codigo: str | None = None
    resumen_asesor: str | None = None
    trace_id: str | None = None
    creado_en: str | None = None


# --------------------------------------------------------------------------- #
# Ayudas
# --------------------------------------------------------------------------- #
_AsesorDep = Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA_ASESOR))]


def _identificador(identidad: Identidad) -> str:
    """Quién es el asesor a efectos de auditoría y de ocupación de la sala."""
    return getattr(identidad, "actor", None) or getattr(identidad, "sub", None) or "asesor"


def _contexto_de(memoria: Any, conversation_id: str) -> dict[str, Any] | None:
    """Busca el expediente de derivación asociado a una conversación."""
    for elemento in memoria.pendientes_de_atender():
        if elemento["conversation_id"] == conversation_id:
            return dict(elemento)
    ultima = memoria.ultima_de_conversacion(conversation_id)
    if ultima is None:
        return None
    return {"conversation_id": conversation_id, "cuenta_id": getattr(ultima, "cuenta_ref", None)}


def _sala(memoria: Any, conversation_id: str) -> EstadoSala:
    """Proyecta el estado de la sala a partir de la memoria de conversación."""
    asesor = memoria.asesor_presente(conversation_id)
    contexto = _contexto_de(memoria, conversation_id) or {}
    turnos = [
        TurnoPublicado(
            rol=turno.rol,
            texto=turno.utterance,
            ts=turno.ts,
            autor=asesor if turno.rol == "asesor" else None,
        )
        for turno in memoria.turnos(conversation_id)
        if turno.utterance
    ]
    return EstadoSala(
        conversation_id=conversation_id,
        modo="ASISTIDA" if asesor else "AUTONOMA",
        asesor=asesor,
        derivada=memoria.fue_derivada(conversation_id),
        turnos=turnos,
        resumen_asesor=contexto.get("resumen_asesor"),
        cuenta_id=contexto.get("cuenta_id"),
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get(
    "/cola",
    response_model=list[ElementoCola],
    summary="Derivaciones esperando a que un asesor las recoja",
)
def cola(identidad: _AsesorDep, memoria: MemoriaDep) -> list[ElementoCola]:
    """Cola del 104: casos derivados que nadie ha tomado todavía.

    Se excluyen las conversaciones que ya tienen un asesor dentro, porque una cola que
    muestra lo que otro está atendiendo genera dos personas sobre el mismo cliente.
    """
    return [ElementoCola(**elemento) for elemento in memoria.pendientes_de_atender()]


@router.post(
    "/conversacion/{conversation_id}/unirse",
    response_model=EstadoSala,
    summary="El asesor entra a la conversación y la IA pasa a copiloto",
)
def unirse(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> EstadoSala:
    """Suma al asesor a la sala. A partir de aquí el asistente no responde al cliente.

    Raises:
        ErrorApi: ``404`` si la conversación no existe; ``409`` si ya hay otro asesor.
    """
    if not memoria.turnos(conversation_id):
        raise ErrorApi(
            codigo="CONVERSACION_NO_ENCONTRADA",
            detalle="no hay ninguna conversación con ese identificador",
            estado=404,
        )
    asesor_id = _identificador(identidad)
    if not memoria.unir_asesor(conversation_id, asesor_id):
        raise ErrorApi(
            codigo="SALA_OCUPADA",
            detalle="otro asesor ya está atendiendo esta conversación",
            estado=409,
            datos={"asesor_actual": memoria.asesor_presente(conversation_id)},
        )
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "ASESOR_ENTRA",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "modo": "ASISTIDA",
        },
        **identidad.contexto_auditoria(),
    )
    _LOG.info("asesor %s entra en la conversación %s", asesor_id, conversation_id)
    return _sala(memoria, conversation_id)


@router.post(
    "/conversacion/{conversation_id}/mensaje",
    response_model=EstadoSala,
    summary="El asesor escribe al cliente dentro de la conversación",
)
def mensaje(
    conversation_id: str,
    cuerpo: MensajeAsesor,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> EstadoSala:
    """Publica un turno de rol ``asesor``.

    **Este texto no pasa por el verificador numérico**, y es deliberado: el verificador
    garantiza que la *máquina* no invente cifras. Una persona responde de sus propias
    palabras. Lo que sí queda es el rastro nominal en la bitácora.

    Raises:
        ErrorApi: ``409`` si quien escribe no es el asesor que ocupa la sala.
    """
    asesor_id = _identificador(identidad)
    presente = memoria.asesor_presente(conversation_id)
    if presente != asesor_id:
        raise ErrorApi(
            codigo="ASESOR_NO_EN_SALA",
            detalle="únase a la conversación antes de escribir en ella",
            estado=409,
            datos={"asesor_actual": presente},
        )
    texto = cuerpo.texto.strip()
    memoria.anotar_turno(
        conversation_id,
        Turno(utterance=texto, rol="asesor", ts=datetime.now(UTC), progreso=True),
    )
    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "MENSAJE_ASESOR",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "caracteres": len(texto),
            # Se declara explícitamente para que nadie lea la bitácora y crea que el
            # verificador avaló este texto: no lo hizo, ni debía.
            "verificacion_numerica": "NO_APLICA",
            "motivo_no_aplica": "turno escrito por una persona, no por el modelo",
        },
        **identidad.contexto_auditoria(),
    )
    return _sala(memoria, conversation_id)


@router.post(
    "/conversacion/{conversation_id}/salir",
    response_model=EstadoSala,
    summary="El asesor deja la sala y la IA vuelve a atender",
)
def salir(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> EstadoSala:
    """Libera la sala. El asistente vuelve a modo autónomo."""
    asesor_id = _identificador(identidad)
    memoria.salir_asesor(conversation_id)
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "ASESOR_SALE",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "modo": "AUTONOMA",
        },
        **identidad.contexto_auditoria(),
    )
    return _sala(memoria, conversation_id)


@router.get(
    "/conversacion/{conversation_id}",
    response_model=EstadoSala,
    summary="Estado de la sala y transcripción completa",
)
def estado(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
) -> EstadoSala:
    """Devuelve el estado y todos los turnos.

    Es lo que sondea la consola del asesor. Se eligió sondeo y no un canal permanente
    porque para una demostración el resultado visible es idéntico y la mitad de piezas
    pueden fallar. El salto a eventos del servidor no cambia este contrato.
    """
    return _sala(memoria, conversation_id)
