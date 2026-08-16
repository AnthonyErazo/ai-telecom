"""Deja las casuísticas listas para que el MOTOR las consulte, no solo el buscador.

Las tres tablas (`casuistica_reconexion`, `casuistica_prorrateo_alta`,
`casuistica_descuento_cuota`) entraron con `cargar_casuisticas.py` en TEXT y sin índices.
Para lo que servían hasta ahora —alimentar el corpus RAG, que se lee entero una vez al
arrancar— daba igual. Para lo que vienen a servir ahora no: el motor tiene que preguntar
«¿qué promoción tiene ESTA cuenta?» en cada explicación, y sin índice eso es un escaneo
de 33 823 filas por consulta; y con `porcentajepromo` en TEXT, comparar o multiplicar
exige convertir en cada uso, que es donde se cuelan los errores.

Qué hace, y por qué así:

* **Vistas tipadas** en vez de alterar las columnas. La tabla cruda se queda intacta y
  sigue siendo el reflejo exacto del CSV que entregó negocio —si mañana hay que reprocesar
  o discutir un dato, ahí está sin tocar—. La vista es la que el motor consulta. Cambiar
  el tipo en origen obligaría además a recargar los 92 785 registros.
* **Conversión tolerante** (`NULLIF` + regex antes de castear). Estos ficheros traen celdas
  vacías y valores con coma decimal; un `CAST` directo revienta la vista entera por una
  fila mala. Aquí una celda ilegible se queda en `NULL`, que el motor sabe interpretar
  como «no consta», que es exactamente lo que es.
* **Índices sobre la cuenta**, que es la única clave por la que el motor pregunta.

Es idempotente: se puede correr las veces que haga falta.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from apps.api.settings import obtener_ajustes  # noqa: E402

_LOG = logging.getLogger("tipar_casuisticas")

#: Un número con coma o punto decimal, o vacío. Lo que no case se convierte en NULL en vez
#: de tumbar la vista: prefiero una laguna declarada a un error de carga.
_NUMERO = r"^-?[0-9]+([.,][0-9]+)?$"


def _num(columna: str, tipo: str = "numeric") -> str:
    """Expresión SQL que convierte una columna TEXT a número, o NULL si no lo es."""
    limpio = f"replace(trim({columna}), ',', '.')"
    return (
        f"CASE WHEN {limpio} ~ '{_NUMERO}' THEN CAST({limpio} AS {tipo}) END AS {columna}"
    )


VISTAS: tuple[tuple[str, str], ...] = (
    (
        "v_descuento_cuota",
        f"""
        SELECT cuentafinanciera,
               billingarrangement,
               ciclo,
               -- 'RA' = renta adelantada, 'RV' = vencida. Es el dato que hasta ahora se
               -- deducía del sufijo del grupo de cargo; aquí viene dicho.
               upper(trim(tiporenta))                    AS tipo_renta,
               trim(tipo_descuento)                      AS tipo_descuento,
               trim(descripcion)                         AS descripcion,
               trim(traduccion)                          AS traduccion,
               {_num("promotionduration", "integer")},
               {_num("porcentajepromo")},
               {_num("diasvencidos", "integer")},
               {_num("diasadelantados", "integer")},
               {_num("cuotaactual", "integer")},
               {_num("monto_descuento")},
               fechainicio, fechafin, chargecode
        FROM casuistica_descuento_cuota
        """,
    ),
    (
        "v_prorrateo_alta",
        f"""
        SELECT cuentafinanciera,
               numerorecibo          AS numero_recibo,
               ciclica,
               fecha_inicio_minima   AS fecha_inicio,
               fecha_fin_maxima      AS fecha_fin,
               {_num("suma_prorrateo")},
               {_num("q_cargos", "integer")}
        FROM casuistica_prorrateo_alta
        """,
    ),
    (
        "v_reconexion",
        f"""
        SELECT cuentafinanciera,
               codigo,
               numerorecibo    AS numero_recibo,
               descripcion,
               fechareconexion AS fecha_reconexion,
               fechacorte      AS fecha_corte,
               ciclica,
               {_num("monto")}
        FROM casuistica_reconexion
        """,
    ),
)

#: La única clave por la que pregunta el motor.
INDICES: tuple[tuple[str, str], ...] = (
    ("casuistica_descuento_cuota", "cuentafinanciera"),
    ("casuistica_prorrateo_alta", "cuentafinanciera"),
    ("casuistica_reconexion", "cuentafinanciera"),
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with psycopg.connect(obtener_ajustes().dsn_postgres, prepare_threshold=None) as conexion:
        cur = conexion.cursor()
        for tabla, columna in INDICES:
            nombre = f"ix_{tabla}_cuenta"
            cur.execute(f"CREATE INDEX IF NOT EXISTS {nombre} ON {tabla} ({columna})")
            _LOG.info("índice %s listo", nombre)
        for vista, consulta in VISTAS:
            cur.execute(f"CREATE OR REPLACE VIEW {vista} AS {consulta}")
            total = cur.execute(f"SELECT COUNT(*) FROM {vista}").fetchone()
            _LOG.info("vista %s: %s filas", vista, total[0] if total else "?")
        conexion.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
