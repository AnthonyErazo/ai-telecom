"""Aplica ``db/esquema.sql`` a la base configurada.

    python -m db.migrar              # aplica el esquema
    python -m db.migrar --mostrar    # solo imprime la ruta y el tamaño

Antes esto recorría ``db/migraciones/*.sql`` en orden de versión y llevaba una tabla de
control. Ya no hace falta: **el esquema es un solo fichero idempotente**. Un proyecto que
nunca se ha desplegado por incrementos no gana nada con un historial de migraciones; solo
obliga a leer varios ficheros para saber qué tablas existen, y abre la puerta a que dos
de ellos definan la misma tabla con columnas distintas (que es exactamente lo que pasó).

El destino sale de ``SUPABASE_DB_URL`` y, en su defecto, de ``DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ESQUEMA = RAIZ / "db" / "esquema.sql"


def _dsn() -> str:
    """Cadena de conexión, con Supabase por delante."""
    for var in ("SUPABASE_DB_URL", "DATABASE_URL"):
        valor = (os.getenv(var) or "").strip()
        if valor:
            return valor
    raise SystemExit("defina SUPABASE_DB_URL (o DATABASE_URL) para aplicar el esquema")


def aplicar() -> int:
    """Aplica el esquema completo. Idempotente: se puede repetir sin perder datos."""
    if not ESQUEMA.is_file():
        raise SystemExit(f"no se encuentra {ESQUEMA}")
    import psycopg

    with psycopg.connect(_dsn(), connect_timeout=30, autocommit=True) as conexion:
        conexion.execute(ESQUEMA.read_text(encoding="utf-8"))
        total = conexion.execute(
            "select count(*) from information_schema.tables where table_schema='public'"
        ).fetchone()[0]
    print(f"  esquema aplicado · {total} tablas y vistas en public")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mostrar", action="store_true", help="solo imprime la ruta del esquema")
    args = parser.parse_args()
    if args.mostrar:
        print(f"  {ESQUEMA} · {len(ESQUEMA.read_text(encoding='utf-8').splitlines())} líneas")
        return 0
    return aplicar()


if __name__ == "__main__":
    sys.exit(main())
