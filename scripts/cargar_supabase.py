"""Sube a Supabase todo lo que el sistema lee.

    python scripts/cargar_supabase.py             # carga todo
    python scripts/cargar_supabase.py --verificar # solo comprueba las tablas
    python scripts/cargar_supabase.py --solo cargo_facturado

Qué sube y de dónde
-------------------
==========================================  ============================
``$DATASET_DESAFIO1/Cargos_FacturadosV2.csv``   → ``cargo_facturado``  (297 002)
``$DATASET_DESAFIO1/REGISTROS_CLIENTES_...``    → ``cliente_planta``   (20 000)
``data/vocabulario/vocabulario_peruano.json``   → ``vocabulario_peruano``
``data/externo_faqs/faqs_externas.json``        → ``faq_externa``
==========================================  ============================

Ninguno de esos ficheros está en el repositorio. El dataset vive fuera por
confidencialidad (BASES §9) y ``data/`` está en ``.gitignore``.

Cómo se crean las tablas
------------------------
Hay dos caminos y el script elige solo:

1. **Con ``SUPABASE_DB_URL`` definida** (la cadena de conexión de Postgres): el script
   aplica ``db/esquema.sql`` por sí mismo y carga a
   continuación. Un solo comando, sin pasos manuales.
2. **Sin ella**: hay que pegar esa migración una vez en el editor SQL del panel.

No es una preferencia: la clave de servicio autoriza el API REST, que **inserta filas
pero no crea tablas**. PostgREST no hace DDL y no hay endpoint alternativo — se
comprobaron ``/pg/query``, ``/rest/v1/rpc/exec``, la ruta de plataforma (404 los tres) y
la API de gestión (403: exige un token ``sbp_``, no la clave de servicio).

Idempotencia
------------
Todo va con ``Prefer: resolution=merge-duplicates`` sobre una clave única, así que
ejecutarlo dos veces deja la base igual y no duplicada. Es lo que permite recargar tras
regenerar un corpus sin vaciar antes la tabla.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]

#: Filas por petición. 297 002 cargos en lotes de 200 serían 1 485 llamadas; con 2 000
#: son 149 y el cuerpo ronda 1 MB, que PostgREST admite sin problema.
TAMANO_LOTE = 2000

#: Ficheros del dataset, tal y como se entregaron.
FICHERO_CARGOS = "Cargos_FacturadosV2.csv"
FICHERO_CLIENTES = "REGISTROS_CLIENTES_20MIL.csv"


def _cargar_env() -> None:
    """Lee ``.env`` sin depender de pydantic-settings ni exportar nada al shell."""
    ruta = RAIZ / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        if "=" in linea and not linea.strip().startswith("#"):
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip())


def _credenciales() -> tuple[str, str]:
    """URL y clave **de servicio**: con RLS activo y sin políticas, la publicable no ve nada."""
    _cargar_env()
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    clave = os.getenv("SUPABASE_SECRET_KEY") or ""
    if not url or not clave:
        raise SystemExit("faltan SUPABASE_URL y/o SUPABASE_SECRET_KEY en el entorno o en .env")
    return url, clave


def _peticion(
    url: str,
    clave: str,
    ruta: str,
    *,
    metodo: str = "GET",
    cuerpo: Any = None,
    extra: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Una llamada al API REST. Devuelve ``(estado, cuerpo)`` sin lanzar en 4xx."""
    cabeceras = {
        "apikey": clave,
        "Authorization": f"Bearer {clave}",
        "Content-Type": "application/json",
    }
    cabeceras.update(extra or {})
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    peticion = urllib.request.Request(
        f"{url}/rest/v1/{ruta}", data=datos, headers=cabeceras, method=metodo
    )
    try:
        with urllib.request.urlopen(peticion, timeout=180) as respuesta:
            return respuesta.status, respuesta.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]


def _ruta_dataset() -> Path | None:
    """Carpeta del dataset, de ``DATASET_DESAFIO1``. ``None`` si no está definida."""
    bruto = (os.getenv("DATASET_DESAFIO1") or "").strip()
    if not bruto:
        return None
    carpeta = Path(bruto)
    return carpeta if carpeta.is_dir() else None


def _leer_csv(fichero: Path) -> Iterator[dict[str, str]]:
    """Recorre un CSV del dataset saneando lo que el fichero real exige.

    Dos cosas que no son opcionales: el separador es ``;`` y hay cabeceras con **espacio
    final** (``"FECHA-VENCIMIENTO "``). Sin recortarlas, el campo se pierde en silencio.
    """
    with fichero.open(encoding="utf-8", errors="replace", newline="") as fh:
        lector = csv.DictReader(fh, delimiter=";")
        if lector.fieldnames:
            lector.fieldnames = [c.strip() for c in lector.fieldnames]
        for fila in lector:
            yield {k: (v or "").strip() for k, v in fila.items() if k}


