"""Mock de **Amdocs (CRM)** — el historial de órdenes de una cuenta.

Es la fuente de la *"trazabilidad de pedidos, suspensiones morosas, altas/bajas, cambios
de plan, equipos financiados, paquetes y promociones"* que enumera la ficha, y sin la
cual el motor no puede atribuir una variación a su causa.

Contrato::

    GET /orders/{cuenta_id}?formato=amdocs      (por defecto)
    200 {"cuenta_id", "formato": "amdocs", "total": 2,
         "orders": [{"ORDER_ID","ACCOUNT_ID","ORDER_TYPE","ORDER_DATE",
                     "SERVICE_ID","CHANNEL","DETAIL_JSON"}]}

    GET /orders/{cuenta_id}?formato=canonico
    200 {"cuenta_id", "formato": "canonico", "total": 2,
         "movimientos": [MovementEvent, ...]}

**Por qué dos formatos.** El nativo (``amdocs``) es el que devuelve el sistema real:
columnas en mayúsculas y el detalle como cadena JSON. Es el que consume el ACL, de modo
que la traducción a ``MovementEvent`` se ejercita en cada llamada y no aparece por
sorpresa el día que llegue el export de verdad. El canónico existe para poder inspeccionar
el resultado de esa traducción sin levantar la API —útil en la demo y en depuración—, y
lo produce el mismo ``movistar_map`` que usa el ACL: si los dos formatos difirieran,
sería un fallo del mapa, no de dos implementaciones distintas.

Arranque::

    uvicorn apps.mocks.amdocs.servidor:app --port 8802
"""

from __future__ import annotations

import csv
import logging
import os
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, status

from packages.datagen.mapping.movistar_map import (
    COLUMNAS_ORDENES,
    a_movimiento,
    validar_ordenes,
)

__all__ = ["app", "ruta_ordenes"]

_LOG = logging.getLogger(__name__)

#: Variable con la que se apunta el mock a otro CSV de órdenes.
VAR_DATOS = "AMDOCS_DATOS"


def _raiz_proyecto() -> Path:
    """Raíz del repositorio (``apps/mocks/amdocs/servidor.py`` → tres arriba)."""
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def ruta_ordenes() -> Path:
    """Fichero CSV del que se sirven las órdenes.

    Prioridad: ``AMDOCS_DATOS`` → ``apps/mocks/amdocs/datos/ordenes.csv`` →
    ``data/sintetico/ordenes.csv``.
    """
    declarado = os.getenv(VAR_DATOS, "").strip()
    if declarado:
        ruta = Path(declarado)
        return ruta if ruta.suffix else ruta / "ordenes.csv"
    propio = Path(__file__).resolve().parent / "datos" / "ordenes.csv"
    if propio.is_file():
        return propio
    return _raiz_proyecto() / "data" / "sintetico" / "ordenes.csv"


@lru_cache(maxsize=1)
def _indice() -> dict[str, list[dict[str, str]]]:
    """Índice ``cuenta_id -> filas`` construido una sola vez por proceso.

    El CSV del dataset son unos cientos de filas; en el sistema real esto sería una
    consulta al CRM. Cachearlo aquí evita releer el fichero en cada petición sin cambiar
    el contrato.
    """
    ruta = ruta_ordenes()
    if not ruta.is_file():
        _LOG.error("no existe el fichero de órdenes: %s", ruta)
        return {}
    agrupadas: dict[str, list[dict[str, str]]] = defaultdict(list)
    with ruta.open(encoding="utf-8", newline="") as fichero:
        for fila in csv.DictReader(fichero):
            agrupadas[str(fila.get("ACCOUNT_ID", "")).strip()].append(dict(fila))
    for filas in agrupadas.values():
        filas.sort(
            key=lambda fila: (str(fila.get("ORDER_DATE", "")), str(fila.get("ORDER_ID", "")))
        )
    _LOG.info("cargadas %s cuentas con órdenes desde %s", len(agrupadas), ruta)
    return dict(agrupadas)


app = FastAPI(
    title="Mock Amdocs (CRM)",
    summary="Historial de órdenes: cambios de plan, suspensiones, altas y financiaciones.",
    description=__doc__,
    version="1.0.0",
)


@app.get("/salud", summary="Liveness del mock")
def salud() -> dict[str, Any]:
    """Indica de qué fichero se sirven las órdenes y cuántas cuentas tiene."""
    indice = _indice()
    return {
        "estado": "ok",
        "servicio": "mock-amdocs",
        "datos": str(ruta_ordenes()),
        "cuentas": len(indice),
        "ordenes": sum(len(filas) for filas in indice.values()),
        "columnas": list(COLUMNAS_ORDENES),
    }


@app.get("/orders/{cuenta_id}", summary="Historial de órdenes de una cuenta")
def obtener_ordenes(
    cuenta_id: str,
    formato: Literal["amdocs", "canonico"] = Query(
        default="amdocs",
        description="'amdocs' devuelve las columnas nativas; 'canonico', MovementEvent[]",
    ),
    desde: str | None = Query(default=None, description="Filtra por fecha ISO (inclusive)"),
    hasta: str | None = Query(default=None, description="Filtra por fecha ISO (exclusive)"),
) -> dict[str, Any]:
    """Devuelve las órdenes de la cuenta, ordenadas cronológicamente.

    Una cuenta sin órdenes **no es un 404**: es un cliente estable, y el motor debe poder
    concluir "su recibo no varió" sin tratarlo como un fallo de integración.
    """
    filas = list(_indice().get(cuenta_id, ()))
    if desde:
        filas = [fila for fila in filas if str(fila.get("ORDER_DATE", "")) >= desde]
    if hasta:
        filas = [fila for fila in filas if str(fila.get("ORDER_DATE", "")) < hasta]

    if formato == "canonico":
        movimientos = []
        for fila in filas:
            try:
                movimientos.append(a_movimiento(fila).model_dump(mode="json"))
            except (ValueError, KeyError, TypeError) as error:
                _LOG.warning("orden %s no traducible: %s", fila.get("ORDER_ID"), error)
        return {
            "cuenta_id": cuenta_id,
            "formato": "canonico",
            "total": len(movimientos),
            "movimientos": movimientos,
        }

    return {
        "cuenta_id": cuenta_id,
        "formato": "amdocs",
        "total": len(filas),
        "orders": filas,
    }


@app.get("/orders/{cuenta_id}/validacion", summary="Control de calidad del ACL")
def validar_cuenta(cuenta_id: str) -> dict[str, Any]:
    """Pasa las órdenes de la cuenta por ``validar_ordenes`` del mapa de Movistar.

    Lista vacía de errores significa que el export es apto para ingesta. Es el mismo
    control que se le aplicaría al fichero real de Amdocs antes de cargarlo.
    """
    filas = _indice().get(cuenta_id)
    if filas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "codigo": "CUENTA_NO_ENCONTRADA",
                "detalle": f"no hay órdenes para la cuenta {cuenta_id}",
            },
        )
    errores = validar_ordenes(filas)
    return {"cuenta_id": cuenta_id, "apto": not errores, "errores": errores, "filas": len(filas)}
