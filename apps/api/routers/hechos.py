"""``GET /v1/hechos`` — el ``FactSet`` conciliado de un periodo.

Es la superficie de la que sale toda cifra del sistema: el LLM no calcula, el
verificador ancla contra este documento y la auditoría guarda su ``sha256``. Exponerlo
tal cual permite auditar la explicación sin ejecutar el modelo.

Nivel exigido: **LOA2**. Un ``FactSet`` es una lista de importes; no hay forma de
enseñárselo a ``LOA1``, cuyo contrato es "existencia y dirección del cambio, ningún
monto".

Código de error propio: **409 ``INVARIANTE_FALLIDO``** cuando ``|residual_cent| > 1``.
No se devuelve un FactSet "aproximado": si la suma de las variaciones por concepto no
reproduce la diferencia entre totales, el recibo no se explica, se deriva. El cuerpo del
409 lleva el residual exacto para que el asesor arranque con el dato en la mano.

Este módulo publica además :func:`construir_hechos`, que reutilizan ``/v1/explicar`` y
``/v1/derivacion``: la construcción de hechos ocurre en un único sitio.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from apps.api.acl import (
    CuentaNoEncontradaExterna,
    DatosCuenta,
    ErrorSistemaExterno,
    RepositorioCuentas,
)
from apps.api.deps import AuditoriaDep, ReglasDep, RepositorioDep, nuevo_trace_id
from apps.api.errores import (
    ErrorApi,
    cuenta_no_encontrada,
    invariante_fallido,
    no_encontrado,
    sistema_externo_caido,
)
from apps.api.security import Identidad, cuenta_autorizada, requiere_nivel
from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.reglas import ConfiguracionReglas
from packages.facts_engine.motor import SinReciboPrevio, construir_factset, resumen_de_conciliacion

__all__ = ["cargar_datos_cuenta", "construir_hechos", "payload_facts_built", "router"]

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["hechos"])


# --------------------------------------------------------------------------- #
# Construcción compartida
# --------------------------------------------------------------------------- #
def cargar_datos_cuenta(
    repositorio: RepositorioCuentas,
    cuenta_id: str,
    periodo: str | None,
    *,
    trace_id: str | None = None,
) -> DatosCuenta:
    """Carga recibos y movimientos por el ACL traduciendo sus errores a HTTP.

    Raises:
        ErrorApi: 404 si la cuenta o el periodo no existen; 503 si BrainyBill o Amdocs
            no responden.
    """
    try:
        return repositorio.cargar(cuenta_id, periodo)
    except CuentaNoEncontradaExterna as error:
        if "@" in str(error.cuenta_id):
            raise no_encontrado(
                "PERIODO_NO_ENCONTRADO",
                f"la cuenta {cuenta_id} no tiene un recibo del periodo {periodo}",
                cuenta_id=cuenta_id,
                periodo=periodo,
            ) from error
        raise cuenta_no_encontrada(cuenta_id) from error
    except ErrorSistemaExterno as error:
        _LOG.error("sistema externo caído en la traza %s: %s", trace_id, error)
        raise sistema_externo_caido(error.sistema, error.detalle) from error


def construir_hechos(
    repositorio: RepositorioCuentas,
    reglas: ConfiguracionReglas,
    cuenta_id: str,
    periodo: str | None,
    *,
    trace_id: str | None = None,
) -> tuple[FactSet, DatosCuenta]:
    """Carga la cuenta y construye su ``FactSet`` sellado.

    El motor **no lanza** si el invariante se rompe: devuelve el FactSet con
    ``invariante.ok = False`` y todos sus datos, porque quien deriva necesita esos datos.
    La decisión de qué hacer con eso la toma cada endpoint (409 aquí, derivación en
    ``/v1/explicar``).

    Raises:
        ErrorApi: 404/503 del ACL, o 422 ``SIN_RECIBO_PREVIO`` si solo hay un recibo.
    """
    datos = cargar_datos_cuenta(repositorio, cuenta_id, periodo, trace_id=trace_id)
    try:
        factset = construir_factset(
            datos.recibo,
            datos.previos,
            datos.movimientos,
            reglas,
            beneficios_vigentes=datos.beneficios or None,
        )
    except SinReciboPrevio as error:
        raise ErrorApi(
            422,
            "SIN_RECIBO_PREVIO",
            "no hay un recibo anterior con el que comparar: no hay variación que explicar todavía",
            trace_id=trace_id,
            datos={"cuenta_id": cuenta_id, "periodo": datos.periodo},
        ) from error
    except ValueError as error:
        raise ErrorApi(
            422,
            "RECIBOS_INCONSISTENTES",
            f"no se pudieron comparar los recibos de la cuenta: {error}",
            trace_id=trace_id,
        ) from error
    return factset, datos


def payload_facts_built(factset: FactSet, datos: DatosCuenta | None = None) -> dict[str, object]:
    """Payload del evento ``FACTS_BUILT`` (incluye ``residual_cent``, obligatorio).

    ``datos`` es opcional porque hay un caso en el que no existen: cuando
    ``POST /v1/derivacion`` **reutiliza** el FactSet del turno anterior en vez de
    volver a construirlo. Ese turno también tiene que dejar sus hechos en la bitácora
    —si no, el expediente que recoge el asesor no tendría desglose—, y lo único que no
    se puede decir de él es cuántos recibos había disponibles, que es un dato de la
    carga y no del recibo.
    """
    payload = dict(resumen_de_conciliacion(factset))
    payload.update(
        {
            "factset_sha256": factset.sha256,
            "total_actual_cent": factset.total_actual_cent,
            "total_previo_cent": factset.total_previo_cent,
            "periodo_actual": factset.periodo_actual,
            "periodo_previo": factset.periodo_previo,
            "lineas": len(factset.lineas),
            "movimientos_ciclo": len(factset.movimientos_ciclo),
            "recibos_disponibles": (len(datos.previos) + 1) if datos is not None else None,
            # Cabecera del recibo: lo que el asesor necesita nombrar en la primera
            # frase ("su recibo de julio vence el 12"). Va a la bitácora para que el
            # paquete del asesor no tenga que volver a abrir el recibo.
            "deuda_anterior_cent": factset.deuda_anterior_cent,
            "total_a_pagar_cent": factset.total_a_pagar_cent,
            "dias_ciclo": factset.dias_ciclo,
            "fecha_vencimiento": factset.fecha_vencimiento,
        }
    )
    return payload


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@router.get(
    "/hechos",
    summary="FactSet conciliado del periodo (409 si el recibo no cuadra)",
    response_model=FactSet,
    responses={
        409: {"description": "INVARIANTE_FALLIDO: |residual_cent| > 1, el recibo no se explica"},
        422: {"description": "SIN_RECIBO_PREVIO: no hay con qué comparar"},
    },
)
def obtener_hechos(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    repositorio: RepositorioDep,
    reglas: ReglasDep,
    auditoria: AuditoriaDep,
    cuenta_id: Annotated[
        str | None,
        Query(description="Redundante: la cuenta se deriva del token. Si difiere, 403."),
    ] = None,
    periodo: Annotated[str | None, Query(description="YYYY-MM; por defecto, el último")] = None,
) -> FactSet:
    """Devuelve el ``FactSet`` del periodo pedido.

    El ``account_ref`` sale **siempre** del token: ``cuenta_id`` solo se admite como
    confirmación explícita del cliente y, si no coincide, la petición se rechaza con
    ``403 CUENTA_NO_AUTORIZADA`` en lugar de resolverse en silencio.
    """
    cuenta = cuenta_autorizada(identidad, cuenta_id)
    trace_id = nuevo_trace_id()
    contexto = identidad.contexto_auditoria()

    auditoria.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "GET /v1/hechos",
            "periodo": periodo,
            "canal": str(identidad.canal or "APP"),
            "nivel": str(identidad.acr),
            "verbosidad": "NO_APLICA",
            "utterance": "",
        },
        **contexto,
    )

    factset, datos = construir_hechos(repositorio, reglas, cuenta, periodo, trace_id=trace_id)

    auditoria.emitir(
        EtapaAuditoria.FACTS_BUILT, trace_id, payload_facts_built(factset, datos), **contexto
    )
    auditoria.emitir(
        EtapaAuditoria.INVARIANTE,
        trace_id,
        {
            "ok": factset.invariante.ok,
            "residual_cent": factset.invariante.residual_cent,
            "suma_deltas_cent": factset.invariante.suma_deltas_cent,
            "delta_total_cent": factset.invariante.delta_total_cent,
        },
        **contexto,
    )

    if not factset.invariante.ok:
        auditoria.emitir(
            EtapaAuditoria.RESPONSE,
            trace_id,
            {
                "bloques": 0,
                "acciones": 0,
                "modo": "NO_APLICA",
                "derivada": True,
                "codigo": "INVARIANTE_FALLIDO",
            },
            **contexto,
        )
        auditoria.cerrar_turno(trace_id, cuenta_ref=cuenta)
        raise invariante_fallido(
            cuenta, factset.periodo_actual, factset.invariante.residual_cent, trace_id=trace_id
        )

    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "bloques": 0,
            "acciones": 0,
            "modo": "NO_APLICA",
            "derivada": False,
            "factset_sha256": factset.sha256,
        },
        **contexto,
    )
    auditoria.cerrar_turno(trace_id, cuenta_ref=cuenta)
    return factset
