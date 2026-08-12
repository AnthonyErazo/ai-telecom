"""``POST /v1/derivacion`` — hand-off a un asesor humano con el contexto cargado.

La ficha pide *"flujos de derivación inteligente (hand-off) hacia asesores humanos
cuando la consulta salga del alcance de facturación, **transfiriendo el contexto de la
interacción**"*. Aquí se materializa ese contexto en dos cosas:

* un **``context_ref``**: referencia estable con la que el asesor recupera el turno
  completo (FactSet sellado, explicación entregada, verificación, historial);
* un **``resumen_asesor``**: el "modo asesor", un brief de siete líneas etiquetadas
  pensado para leerse **en menos de ocho segundos** mientras suena el teléfono. No es
  una narración: es una ficha escaneable, con el dato en la misma columna siempre.

El brief lleva importes, así que solo lo ven ``LOA2`` y ``LOA_ASESOR``; a ``LOA1`` se le
entrega saneado por :func:`apps.api.security.redactar_para_nivel`.

Este módulo publica :func:`construir_resumen_asesor` y :func:`nuevo_context_ref`, que
también usa ``/v1/explicar`` cuando la derivación la decide el sistema (invariante roto,
umbral de incomprensión, verificación fallida) y no el cliente.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import (
    AuditoriaDep,
    MemoriaDep,
    RegistroExplicacion,
    ReglasDep,
    RepositorioDep,
    TelemetriaDep,
    nuevo_trace_id,
)
from apps.api.errores import ErrorApi
from apps.api.routers.hechos import construir_hechos, payload_facts_built
from apps.api.security import Identidad, cuenta_autorizada, requiere_nivel
from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import (
    EtapaAuditoria,
    MotivoDerivacion,
    NivelAseguramiento,
)
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.paquete_asesor import ACCION_PENDIENTE
from packages.core_domain.esquemas.respuesta import Derivacion, PeticionDerivacion
from packages.retriever.saneador import sanear

__all__ = [
    "ACCION_PENDIENTE",
    "RespuestaDerivacion",
    "construir_contexto_derivacion",
    "construir_resumen_asesor",
    "nuevo_context_ref",
    "router",
]

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["derivacion"])

#: Espacio de nombres para que un mismo turno produzca siempre el mismo ``context_ref``.
NAMESPACE_CONTEXTO = uuid.UUID("6f1f2f5c-6d5b-5a2f-9d47-2f6c1a0b7e31")

# ``ACCION_PENDIENTE`` —la tarea del asesor por motivo— se mudó a
# ``packages.core_domain.esquemas.paquete_asesor``: la usan este endpoint y el brief del
# paquete del asesor, y dos copias de la misma tabla acabarían diciendo cosas distintas
# del mismo motivo. Se sigue reexportando desde aquí porque es donde la buscará quien
# venga leyendo el flujo de derivación.

class RespuestaDerivacion(BaseModel):
    """Lo que devuelve ``POST /v1/derivacion``."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    conversation_id: uuid.UUID
    derivacion: Derivacion
    context_ref: str
    resumen_asesor: str
    cola: str = Field(default="FACTURACION_104", description="Cola de atención destino")
    prioridad: str = "NORMAL"
    creado_en: datetime
    vigencia_min: int = Field(default=120, description="Minutos que el contexto sigue vivo")
    factset_sha256: str | None = None
    lineas_brief: int = Field(default=0, description="Líneas del brief (control de longitud)")


def nuevo_context_ref(conversation_id: str, trace_id: str) -> str:
    """Referencia estable del contexto de una derivación.

    Determinista sobre ``(conversation_id, trace_id)``: si el canal reintenta la misma
    derivación no se crean dos contextos para el mismo turno.
    """
    semilla = uuid.uuid5(NAMESPACE_CONTEXTO, f"{conversation_id}|{trace_id}")
    return f"ctx-{semilla.hex[:16]}"


