"""``GET /v1/historial`` — hasta 5 recibos anteriores de la cuenta, resumidos.

`RepositorioCuentas.cargar` (``apps/api/acl.py``) ya trae hasta ``ciclos`` periodos
(6 por defecto) y deja los anteriores al actual en ``DatosCuenta.previos``; este
endpoint solo los lista. No construye ningún ``FactSet`` (no hay delta que calcular
para una lista), así que no exige que cada periodo tenga uno anterior con el que
compararse — a diferencia de ``GET /v1/hechos``, aquí el periodo más antiguo del
historial no falla.

Para ver el detalle de un periodo concreto (y para que BillSense conteste sobre él
en el chat), se usa lo que ya existe: ``GET /v1/hechos?periodo=YYYY-MM`` y
``POST /v1/explicar`` con ``periodo`` en el cuerpo. Ese periodo más antiguo del
historial sí puede dar ``422 SIN_RECIBO_PREVIO`` ahí, porque ya no queda un periodo
aún más viejo dentro de los mismos ``ciclos`` con el que compararlo — es esperado,
no un error de este endpoint.

Nivel exigido: **LOA2**, igual que ``/v1/hechos``: un resumen de recibo sigue siendo
un listado de importes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from apps.api.deps import AuditoriaDep, RepositorioDep, nuevo_trace_id
from apps.api.routers.hechos import cargar_datos_cuenta
from apps.api.security import Identidad, cuenta_autorizada, requiere_nivel
from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento
from packages.core_domain.esquemas.recibo import Recibo
from packages.core_domain.esquemas.respuesta import ResumenRecibo

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["historial"])

#: Cuántos recibos anteriores como máximo se listan, aparte del actual.
MAXIMO_PREVIOS = 5


def _resumen_de(recibo: Recibo, *, es_actual: bool) -> ResumenRecibo:
    return ResumenRecibo(
        periodo=recibo.periodo,
        total_cent=recibo.total_cent,
        fecha_emision=recibo.fecha_emision.isoformat(),
        fecha_vencimiento=recibo.fecha_vencimiento.isoformat(),
        modalidad_renta=str(recibo.modalidad_renta),
        deuda_anterior_cent=recibo.deuda_anterior_cent,
        estado_servicio=str(recibo.estado_servicio),
        es_actual=es_actual,
    )


@router.get(
    "/historial",
    summary="Hasta 5 recibos anteriores de la cuenta, resumidos",
    response_model=list[ResumenRecibo],
)
def obtener_historial(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    repositorio: RepositorioDep,
    auditoria: AuditoriaDep,
) -> list[ResumenRecibo]:
    """Lista el periodo actual y hasta 5 anteriores, del más reciente al más antiguo."""
    cuenta = cuenta_autorizada(identidad, None)
    trace_id = nuevo_trace_id()
    contexto = identidad.contexto_auditoria()

    auditoria.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "GET /v1/historial",
            "periodo": None,
            "canal": str(identidad.canal or "APP"),
            "nivel": str(identidad.acr),
            "verbosidad": "NO_APLICA",
            "utterance": "",
        },
        **contexto,
    )

    datos = cargar_datos_cuenta(repositorio, cuenta, None, trace_id=trace_id)
    previos = sorted(datos.previos, key=lambda recibo: recibo.periodo, reverse=True)[:MAXIMO_PREVIOS]
    resumen = [_resumen_de(datos.recibo, es_actual=True)]
    resumen.extend(_resumen_de(recibo, es_actual=False) for recibo in previos)

    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {"bloques": 0, "acciones": 0, "modo": "NO_APLICA", "derivada": False, "periodos": len(resumen)},
        **contexto,
    )
    auditoria.cerrar_turno(trace_id, cuenta_ref=cuenta)
    return resumen
