"""Mock de **BrainyBill** — el sistema que expone el recibo actual y los cinco previos.

Reproduce el contrato que describe la ficha del desafío: *"BrainyBill expone la
información de la factura actual y de los CINCO recibos previos, pero hoy NO explica el
recibo de forma inteligente ni orientada al cliente"*. Este servicio hace exactamente
eso —entrega datos, no explicaciones— para que el ACL de la API se ejercite contra un
servicio HTTP real desde el primer día y el salto al BrainyBill de verdad sea cambiar
``BRAINYBILL_BASE_URL``.

Contrato::

    GET /bills/{cuenta_id}?cycles=6
    200 {"cuenta_id", "modalidad_renta", "segmento", "dia_ciclo", "moneda",
         "beneficios_vigentes": [...], "ciclos": 6,
         "recibos": [{"header": {...}, "lines": [...]}, ...]}   # más reciente primero
    404 {"codigo": "CUENTA_NO_ENCONTRADA", ...}

Los datos salen de ``data/sintetico/bills/*.json`` (o de ``apps/mocks/brainybill/datos``
si se prefiere aislarlos). Los importes viajan en **céntimos enteros** con sufijo
``_cent``; si el BrainyBill real entregase soles decimales, el interruptor que lo
absorbe está en ``apps/api/acl.py`` (``IMPORTES_EN_CENTIMOS``), no aquí.

Arranque::

    uvicorn apps.mocks.brainybill.servidor:app --port 8801
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, status

__all__ = ["app", "raiz_datos"]

_LOG = logging.getLogger(__name__)

#: Variable con la que se apunta el mock a otro directorio de datos.
VAR_DATOS = "BRAINYBILL_DATOS"

#: Recibos que expone BrainyBill: el actual y los cinco previos.
CICLOS_POR_DEFECTO = 6


def _raiz_proyecto() -> Path:
    """Raíz del repositorio (``apps/mocks/brainybill/servidor.py`` → tres arriba)."""
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def raiz_datos() -> Path:
    """Directorio del que se sirven los recibos.

    Prioridad: ``BRAINYBILL_DATOS`` → ``apps/mocks/brainybill/datos`` si tiene ficheros
    → ``data/sintetico``. Así se pueden congelar unos datos de demo junto al mock sin
    tocar el dataset generado.
    """
    declarado = os.getenv(VAR_DATOS, "").strip()
    if declarado:
        return Path(declarado)
    propio = Path(__file__).resolve().parent / "datos"
    if propio.is_dir() and any(propio.glob("**/*.json")):
        return propio
    return _raiz_proyecto() / "data" / "sintetico"


def _directorio_bills() -> Path:
    """Carpeta con un JSON por cuenta."""
    raiz = raiz_datos()
    candidato = raiz / "bills"
    return candidato if candidato.is_dir() else raiz


app = FastAPI(
    title="Mock BrainyBill",
    summary="Recibo actual y cinco previos, tal como los expone BrainyBill.",
    description=__doc__,
    version="1.0.0",
)


@app.get("/salud", summary="Liveness del mock")
def salud() -> dict[str, Any]:
    """Indica desde dónde se están sirviendo los recibos y cuántas cuentas hay."""
    directorio = _directorio_bills()
    return {
        "estado": "ok",
        "servicio": "mock-brainybill",
        "datos": str(directorio),
        "cuentas": len(list(directorio.glob("*.json"))) if directorio.is_dir() else 0,
    }


@app.get("/bills", summary="Cuentas disponibles")
def listar_cuentas(
    limite: int = Query(default=50, ge=1, le=1000),
) -> dict[str, Any]:
    """Lista los identificadores de cuenta con recibos cargados."""
    directorio = _directorio_bills()
    if not directorio.is_dir():
        return {"total": 0, "cuentas": []}
    cuentas = sorted(ruta.stem for ruta in directorio.glob("*.json"))
    return {"total": len(cuentas), "cuentas": cuentas[:limite]}


@app.get(
    "/bills/{cuenta_id}",
    summary="Recibo actual y previos de una cuenta",
    responses={404: {"description": "CUENTA_NO_ENCONTRADA"}},
)
def obtener_recibos(
    cuenta_id: str,
    cycles: int = Query(
        default=CICLOS_POR_DEFECTO,
        ge=1,
        le=24,
        description="Cuántos recibos devolver, empezando por el más reciente",
    ),
) -> dict[str, Any]:
    """Devuelve el documento de la cuenta con sus últimos ``cycles`` recibos.

    Los recibos van **del más reciente al más antiguo**, que es el orden en que los
    expone el sistema real. El ACL de la API vuelve a ordenarlos igualmente: no depende
    de este detalle, pero se respeta para que el mock no mienta sobre el contrato.
    """
    ruta = _directorio_bills() / f"{cuenta_id}.json"
    if not ruta.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "codigo": "CUENTA_NO_ENCONTRADA",
                "detalle": f"BrainyBill no tiene recibos de la cuenta {cuenta_id}",
                "datos": {"cuenta_id": cuenta_id},
            },
        )
    try:
        documento = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _LOG.error("no se pudo leer %s: %s", ruta, error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"codigo": "DATOS_ILEGIBLES", "detalle": str(error)},
        ) from error

    recibos = documento.get("recibos", [])
    recibos = sorted(
        recibos,
        key=lambda recibo: recibo.get("header", {}).get("periodo", ""),
        reverse=True,
    )[:cycles]
    documento["recibos"] = recibos
    documento["ciclos"] = len(recibos)
    documento["periodos"] = [recibo.get("header", {}).get("periodo") for recibo in recibos]
    return documento


@app.get(
    "/bills/{cuenta_id}/{periodo}",
    summary="Un recibo concreto de la cuenta",
    responses={404: {"description": "CUENTA_NO_ENCONTRADA o PERIODO_NO_ENCONTRADO"}},
)
def obtener_recibo(cuenta_id: str, periodo: str) -> dict[str, Any]:
    """Devuelve un único recibo por periodo ``YYYY-MM``."""
    documento = obtener_recibos(cuenta_id, cycles=24)
    for recibo in documento["recibos"]:
        if recibo.get("header", {}).get("periodo") == periodo:
            return recibo
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "codigo": "PERIODO_NO_ENCONTRADO",
            "detalle": f"la cuenta {cuenta_id} no tiene recibo del periodo {periodo}",
            "datos": {"cuenta_id": cuenta_id, "periodo": periodo},
        },
    )