def _numero(valor: str) -> float | None:
    """Importe a float, o ``None`` si viene vacío o ilegible."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _entero(valor: str) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _filas_cargos(carpeta: Path) -> Iterator[dict[str, Any]]:
    for f in _leer_csv(carpeta / FICHERO_CARGOS):
        yield {
            "financial_account_key": f.get("FINANCIAL_ACCOUNT_KEY", ""),
            "customer_key": f.get("CUSTOMER_KEY", ""),
            "legal_invoice_number": f.get("LEGAL_INVOICE_NUMBER", ""),
            "billing_cycle_key": _entero(f.get("BILLING_CYCLE_KEY", "")),
            "subscriber_key": f.get("SUBSCRIBER_KEY", ""),
            "charge_net_amount": _numero(f.get("CHARGE_NET_AMOUNT", "")),
            "charge_total_amount": _numero(f.get("CHARGE_TOTAL_AMOUNT", "")),
            "charge_code_id": f.get("CHARGE_CODE_ID", ""),
            "charge_code_desc": f.get("CHARGE_CODE_DESC", ""),
            "charge_code_classification": f.get("CHARGE_CODE_CLASSIFICATION", ""),
            "grupo": f.get("GRUPO", ""),
            "sub_grupo": f.get("SUB_GRUPO", ""),
            "ciclo": f.get("ciclo", ""),
            "fecha_vencimiento": f.get("FECHA-VENCIMIENTO", "") or None,
            "deuda": f.get("DEUDA", "") or None,
            "period_start_date": f.get("PERIOD_START_DATE", "") or None,
            "period_end_date": f.get("PERIOD_END_DATE", "") or None,
        }


def _filas_clientes(carpeta: Path) -> Iterator[dict[str, Any]]:
    for f in _leer_csv(carpeta / FICHERO_CLIENTES):
        yield {
            "cod_cliente": f.get("COD_CLIENTE", ""),
            "financial_account": f.get("FINANCIAL_ACCOUNT", ""),
            "num_anexo": f.get("NUM_ANEXO", ""),
            "telefono_hash": f.get("telefono_hash", "") or None,
            "fecha_activacion_original": f.get("fecha_activacion_original", "") or None,
            "ciclo": _entero(f.get("ciclo", "")),
            "lob_type": f.get("lob_type", "") or None,
            "negocio": f.get("negocio", "") or None,
        }


def _filas_json(relativo: str, columnas: Sequence[str]) -> Iterator[dict[str, Any]]:
    fichero = RAIZ / relativo
    if not fichero.exists():
        return
    datos = json.loads(fichero.read_text(encoding="utf-8"))
    for fila in datos.get("terminos") or datos.get("faqs") or []:
        yield {c: fila.get(c) for c in columnas}


#: tabla → (clave de conflicto, generador de filas, descripción del origen)
def _fuentes() -> dict[str, tuple[str, Any, str]]:
    carpeta = _ruta_dataset()
    return {
        # Sin clave de conflicto: `cargo_facturado` no tiene clave natural única (ver el
        # comentario en la migración). Se inserta plano y se recarga vaciando antes.
        "cargo_facturado": (
            "",
            (lambda: _filas_cargos(carpeta)) if carpeta else None,
            f"$DATASET_DESAFIO1/{FICHERO_CARGOS}",
        ),
        "cliente_planta": (
            "cod_cliente,num_anexo",
            (lambda: _filas_clientes(carpeta)) if carpeta else None,
            f"$DATASET_DESAFIO1/{FICHERO_CLIENTES}",
        ),
        "vocabulario_peruano": (
            "termino",
            lambda: _filas_json(
                "data/vocabulario/vocabulario_peruano.json",
                ("termino", "significa", "concepto_id", "procedencia", "nota", "variantes"),
            ),
            "data/vocabulario/vocabulario_peruano.json",
        ),
        "faq_externa": (
            "faq_id",
            lambda: _filas_json(
                "data/externo_faqs/faqs_externas.json",
                (
                    "faq_id", "pregunta", "respuesta", "intencion", "categoria",
                    "idioma", "fuente", "licencia", "traducida",
                ),
            ),
            "data/externo_faqs/faqs_externas.json",
        ),
    }


def _ruta_insercion(tabla: str, conflicto: str) -> str:
    """Ruta REST: con ``on_conflict`` solo si la tabla tiene clave natural única."""
    return f"{tabla}?on_conflict={conflicto}" if conflicto else tabla


def _prefer(conflicto: str) -> dict[str, str]:
    """Cabecera ``Prefer``. Sin clave de conflicto no se puede pedir *merge*."""
    if conflicto:
        return {"Prefer": "resolution=merge-duplicates,return=minimal"}
    return {"Prefer": "return=minimal"}


def _vaciar(url: str, clave: str, tabla: str) -> bool:
    """Borra todas las filas de ``tabla``.

    Se usa antes de recargar una tabla sin clave natural: el dataset es una instantánea
    completa, así que insertar encima duplicaría todo. PostgREST exige un filtro para
    permitir un DELETE, de ahí el ``id=gte.0``.
    """
    estado, cuerpo = _peticion(url, clave, f"{tabla}?id=gte.0", metodo="DELETE",
                               extra={"Prefer": "return=minimal"})
    if estado >= 400:
        print(f"  ERROR vaciando {tabla}: {estado} {cuerpo[:160]}")
        return False
    return True


def _subir(url: str, clave: str, tabla: str, conflicto: str, filas: Iterator[dict[str, Any]]) -> int:
    """Sube en lotes. Devuelve cuántas filas se enviaron, o -1 si hubo error."""
    lote: list[dict[str, Any]] = []
    enviadas = 0
    for fila in filas:
        lote.append(fila)
        if len(lote) >= TAMANO_LOTE:
            estado, cuerpo = _peticion(url, clave, _ruta_insercion(tabla, conflicto),
                                       metodo="POST", cuerpo=lote, extra=_prefer(conflicto))
            if estado >= 400:
                print(f"  ERROR {tabla}: {estado} {cuerpo[:220]}")
                return -1
            enviadas += len(lote)
            print(f"    {tabla}: {enviadas:,} filas...", flush=True)
            lote = []
    if lote:
        estado, cuerpo = _peticion(url, clave, _ruta_insercion(tabla, conflicto),
                                   metodo="POST", cuerpo=lote, extra=_prefer(conflicto))
        if estado >= 400:
            print(f"  ERROR {tabla}: {estado} {cuerpo[:220]}")
            return -1
        enviadas += len(lote)
    return enviadas


#: Migración que crea todo lo que este script llena.
MIGRACION = RAIZ / "db" / "migraciones" / "004_supabase_corpus.sql"


def aplicar_migracion() -> bool:
    """Aplica el DDL por conexión directa si hay ``SUPABASE_DB_URL``.

    Devuelve ``True`` si se aplicó. El fichero es idempotente (todo ``IF NOT EXISTS`` o
    ``CREATE OR REPLACE``), así que ejecutarlo de más no rompe nada; por eso no se
    comprueba antes si las tablas ya están.
    """
    cadena = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not cadena:
        return False
    try:
        import psycopg
    except ImportError:
        print("  falta psycopg para la conexión directa: pip install 'psycopg[binary]'")
        return False
    sql = MIGRACION.read_text(encoding="utf-8")
    try:
        with psycopg.connect(cadena, connect_timeout=20, autocommit=True) as conexion:
            conexion.execute(sql)
    except Exception as exc:
        print(f"  ERROR aplicando la migración: {type(exc).__name__}: {str(exc)[:200]}")
        return False
    print(f"  migración aplicada: {MIGRACION.name}")
    return True


def cargar(*, solo_verificar: bool = False, solo: str | None = None) -> int:
    """Sube todo lo configurado. Devuelve el código de salida del proceso."""
    url, clave = _credenciales()
    fuentes = _fuentes()

    faltan = [t for t in fuentes if _peticion(url, clave, f"{t}?limit=0")[0] >= 400]
    if faltan and aplicar_migracion():
        faltan = [t for t in fuentes if _peticion(url, clave, f"{t}?limit=0")[0] >= 400]
    if faltan:
        print(f"  FALTAN TABLAS: {', '.join(faltan)}")
        print()
        print("  Dos formas de crearlas:")
        print("   a) defina SUPABASE_DB_URL en .env y vuelva a ejecutar este script")
        print("      (Supabase -> Settings -> Database -> Connection string -> URI)")
        print("   b) pegue db/esquema.sql en el SQL Editor del panel")
        return 2
    if solo_verificar:
        for tabla in fuentes:
            print(f"  {tabla:<22} existe")
        return 0

    total = 0
    for tabla, (conflicto, generador, origen) in fuentes.items():
        if solo and tabla != solo:
            continue
        if generador is None:
            print(f"  SALTADO {tabla:<15} defina DATASET_DESAFIO1 para cargar {origen}")
            continue
        print(f"  {tabla} <- {origen}")
        # Sin clave natural única no hay upsert posible: para que recargar no duplique,
        # se vacía antes. El origen está en disco, así que no se pierde nada.
        if not conflicto and not _vaciar(url, clave, tabla):
            return 1
        enviadas = _subir(url, clave, tabla, conflicto, generador())
        if enviadas < 0:
            return 1
        print(f"  {tabla:<22} {enviadas:>8,} filas")
        total += enviadas
    print(f"\n  total: {total:,} filas")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga los corpus y el dataset en Supabase.")
    parser.add_argument("--verificar", action="store_true", help="solo comprueba que las tablas existan")
    parser.add_argument("--solo", metavar="TABLA", help="carga una sola tabla")
    args = parser.parse_args()
    return cargar(solo_verificar=args.verificar, solo=args.solo)


if __name__ == "__main__":
    sys.exit(main())