def _linea_causa(factset: FactSet) -> str:
    """Línea CAUSA del brief: la causa dominante con su impacto y su confianza."""
    causa = factset.causa_dominante()
    if causa is None:
        return "CAUSA        sin causa atribuida por el motor (revisar el detalle)"
    movimientos = ", ".join(str(identificador) for identificador in causa.movimientos)
    referencia = f" · orden {movimientos}" if movimientos else ""
    return (
        f"CAUSA        {causa.etiqueta_cliente} · {formatear_soles(causa.monto_cent)} "
        f"({causa.participacion_pct}%) · confianza {causa.confianza:.2f}{referencia}"
    )


def construir_resumen_asesor(
    factset: FactSet,
    *,
    cuenta_id: str,
    motivo_codigo: MotivoDerivacion,
    utterance: str = "",
    canal: str = "APP",
    verificacion: str | None = None,
    modo: str | None = None,
    detalle_motivo: str | None = None,
) -> str:
    """Brief del asesor: siete líneas etiquetadas, legibles en menos de ocho segundos.

    El formato es deliberadamente rígido —etiqueta en mayúsculas, columna fija, un dato
    por línea— porque quien lo lee está con el cliente al teléfono. Cada línea responde
    una pregunta: quién, qué preguntó, cuánto varió, por qué, qué se le dijo ya, por qué
    llega a un humano y qué tiene que hacer el humano.

    Args:
        factset: hechos sellados del turno (la fuente de todas las cifras del brief).
        cuenta_id: cuenta atendida.
        motivo_codigo: motivo de la derivación.
        utterance: lo que escribió el cliente, recortado.
        canal: canal de origen.
        verificacion: veredicto del verificador numérico, si hubo explicación.
        modo: cómo se generó la explicación (``LLM``/``PLANTILLA``).
        detalle_motivo: matiz libre del motivo (regla dura disparada, score…).

    Returns:
        El brief en texto plano, listo para pintarse en la consola del asesor.
    """
    pregunta = " ".join(utterance.split())[:90] or "no registrada"
    vencimiento = (
        factset.fecha_vencimiento.strftime("%d/%m/%Y") if factset.fecha_vencimiento else "s/f"
    )
    ya_dicho = (
        f"explicación entregada · modo {modo or 'PLANTILLA'} · verificación "
        f"{verificacion or 'NO_APLICA'}"
        if verificacion
        else "aún no se le entregó explicación"
    )
    motivo_texto = detalle_motivo or str(motivo_codigo)
    lineas = [
        f"CLIENTE      {cuenta_id} · recibo {factset.periodo_actual} · renta "
        f"{factset.modalidad_renta} · vence {vencimiento}",
        f"CONSULTA     «{pregunta}» · canal {canal}",
        f"VARIACIÓN    {formatear_soles(factset.total_previo_cent)} → "
        f"{formatear_soles(factset.total_actual_cent)} "
        f"({formatear_soles(factset.delta_total_cent)})",
        _linea_causa(factset),
        f"YA EXPLICADO {ya_dicho}",
        f"DERIVA POR   {motivo_texto}",
        f"PENDIENTE    {ACCION_PENDIENTE.get(motivo_codigo, 'atender la consulta del cliente')}",
    ]
    if factset.deuda_anterior_cent:
        lineas.insert(
            3,
            f"DEUDA        arrastra {formatear_soles(factset.deuda_anterior_cent)} · "
            f"total a pagar {formatear_soles(factset.total_a_pagar_cent)}",
        )
    if not factset.invariante.ok:
        lineas.insert(
            4,
            f"⚠ DESCUADRE  residual de {factset.invariante.residual_cent} céntimos: "
            "no confirmar importes sin revisar",
        )
    return "\n".join(lineas)


