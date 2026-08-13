"""Crea `orden_servicio` en Supabase y carga `Ordenes.csv`.

La clave del cruce es `SUBSCRIBER_KEY`, que el diccionario de datos declara como PK de la
suscripción y que aparece tanto en la facturación como en las órdenes. La cuenta
financiera no viene en el CSV de órdenes: se resuelve contra `cliente_planta`
(`num_anexo` = SUBSCRIBER_KEY), que es la planta comercial.

Se guarda la cuenta ya resuelta en la propia fila —desnormalizada a propósito— porque el
ACL busca las órdenes POR CUENTA, y hacer ese JOIN en cada explicación de recibo
convertiría una consulta por índice en un cruce de 58 000 filas.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")
import psycopg  # noqa: E402

#: El dataset vive fuera del repositorio (confidencialidad, BASES §9). Se localiza con
#: `DATASET_DESAFIO1`, y si no está se cae a la carpeta hermana del escritorio, que es
#: donde la entrega la organización.
ORIGEN = Path(os.getenv("DATASET_DESAFIO1")
               or Path(__file__).resolve().parents[2] / "desafio1") / "Ordenes.csv"
LOTE = 5000

DDL = """
CREATE TABLE IF NOT EXISTS orden_servicio (
    id                   BIGSERIAL PRIMARY KEY,
    customer_key         TEXT NOT NULL,
    subscriber_key       TEXT NOT NULL,
    financial_account    TEXT,
    razon_desc           TEXT,
    razon_id             TEXT,
    tipo_item            TEXT,
    estado               TEXT,
    inicio               TIMESTAMP,
    completado           TIMESTAMP,
    UNIQUE (subscriber_key, razon_id, completado, tipo_item)
);
CREATE INDEX IF NOT EXISTS ix_orden_cuenta ON orden_servicio (financial_account);
CREATE INDEX IF NOT EXISTS ix_orden_suscripcion ON orden_servicio (subscriber_key);
"""


def fecha(valor: str) -> str | None:
    limpio = (valor or "").strip()
    return limpio or None


def main() -> int:
    con = psycopg.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=30,
                          autocommit=True, prepare_threshold=None)
    con.execute(DDL)
    print("tabla orden_servicio lista")

    # La planta cabe holgada en memoria (20 000 filas) y evita un JOIN por fila.
    planta = {
        str(anexo): str(cuenta)
        for anexo, cuenta in con.execute(
            "SELECT num_anexo, financial_account FROM cliente_planta"
        ).fetchall()
    }
    print(f"planta cargada: {len(planta):,} suscripciones")

    con.execute("TRUNCATE orden_servicio")
    total = con_cuenta = 0
    lote: list[tuple] = []
    with ORIGEN.open(encoding="utf-8", errors="replace", newline="") as f:
        for fila in csv.DictReader(f):
            suscripcion = (fila.get("SUBSCRIBER_KEY") or "").strip()
            cuenta = planta.get(suscripcion)
            con_cuenta += bool(cuenta)
            lote.append((
                (fila.get("CUSTOMER_KEY") or "").strip(), suscripcion, cuenta,
                (fila.get("ORDER_ACTION_REASON_DESC") or "").strip(),
                (fila.get("ORDER_ACTION_REASON_ID") or "").strip(),
                (fila.get("ORDER_ITEM_TYPE_DESC") or "").strip(),
                (fila.get("ORDER_ACTION_STATUS_DESC") or "").strip(),
                fecha(fila.get("ORDER_ACTION_START_DATE", "")),
                fecha(fila.get("ORDER_ACTION_COMPLETION_DATE", "")),
            ))
            total += 1
            if len(lote) >= LOTE:
                volcar(con, lote); lote.clear()
                print(f"   {total:,} filas…", flush=True)
    if lote:
        volcar(con, lote)

    n = con.execute("SELECT count(*) FROM orden_servicio").fetchone()[0]
    print(f"\ncargadas {n:,} de {total:,} filas · con cuenta resuelta: {con_cuenta:,} "
          f"({con_cuenta * 100 // max(total, 1)} %)")
    return 0


def volcar(con, lote):
    with con.cursor() as cur:
        cur.executemany(
            """INSERT INTO orden_servicio
               (customer_key, subscriber_key, financial_account, razon_desc, razon_id,
                tipo_item, estado, inicio, completado)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            lote,
        )


if __name__ == "__main__":
    sys.exit(main())
