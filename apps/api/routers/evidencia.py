"""``GET /v1/evidencia/{explicacion_id}`` — de dónde salió cada cosa que se dijo.

Devuelve la lista de items ``{tipo, ref_id, snippet}`` que respaldan una explicación ya
entregada. Es la contrapartida visible de la exigencia *"respuestas limitadas
estrictamente a la base de datos de facturación provista"*: cualquiera puede pedir la
evidencia de una respuesta y comprobar que cada afirmación tiene detrás una línea del
recibo, una orden del CRM, un tramo del ciclo o una entrada del catálogo.

Dos orígenes se mezclan, y se distinguen por ``tipo``:

* **hechos** (``linea``, ``mov``, ``tramo``, ``factset``): salen del FactSet sellado.
  Llevan cifras porque las cifras son suyas.
* **corpus** (``cat``, ``faq``, ``casuistica``): salen del retriever y vienen ya
  saneados, sin una sola cifra. Un importe de una FAQ genérica no es un importe de este
  cliente y no puede presentarse como evidencia de su recibo.

Nivel exigido: **LOA2**, y el ``explicacion_id`` tiene que pertenecer a la cuenta del
token.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import MemoriaConversaciones, MemoriaDep, ReglasDep
from apps.api.errores import ErrorApi, no_encontrado
from apps.api.security import Identidad, requiere_nivel
from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import NivelAseguramiento, TipoEvidencia
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import ItemEvidencia
from packages.core_domain.reglas import ConfiguracionReglas

__all__ = ["RespuestaEvidencia", "evidencia_de_factset", "router"]

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["evidencia"])


class RespuestaEvidencia(BaseModel):
    """Cuerpo de ``GET /v1/evidencia/{explicacion_id}``."""

    model_config = ConfigDict(extra="forbid")

    explicacion_id: str
    trace_id: str
    periodo: str
    factset_sha256: str
    items: list[ItemEvidencia] = Field(default_factory=list)
    total: int = 0
    corpus_saneado: bool = Field(
        default=True, description="Los items de corpus vienen sin cifras, por construcción"
    )


def evidencia_de_factset(factset: FactSet, reglas: ConfiguracionReglas) -> list[ItemEvidencia]:
    """Items de evidencia derivados del propio FactSet.

    Una entrada por línea con variación (con su importe anterior, el actual y la
    diferencia), una por movimiento del ciclo que se usó para atribuir, y una por tramo
    cuando la línea trae la tabla que explica el prorrateo —*"la tabla de tramos ES la
    explicación"*—.
    """
    items: list[ItemEvidencia] = [
        ItemEvidencia(
            tipo=str(TipoEvidencia.FACTSET),
            ref_id=str(factset.factset_id),
            snippet=(
                f"Recibo {factset.periodo_previo} {formatear_soles(factset.total_previo_cent)} → "
                f"recibo {factset.periodo_actual} "
                f"{formatear_soles(factset.total_actual_cent)}: "
                f"{formatear_soles(factset.delta_total_cent)}. Residual de conciliación: "
                f"{factset.invariante.residual_cent} céntimos."
            ),
            fact_id="factset:delta_total_cent",
        )
    ]
    for linea in factset.lineas:
        if not linea.se_explica:
            continue
        items.append(
            ItemEvidencia(
                tipo=str(TipoEvidencia.LINEA),
                ref_id=linea.concepto_id,
                snippet=(
                    f"{linea.nombre_comercial}: "
                    f"{formatear_soles(linea.monto_previo_cent)} → "
                    f"{formatear_soles(linea.monto_actual_cent)} "
                    f"({formatear_soles(linea.delta_cent)}, {linea.clase}). "
                    f"Confianza de atribución {linea.confianza:.2f}."
                ),
                fact_id=f"linea:{linea.concepto_id}.delta_cent",
            )
        )
        if linea.movimiento_id is not None:
            items.append(
                ItemEvidencia(
                    tipo=str(TipoEvidencia.MOVIMIENTO),
                    ref_id=str(linea.movimiento_id),
                    snippet=(
                        f"Orden {linea.movimiento_id} del historial de Amdocs, "
                        f"tipo {linea.causa}, atribuida a {linea.concepto_id}."
                    ),
                    fact_id=f"linea:{linea.concepto_id}.movimiento_id",
                )
            )
        for indice, tramo in enumerate(linea.tramos or []):
            items.append(
                ItemEvidencia(
                    tipo=str(TipoEvidencia.TRAMO),
                    ref_id=f"{linea.concepto_id}#{indice}",
                    snippet=(
                        f"{tramo.etiqueta}: {tramo.dias} días a "
                        f"{formatear_soles(tramo.tarifa_mensual_cent)} mensuales "
                        f"⇒ {formatear_soles(tramo.monto_prorrateado_cent)}"
                        + ("" if tramo.facturable else " (no se cobró: servicio suspendido)")
                    ),
                    fact_id=f"linea:{linea.concepto_id}.tramos",
                )
            )
        concepto = reglas.concepto(linea.concepto_id)
        if concepto is not None:
            items.append(
                ItemEvidencia(
                    tipo=str(TipoEvidencia.CATALOGO),
                    ref_id=concepto.concepto_id,
                    snippet=concepto.definicion_cliente,
                    fact_id=f"cat:{concepto.concepto_id}",
                )
            )
    return items


def _rehidratar(memoria: MemoriaConversaciones, explicacion_id: str) -> Any | None:
    """Segunda oportunidad: buscar la explicación en el *checkpointer* del grafo.

    ``MemoriaConversaciones`` vive en RAM, así que un reinicio del proceso la vacía y
    esta consulta respondía ``404`` aunque el turno entero siguiera en disco. Con
    ``ORQUESTADOR=grafo`` el estado del turno —``FactSet`` incluido— está persistido, y
    de ahí se puede reconstruir el registro.

    Es **estrictamente aditivo**: solo se llama cuando la memoria viva ya falló, no
    lanza nunca y no relaja ninguna comprobación —el ``cuenta_ref`` rehidratado pasa por
    el mismo control de propiedad que el vivo—. Si no encuentra nada, se responde el
    mismo ``404`` de siempre.

    La guarda sobre ``sys.modules`` no es una micro-optimización: si este proceso nunca
    cargó la capa de orquestación —``ORQUESTADOR=directo``— entonces tampoco escribió
    ningún *checkpoint*, así que no hay nada que buscar. Y, sobre todo, ese modo existe
    para funcionar **sin LangGraph**: importarlo por la puerta de atrás en un 404 sería
    romper justo la propiedad que hace del respaldo un respaldo.
    """
    if "packages.orquestacion.checkpointer" not in sys.modules:
        return None
    try:
        from packages.orquestacion.rehidratacion import rehidratar_explicacion

        return rehidratar_explicacion(memoria, explicacion_id)
    # La capa de orquestación es opcional: sin ella, el 404 es la respuesta correcta.
    except Exception as error:
        _LOG.debug("sin rehidratación para %s: %s", explicacion_id, error)
        return None


@router.get(
    "/evidencia/{explicacion_id}",
    summary="Evidencia que respalda una explicación entregada",
    response_model=RespuestaEvidencia,
    responses={404: {"description": "EXPLICACION_NO_ENCONTRADA"}},
)
def obtener_evidencia(
    explicacion_id: str,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    memoria: MemoriaDep,
    reglas: ReglasDep,
    solo: Annotated[
        str | None, Query(description="Filtra por tipo: linea, mov, tramo, cat, faq, casuistica")
    ] = None,
) -> RespuestaEvidencia:
    """Devuelve la evidencia del turno identificado por ``explicacion_id``.

    El identificador es el ``trace_id`` del turno, que la respuesta de ``/v1/explicar``
    publica en ``telemetria.explicacion_id`` y en la cabecera ``X-Trace-Id``.
    """
    registro = memoria.explicacion(explicacion_id) or _rehidratar(memoria, explicacion_id)
    if registro is None:
        raise no_encontrado(
            "EXPLICACION_NO_ENCONTRADA",
            f"no hay evidencia viva para la explicación {explicacion_id}; "
            "vuelva a pedir la explicación para regenerarla",
            explicacion_id=explicacion_id,
        )
    if registro.cuenta_ref != identidad.cuenta_ref:
        raise ErrorApi(
            403,
            "CUENTA_NO_AUTORIZADA",
            "esa explicación pertenece a otra cuenta",
            datos={"explicacion_id": explicacion_id},
        )

    items = [*evidencia_de_factset(registro.factset, reglas), *registro.evidencia]
    if solo:
        tipos = {parte.strip() for parte in solo.split(",") if parte.strip()}
        items = [item for item in items if item.tipo in tipos]
    return RespuestaEvidencia(
        explicacion_id=registro.explicacion_id,
        trace_id=registro.trace_id,
        periodo=registro.periodo,
        factset_sha256=registro.factset.sha256,
        items=items,
        total=len(items),
    )