def construir_contexto_derivacion(
    *,
    context_ref: str,
    trace_id: str,
    conversation_id: str,
    cuenta_id: str,
    factset: FactSet,
    motivo_codigo: MotivoDerivacion,
    resumen_asesor: str,
    utterance: str = "",
    canal: str = "APP",
    explicacion: RegistroExplicacion | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Contexto completo que recoge el asesor con el ``context_ref``.

    Guarda el FactSet **sellado**: el asesor ve exactamente las mismas cifras que vio el
    cliente, y el ``sha256`` prueba que nadie las tocó por el camino.
    """
    contexto: dict[str, Any] = {
        "context_ref": context_ref,
        "trace_id": trace_id,
        "conversation_id": conversation_id,
        "cuenta_id": cuenta_id,
        "canal": canal,
        "creado_en": datetime.now(UTC).isoformat(),
        "motivo_codigo": str(motivo_codigo),
        "resumen_asesor": resumen_asesor,
        "utterance": utterance,
        "factset": factset.model_dump(mode="json"),
        "factset_sha256": factset.sha256,
    }
    if explicacion is not None:
        contexto["explicacion"] = {
            "explicacion_id": explicacion.explicacion_id,
            "texto": explicacion.respuesta.texto,
            "modo": str(explicacion.respuesta.gobernanza.modo),
            "verificacion": explicacion.respuesta.gobernanza.verificacion_numerica,
            "aserciones_no_ancladas": explicacion.respuesta.gobernanza.aserciones_no_ancladas,
            "score_incomprension": explicacion.score_incomprension,
        }
    if extra:
        contexto.update(extra)
    return contexto


@router.post(
    "/derivacion",
    summary="Deriva a un asesor humano con el contexto de la interacción",
    response_model=RespuestaDerivacion,
)
def derivar(
    peticion: PeticionDerivacion,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA1))],
    repositorio: RepositorioDep,
    reglas: ReglasDep,
    auditoria: AuditoriaDep,
    memoria: MemoriaDep,
    telemetria: TelemetriaDep,
) -> RespuestaDerivacion:
    """Crea el contexto del hand-off y devuelve la referencia y el brief del asesor.

    Reutiliza el FactSet del último turno de la conversación si existe; si no —por
    ejemplo, el cliente pide asesor sin haber pedido explicación—, lo construye. Un
    hand-off sin hechos sería exactamente la derivación "a ciegas" que este proyecto
    quiere eliminar.
    """
    cuenta = cuenta_autorizada(identidad, peticion.cuenta_id)
    trace_id = nuevo_trace_id()
    conversacion = str(peticion.conversation_id)
    contexto_auditoria = identidad.contexto_auditoria()

    auditoria.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "POST /v1/derivacion",
            "periodo": peticion.periodo,
            "canal": str(identidad.canal or "APP"),
            "nivel": str(identidad.acr),
            "verbosidad": "NO_APLICA",
            "utterance": peticion.utterance,
            "conversation_id": conversacion,
        },
        **contexto_auditoria,
    )

    previa = memoria.ultima_de_conversacion(conversacion)
    if previa is not None and (not peticion.periodo or previa.periodo == peticion.periodo):
        factset = previa.factset
    else:
        factset, _ = construir_hechos(
            repositorio, reglas, cuenta, peticion.periodo, trace_id=trace_id
        )
        previa = None

    # Los hechos van a la bitácora **también aquí**, aunque el FactSet se reutilice del
    # turno anterior. Sin este evento, el turno de derivación quedaba registrado con su
    # motivo y su ``context_ref`` pero sin ni una cifra, y el paquete que el asesor
    # reconstruye desde la bitácora se quedaba sin desglose: el expediente decía a quién
    # atender y no qué mirar.
    auditoria.emitir(
        EtapaAuditoria.FACTS_BUILT,
        trace_id,
        {**payload_facts_built(factset), "reutilizado": previa is not None},
        **contexto_auditoria,
    )

    if factset.cuenta_id != cuenta:  # defensa en profundidad: nunca se cruzan cuentas
        raise ErrorApi(
            403,
            "CUENTA_NO_AUTORIZADA",
            "el contexto recuperado pertenece a otra cuenta",
            trace_id=trace_id,
        )

    context_ref = nuevo_context_ref(conversacion, trace_id)
    resumen = construir_resumen_asesor(
        factset,
        cuenta_id=cuenta,
        motivo_codigo=peticion.motivo_codigo,
        utterance=peticion.utterance or (previa.utterance if previa else ""),
        canal=str(identidad.canal or "APP"),
        verificacion=(
            previa.respuesta.gobernanza.verificacion_numerica if previa is not None else None
        ),
        modo=str(previa.respuesta.gobernanza.modo) if previa is not None else None,
        detalle_motivo=peticion.motivo,
    )
    memoria.guardar_contexto(
        context_ref,
        construir_contexto_derivacion(
            context_ref=context_ref,
            trace_id=trace_id,
            conversation_id=conversacion,
            cuenta_id=cuenta,
            factset=factset,
            motivo_codigo=peticion.motivo_codigo,
            resumen_asesor=resumen,
            utterance=peticion.utterance,
            canal=str(identidad.canal or "APP"),
            explicacion=previa,
        ),
    )
    memoria.marcar_derivada(conversacion)
    # Pedir asesor cierra la sonda de silencio como REPREGUNTA: la explicación no bastó.
    telemetria.registrar_turno_usuario(
        peticion.conversation_id, peticion.utterance, pide_humano=True
    )

    derivada = Derivacion(
        requerida=True,
        motivo=peticion.motivo or ACCION_PENDIENTE.get(peticion.motivo_codigo),
        motivo_codigo=peticion.motivo_codigo,
        context_ref=context_ref,
        resumen_asesor=resumen,
        senal_disparadora=f"peticion_explicita:{peticion.motivo_codigo}",
    )

    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "derivar": True,
            "motivo_codigo": str(peticion.motivo_codigo),
            "score_incomprension": previa.score_incomprension if previa else None,
            "modo": "DERIVACION_EXPLICITA",
            "context_ref": context_ref,
            # La misma señal que viaja en la respuesta, para que el paquete del asesor
            # pueda decir *por qué* llegó a una persona leyendo solo la bitácora.
            "senal_disparadora": f"peticion_explicita:{peticion.motivo_codigo}",
        },
        **contexto_auditoria,
    )
    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "bloques": 0,
            "acciones": 1,
            "modo": "DERIVACION",
            "derivada": True,
            "context_ref": context_ref,
            "lineas_brief": len(resumen.splitlines()),
        },
        **contexto_auditoria,
    )
    auditoria.cerrar_turno(trace_id, cuenta_ref=cuenta)

    if not identidad.ve_montos:
        # El brief lleva importes: en LOA1 se entrega saneado, igual que la explicación.
        resumen, _ = sanear(resumen)
        derivada = derivada.model_copy(update={"resumen_asesor": resumen})

    return RespuestaDerivacion(
        trace_id=trace_id,
        conversation_id=peticion.conversation_id,
        derivacion=derivada,
        context_ref=context_ref,
        resumen_asesor=resumen,
        creado_en=datetime.now(UTC),
        factset_sha256=factset.sha256 if identidad.ve_montos else None,
        lineas_brief=len(resumen.splitlines()),
    )


@router.get(
    "/derivacion/{context_ref}",
    summary="Contexto completo de una derivación (consola del asesor)",
)
def obtener_contexto(
    context_ref: str,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    memoria: MemoriaDep,
) -> dict[str, Any]:
    """Devuelve el contexto que dejó el hand-off.

    Es lo que abre el asesor del 104 al recibir la llamada: la ficha, el FactSet sellado
    y la explicación que ya se le dio al cliente, para no repetírsela.
    """
    contexto = memoria.contexto(context_ref)
    if contexto is None:
        raise ErrorApi(
            404,
            "CONTEXTO_NO_ENCONTRADO",
            f"no hay contexto vivo con la referencia {context_ref}",
            datos={"context_ref": context_ref},
        )
    if contexto.get("cuenta_id") != identidad.cuenta_ref:
        raise ErrorApi(
            403,
            "CUENTA_NO_AUTORIZADA",
            "el contexto pertenece a otra cuenta",
            datos={"context_ref": context_ref},
        )
    return contexto
