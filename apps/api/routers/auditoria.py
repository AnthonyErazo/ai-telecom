"""``GET /v1/auditoria?trace_id=...`` — la bitácora del turno y su integridad.

La métrica técnica oficial de la ficha es *"Tasa de Alucinación: cero invenciones
financieras **comprobables mediante logs de la terminal**"*. Este endpoint expone
exactamente esos logs, ya encadenados por hash:

* ``resumen`` — una línea por etapa con el contador
  ``AFIRMACIONES NUMÉRICAS n · ANCLADAS n · NO ANCLADAS n``.
* ``cadena_valida`` — resultado de recorrer la cadena
  ``hash_n = SHA256(hash_{n-1} || json_canonico(evento_n))``. Si alguien retocó un
  evento pasado, aquí sale ``False`` y el índice exacto donde se rompió.
* ``terminal`` — el mismo resumen que se imprime en la consola del servidor, para
  pegarlo en la demo sin acceso al contenedor.

Nivel exigido: **LOA2**. Un turno solo lo puede leer la cuenta a la que pertenece; un
asesor lo lee a través de su ``acting_on_behalf_of``, que también quedó registrado.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from apps.api.deps import AuditoriaDep
from apps.api.errores import ErrorApi, no_encontrado
from apps.api.security import Identidad, requiere_nivel
from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento
from packages.governance.auditoria import ResumenTurno, formatear_para_terminal

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["auditoria"])


@router.get(
    "/auditoria",
    summary="Eventos auditados de un turno y validez de la cadena de hashes",
    responses={404: {"description": "TRAZA_NO_ENCONTRADA"}},
)
def obtener_auditoria(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    auditoria: AuditoriaDep,
    trace_id: Annotated[str, Query(description="Traza devuelta por /v1/explicar")],
    incluir_eventos: Annotated[bool, Query(description="Adjunta el detalle completo")] = True,
    etapas: Annotated[
        str | None,
        Query(description="Filtra por etapas separadas por coma, p. ej. VERIFY,RESPONSE"),
    ] = None,
) -> dict[str, Any]:
    """Devuelve los eventos del turno, su resumen y si la cadena está íntegra."""
    filtro: tuple[EtapaAuditoria, ...] | None = None
    if etapas:
        try:
            filtro = tuple(
                EtapaAuditoria(parte.strip().upper())
                for parte in etapas.split(",")
                if parte.strip()
            )
        except ValueError as error:
            raise ErrorApi(
                422, "ETAPA_DESCONOCIDA", f"etapa de auditoría no reconocida: {error}"
            ) from error

    eventos = auditoria.leer(trace_id, etapas=filtro)
    if not eventos:
        raise no_encontrado(
            "TRAZA_NO_ENCONTRADA",
            f"no hay eventos auditados con la traza {trace_id}",
            trace_id=trace_id,
        )

    cuentas = {evento.cuenta_ref for evento in eventos if evento.cuenta_ref}
    if cuentas and identidad.cuenta_ref not in cuentas:
        raise ErrorApi(
            403,
            "CUENTA_NO_AUTORIZADA",
            "esa traza pertenece a otra cuenta",
            datos={"trace_id": trace_id},
        )

    resumen: ResumenTurno = auditoria.resumen(trace_id)
    cadena_valida, indice_roto = auditoria.verificar_cadena()
    cuerpo: dict[str, Any] = {
        "trace_id": trace_id,
        "cadena_valida": cadena_valida,
        "indice_roto": indice_roto,
        "resumen": resumen.model_dump(mode="json"),
        "terminal": formatear_para_terminal(
            auditoria.leer(trace_id), trace_id, color=False, banner=True
        ).splitlines(),
    }
    if incluir_eventos:
        cuerpo["eventos"] = [evento.model_dump(mode="json") for evento in eventos]
        cuerpo["total_eventos"] = len(eventos)
    return cuerpo


@router.get("/auditoria/cadena", summary="Verifica la cadena de hashes completa")
def verificar_cadena_completa(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    auditoria: AuditoriaDep,
) -> dict[str, Any]:
    """Recorre toda la bitácora y dice si algún evento fue alterado.

    No devuelve contenido de ningún turno: solo el veredicto y, si falla, el índice del
    primer evento cuyo hash no cuadra.
    """
    valida, indice_roto = auditoria.verificar_cadena()
    return {
        "ruta": str(auditoria.ruta),
        "cadena_valida": valida,
        "indice_roto": indice_roto,
        "eventos": auditoria.indice_siguiente,
        "hash_ultimo": auditoria.hash_ultimo,
    }
