"""``GET /salud`` — liveness y preparación del servicio.

``/salud`` es una sonda barata: responde sin tocar BrainyBill, Amdocs ni el índice
vectorial, para que el orquestador no reinicie contenedores porque un sistema externo
tardó. ``/salud/preparacion`` sí comprueba las dependencias y es la que debe mirar el
balanceador antes de mandar tráfico.

Ninguno de los dos exige autenticación: no devuelven un solo dato de cliente.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Response, status

from apps.api.acl import ErrorSistemaExterno
from apps.api.deps import AjustesDep, AuditoriaDep, ProveedorDep, RecuperadorDep

__all__ = ["router"]

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["salud"])

#: Marca de arranque del proceso, para reportar el tiempo en pie.
_ARRANQUE = time.time()


@router.get("/salud", summary="Liveness del servicio")
def salud(ajustes: AjustesDep) -> dict[str, Any]:
    """Indica que el proceso está vivo y con qué configuración arrancó.

    No consulta sistemas externos a propósito: una sonda de liveness que depende de
    terceros convierte la caída de un tercero en un reinicio en cascada.
    """
    return {
        "estado": "ok",
        "servicio": "recibo-claro-api",
        "entorno": ajustes.entorno,
        "rules_version": ajustes.rules_version,
        "llm_mode": ajustes.llm_mode,
        "verificador_estricto": ajustes.verificador_estricto,
        "en_pie_s": round(time.time() - _ARRANQUE, 1),
    }


@router.get("/salud/preparacion", summary="Readiness: dependencias comprobadas")
def preparacion(
    respuesta: Response,
    ajustes: AjustesDep,
    recuperador: RecuperadorDep,
    proveedor: ProveedorDep,
    auditoria: AuditoriaDep,
) -> dict[str, Any]:
    """Comprueba corpus RAG, proveedor generativo y bitácora de auditoría.

    Devuelve ``503`` solo si falta algo **sin lo que no se puede responder**. Que no
    haya proveedor LLM o que el RAG esté degradado no impide explicar: la plantilla
    determinística funciona sin ninguno de los dos y sus cifras salen del FactSet.
    """
    detalle: dict[str, Any] = {
        "almacenamiento": ajustes.almacenamiento(),
        "rag": recuperador.estado() if recuperador is not None else {"disponible": False},
        "llm": {
            "modo": ajustes.llm_mode,
            "proveedor": getattr(proveedor, "nombre", "plantilla-determinista"),
            "degradado": proveedor is None,
        },
    }
    try:
        valida, indice_roto = auditoria.verificar_cadena()
        detalle["auditoria"] = {
            "ruta": str(auditoria.ruta),
            "cadena_valida": valida,
            "indice_roto": indice_roto,
        }
    except (OSError, ValueError) as error:
        detalle["auditoria"] = {"error": str(error)}

    critico = detalle["auditoria"].get("cadena_valida") is False
    detalle["estado"] = "degradado" if (critico or proveedor is None) else "ok"
    detalle["listo"] = not critico
    if critico:
        respuesta.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return detalle


@router.get("/salud/sistemas", summary="Conectividad con BrainyBill y Amdocs")
def sistemas(respuesta: Response, ajustes: AjustesDep) -> dict[str, Any]:
    """Diagnóstico del ACL: qué transporte hay configurado y si responde.

    Es la comprobación que se hace antes de una demo en vivo: dice de un vistazo si se
    está leyendo el dataset del disco o hablando con los mocks.
    """
    from apps.api.deps import obtener_repositorio

    repositorio = obtener_repositorio()
    estado: dict[str, Any] = {
        "brainybill": {
            "destino": ajustes.brainybill_base_url or f"archivo:{ajustes.ruta_datos}",
            "transporte": type(repositorio.brainybill.transporte).__name__,
        },
        "amdocs": {
            "destino": ajustes.amdocs_base_url or f"archivo:{ajustes.ruta_datos}",
            "transporte": type(repositorio.amdocs.transporte).__name__,
        },
        "ciclos": repositorio.ciclos,
    }
    sondas = (
        ("brainybill", repositorio.brainybill.transporte, "/bills/__sonda__", {"cycles": 1}),
        ("amdocs", repositorio.amdocs.transporte, "/orders/__sonda__", {"formato": "amdocs"}),
    )
    for clave, transporte, ruta, params in sondas:
        try:
            transporte.obtener(ruta, params=params)
            estado[clave]["alcanzable"] = True
        except ErrorSistemaExterno as error:
            # Una cuenta inexistente es la respuesta esperada de la sonda: significa que
            # el sistema contesta. Solo un fallo de transporte indica indisponibilidad.
            alcanzable = "no existe" in error.detalle
            estado[clave]["alcanzable"] = alcanzable
            if not alcanzable:
                estado[clave]["detalle"] = error.detalle
                respuesta.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return estado
