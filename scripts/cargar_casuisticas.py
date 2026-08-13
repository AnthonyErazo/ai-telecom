"""Carga en Supabase las tablas de casuística del dataset actualizado del Desafío 1.

Qué sube y para qué sirve cada una
----------------------------------
==============================  =========================================================
``BRAINY_RECONEXIONESV3``       corte y reconexión con **fecha de corte, fecha de
                                reconexión e importe**: la evidencia del cargo por
                                reconexión, que hasta ahora el motor solo podía suponer.
``BRAINY_PRORRATEO_ALTASV3``    prorrateo por alta, con el rango de fechas y la suma
                                prorrateada: el «cobro por los días que usó».
``BRAINY_DESCUENTOS_CUOTAS``    promociones y cuotas, con ``PromotionDuration``,
                                ``PorcentajePromo`` y los días vencidos/adelantados: es
                                lo que explica el fin de descuento y su prorrateo.
``NOTAS_CREDITO``               notas de crédito, una de las nueve causas oficiales.
``CATALOGO_OFERTAS``            tarifa vigente por código de cargo y tipo de renta.
==============================  =========================================================

Por qué todas las columnas son ``TEXT``
---------------------------------------
Porque el dataset es un export y **lo que se guarda es lo que llegó**, sin interpretar.
Las fechas vienen en tres formatos distintos según el fichero y los importes con coma o
con punto; convertirlos aquí obligaría a decidir, en la frontera de la carga, cosas que
el ACL ya sabe decidir con sus propias reglas y con trazabilidad. Guardar crudo y
convertir al leer mantiene una sola conversión en el proyecto —la de
``packages/core_domain/dinero``— en vez de dos que se pueden desincronizar.

Idempotente: cada tabla se vacía y se vuelve a cargar. Son tablas de apoyo derivadas del
export, no datos que el sistema genere, así que recargarlas no pierde nada.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
import psycopg  # noqa: E402

#: El dataset vive fuera del repositorio (confidencialidad, BASES §9).
RAIZ_DATASET = Path(
    os.getenv("DATASET_DESAFIO1") or Path(__file__).resolve().parents[2] / "desafio1"
)

#: fichero → (tabla, delimitador). El delimitador no se adivina: los ficheros del
#: desafío mezclan `;` y `,` según quién los exportó, y detectarlo por olfato falla con
#: los campos que contienen comas dentro de comillas.
FUENTES: tuple[tuple[str, str, str], ...] = (
    ("BRAINY_RECONEXIONESV3.csv", "casuistica_reconexion", ";"),
    ("BRAINY_PRORRATEO_ALTASV3.csv", "casuistica_prorrateo_alta", ";"),
    ("BRAINY_DESCUENTOS_CUOTAS.csv", "casuistica_descuento_cuota", ","),
    ("NOTAS_CREDITO.csv", "nota_credito", ","),
    ("CATALOGO-OFERTAS.csv", "catalogo_oferta", ";"),
)

LOTE = 5000


def columna(nombre: str) -> str:
    """``CuentaFinanciera`` → ``cuentafinanciera``; ``CHARGE CODE`` → ``charge_code``."""
    limpio = re.sub(r"[^0-9a-zA-Z]+", "_", nombre.strip()).strip("_").lower()
    return limpio or "col"


def cargar(con, fichero: Path, tabla: str, delimitador: str) -> int:
    with fichero.open(encoding="utf-8", errors="replace", newline="") as f:
        lector = csv.reader(f, delimiter=delimitador)
        cabecera = next(lector)
        columnas = [columna(c) for c in cabecera]
        # Nombres repetidos tras normalizar: se numeran en vez de perderse.
        vistos: dict[str, int] = {}
        for i, c in enumerate(columnas):
            if c in vistos:
                vistos[c] += 1
                columnas[i] = f"{c}_{vistos[c]}"
            else:
                vistos[c] = 0

        con.execute(f"DROP TABLE IF EXISTS {tabla}")
        con.execute(
            f"CREATE TABLE {tabla} (id BIGSERIAL PRIMARY KEY, "
            + ", ".join(f"{c} TEXT" for c in columnas)
            + ")"
        )
        marcas = ",".join(["%s"] * len(columnas))
        sql = f"INSERT INTO {tabla} ({','.join(columnas)}) VALUES ({marcas})"

        lote: list[tuple] = []
        total = 0
        with con.cursor() as cur:
            for fila in lector:
                # Filas cortas o largas: se ajustan sin descartar. Un export con una coma
                # de más en una descripción no debe costar una casuística entera.
                valores = (fila + [None] * len(columnas))[: len(columnas)]
                lote.append(tuple(v if (v or "") != "" else None for v in valores))
                total += 1
                if len(lote) >= LOTE:
                    cur.executemany(sql, lote)
                    lote.clear()
            if lote:
                cur.executemany(sql, lote)
    return total


def main() -> int:
    dsn = os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("falta SUPABASE_DB_URL en el .env")
        return 1
    if not RAIZ_DATASET.is_dir():
        print(f"no existe el dataset en {RAIZ_DATASET}")
        return 1

    con = psycopg.connect(dsn, connect_timeout=30, autocommit=True, prepare_threshold=None)
    for nombre, tabla, delimitador in FUENTES:
        fichero = RAIZ_DATASET / nombre
        if not fichero.is_file():
            print(f"  {tabla:28} FALTA {nombre}")
            continue
        n = cargar(con, fichero, tabla, delimitador)
        real = con.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0]
        print(f"  {tabla:28} {real:7,} filas   (leídas {n:,})   ← {nombre}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
