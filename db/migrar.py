"""Aplicador idempotente de migraciones SQL para recibo-claro.

Recorre ``db/migraciones/*.sql`` en orden de versión y aplica las que falten,
registrando cada una en la tabla ``schema_migrations``. Volver a ejecutarlo no hace
nada: es la operación que ``make migrate`` repite en cada arranque de la demo.

Garantías
---------
* **Atomicidad por migración.** Cada fichero se ejecuta y se registra dentro de la
  misma transacción. Si falla a mitad, no queda ni el DDL a medias ni el registro.
* **Un solo aplicador a la vez.** Se toma un *advisory lock* de PostgreSQL, así que
  dos contenedores que arranquen a la vez no se pisan.
* **Detección de manipulación.** Se guarda el SHA-256 de cada fichero aplicado. Si el
  contenido de una migración ya aplicada cambia, el aplicador se detiene y lo dice:
  editar una migración pasada es exactamente el error que rompe entornos.
* **Sin trocear el SQL.** El fichero se envía entero al servidor, de modo que los
  cuerpos de función entre ``$$ ... $$`` y los bloques ``DO`` llegan intactos.

Uso::

    python -m db.migrar                          # aplica lo pendiente
    python -m db.migrar --estado                 # qué hay aplicado y qué falta
    python -m db.migrar --listar                 # migraciones locales (sin conectar)
    python -m db.migrar --hasta 002              # aplica hasta la 002 incluida
    python -m db.migrar --dry-run                # enseña el plan, no toca nada
    python -m db.migrar --verificar-dim          # EMBED_DIM vs. las columnas vector(N)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CLAVE_CERROJO",
    "DIRECTORIO_MIGRACIONES",
    "SQL_TABLA_CONTROL",
    "EstadoMigracion",
    "Migracion",
    "ResumenMigracion",
    "aplicar",
    "consultar_estado",
    "main",
    "migraciones_disponibles",
    "verificar_dimension_embedding",
]

#: Directorio de migraciones relativo a la raíz del proyecto.
DIRECTORIO_MIGRACIONES = Path("db") / "migraciones"

#: Nombre de fichero esperado: ``NNN_nombre.sql``.
PATRON_FICHERO = re.compile(r"^(?P<version>\d+)_(?P<nombre>[a-z0-9_\-]+)\.sql$", re.IGNORECASE)

#: Clave del advisory lock que serializa a los aplicadores concurrentes.
CLAVE_CERROJO = 0x5265_6369_626F  # "Recibo" en hexadecimal

#: Dimensión de embedding por defecto (coincide con EMBED_DIM del .env.example).
DIM_EMBEDDING_POR_DEFECTO = 768

SQL_TABLA_CONTROL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         text        PRIMARY KEY,
    nombre          text        NOT NULL,
    checksum_sha256 char(64)    NOT NULL,
    aplicada_en     timestamptz NOT NULL DEFAULT now(),
    duracion_ms     integer     NOT NULL DEFAULT 0,
    aplicada_por    text        NOT NULL DEFAULT current_user
);
COMMENT ON TABLE schema_migrations IS
    'Migraciones ya aplicadas. El checksum detecta que alguien editó una migración pasada.';
"""


# --------------------------------------------------------------------------- #
# Descubrimiento de ficheros
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Migracion:
    """Un fichero ``NNN_nombre.sql`` listo para aplicarse."""

    version: str
    nombre: str
    ruta: Path
    sql: str
    checksum: str

    @property
    def etiqueta(self) -> str:
        """``001_core``: cómo se nombra la migración en los mensajes."""
        return f"{self.version}_{self.nombre}"


@dataclass(frozen=True, slots=True)
class EstadoMigracion:
    """Situación de una migración frente a la base de datos."""

    version: str
    nombre: str
    aplicada: bool
    checksum_local: str
    checksum_aplicado: str | None = None
    aplicada_en: str | None = None

    @property
    def alterada(self) -> bool:
        """Verdadero si el fichero cambió después de haberse aplicado."""
        return (
            self.aplicada
            and self.checksum_aplicado is not None
            and self.checksum_aplicado != self.checksum_local
        )


@dataclass(slots=True)
class ResumenMigracion:
    """Resultado de una pasada del aplicador."""

    aplicadas: list[str]
    omitidas: list[str]
    duracion_ms: int
    dry_run: bool = False

    def a_dict(self) -> dict[str, Any]:
        """Proyección serializable para ``--json``."""
        return {
            "aplicadas": self.aplicadas,
            "omitidas": self.omitidas,
            "duracion_ms": self.duracion_ms,
            "dry_run": self.dry_run,
        }


def raiz_proyecto() -> Path:
    """Localiza la raíz del repositorio subiendo hasta encontrar ``pyproject.toml``."""
    actual = Path(__file__).resolve()
    for candidato in actual.parents:
        if (candidato / "pyproject.toml").is_file():
            return candidato
    return actual.parent.parent


def migraciones_disponibles(directorio: str | Path | None = None) -> list[Migracion]:
    """Lee las migraciones del directorio, ordenadas por versión.

    Args:
        directorio: carpeta con los ``.sql``. Por defecto ``db/migraciones``.

    Returns:
        Las migraciones ordenadas. Los ficheros que no siguen el patrón
        ``NNN_nombre.sql`` se ignoran (permite dejar notas sueltas al lado).

    Raises:
        FileNotFoundError: si el directorio no existe.
        ValueError: si dos ficheros declaran la misma versión.
    """
    carpeta = Path(directorio) if directorio is not None else raiz_proyecto() / DIRECTORIO_MIGRACIONES
    if not carpeta.is_dir():
        raise FileNotFoundError(f"no existe el directorio de migraciones: {carpeta}")

    encontradas: dict[str, Migracion] = {}
    for ruta in sorted(carpeta.glob("*.sql")):
        coincidencia = PATRON_FICHERO.match(ruta.name)
        if coincidencia is None:
            continue
        version = coincidencia.group("version")
        if version in encontradas:
            raise ValueError(
                f"versión duplicada {version}: {encontradas[version].ruta.name} y {ruta.name}"
            )
        sql = ruta.read_text(encoding="utf-8")
        encontradas[version] = Migracion(
            version=version,
            nombre=coincidencia.group("nombre"),
            ruta=ruta,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
    return [encontradas[version] for version in sorted(encontradas)]


# --------------------------------------------------------------------------- #
# Conexión
# --------------------------------------------------------------------------- #
def dsn_por_defecto() -> str:
    """DSN de conexión: ``$DATABASE_URL`` o el de desarrollo del ``.env.example``."""
    return os.getenv("DATABASE_URL", "postgresql://recibo:recibo@localhost:5432/recibo")


def _conectar(dsn: str) -> Any:
    """Abre la conexión con psycopg 3, con un mensaje claro si falta la dependencia."""
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - entorno sin dependencias
        raise RuntimeError(
            "falta psycopg 3: instale las dependencias del proyecto (pip install -e .)"
        ) from error
    return psycopg.connect(dsn, autocommit=False)


# --------------------------------------------------------------------------- #
# Aplicación
# --------------------------------------------------------------------------- #
def _asegurar_tabla_control(conexion: Any) -> None:
    """Crea ``schema_migrations`` si no existe y confirma."""
    with conexion.cursor() as cursor:
        cursor.execute(SQL_TABLA_CONTROL)
    conexion.commit()


def _aplicadas(conexion: Any) -> dict[str, tuple[str, str]]:
    """Devuelve ``{version: (checksum, aplicada_en)}`` de lo ya registrado."""
    with conexion.cursor() as cursor:
        cursor.execute(
            "SELECT version, checksum_sha256, aplicada_en::text FROM schema_migrations"
        )
        return {fila[0]: (fila[1], fila[2]) for fila in cursor.fetchall()}


def aplicar(
    dsn: str | None = None,
    *,
    directorio: str | Path | None = None,
    hasta: str | None = None,
    dry_run: bool = False,
    aceptar_cambio_checksum: bool = False,
    verboso: bool = True,
) -> ResumenMigracion:
    """Aplica las migraciones pendientes en orden.

    Args:
        dsn: cadena de conexión. Por defecto ``$DATABASE_URL``.
        directorio: carpeta de migraciones.
        hasta: última versión a aplicar (inclusive), p. ej. ``"002"``.
        dry_run: calcula el plan y no ejecuta nada.
        aceptar_cambio_checksum: actualiza el checksum registrado de una migración ya
            aplicada cuyo fichero cambió, **sin volver a ejecutarla**. Es una salida de
            emergencia: lo correcto es añadir una migración nueva.
        verboso: imprime el progreso por la salida estándar.

    Returns:
        El resumen de lo aplicado y lo omitido.

    Raises:
        RuntimeError: si una migración ya aplicada tiene un contenido distinto del
            registrado y no se pasó ``aceptar_cambio_checksum``.
    """
    migraciones = migraciones_disponibles(directorio)
    if hasta is not None:
        migraciones = [m for m in migraciones if m.version <= hasta]

    inicio = time.perf_counter()
    aplicadas_ahora: list[str] = []
    omitidas: list[str] = []

    with _conectar(dsn or dsn_por_defecto()) as conexion:
        _asegurar_tabla_control(conexion)

        # Serializa a los aplicadores concurrentes durante toda la pasada.
        with conexion.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (CLAVE_CERROJO,))
        conexion.commit()

        try:
            registradas = _aplicadas(conexion)
            for migracion in migraciones:
                registro = registradas.get(migracion.version)
                if registro is not None:
                    checksum_registrado = registro[0]
                    if checksum_registrado != migracion.checksum:
                        if not aceptar_cambio_checksum:
                            raise RuntimeError(
                                f"la migración {migracion.etiqueta} ya está aplicada pero su "
                                f"contenido cambió (registrado {checksum_registrado[:12]}…, "
                                f"actual {migracion.checksum[:12]}…). Editar migraciones "
                                f"aplicadas rompe los entornos existentes: añada una nueva. "
                                f"Si sabe lo que hace, use --aceptar-cambio-checksum."
                            )
                        if not dry_run:
                            with conexion.cursor() as cursor:
                                cursor.execute(
                                    "UPDATE schema_migrations SET checksum_sha256 = %s "
                                    "WHERE version = %s",
                                    (migracion.checksum, migracion.version),
                                )
                            conexion.commit()
                        if verboso:
                            print(f"  ~ {migracion.etiqueta}: checksum actualizado")
                    omitidas.append(migracion.etiqueta)
                    if verboso:
                        print(f"  = {migracion.etiqueta}: ya aplicada")
                    continue

                if dry_run:
                    aplicadas_ahora.append(migracion.etiqueta)
                    if verboso:
                        print(f"  + {migracion.etiqueta}: se aplicaría ({len(migracion.sql)} bytes)")
                    continue

                marca = time.perf_counter()
                try:
                    with conexion.cursor() as cursor:
                        # El fichero entero en una sola llamada: no se trocea por ';'
                        # para no partir los cuerpos $$ ... $$ ni los bloques DO.
                        cursor.execute(migracion.sql)
                        cursor.execute(
                            "INSERT INTO schema_migrations "
                            "(version, nombre, checksum_sha256, duracion_ms) "
                            "VALUES (%s, %s, %s, %s)",
                            (
                                migracion.version,
                                migracion.nombre,
                                migracion.checksum,
                                int((time.perf_counter() - marca) * 1000),
                            ),
                        )
                    conexion.commit()
                except Exception:
                    conexion.rollback()
                    if verboso:
                        print(f"  ! {migracion.etiqueta}: FALLÓ, transacción revertida")
                    raise
                aplicadas_ahora.append(migracion.etiqueta)
                if verboso:
                    print(
                        f"  + {migracion.etiqueta}: aplicada en "
                        f"{int((time.perf_counter() - marca) * 1000)} ms"
                    )
        finally:
            with conexion.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (CLAVE_CERROJO,))
            conexion.commit()

    return ResumenMigracion(
        aplicadas=aplicadas_ahora,
        omitidas=omitidas,
        duracion_ms=int((time.perf_counter() - inicio) * 1000),
        dry_run=dry_run,
    )


def consultar_estado(
    dsn: str | None = None, *, directorio: str | Path | None = None
) -> list[EstadoMigracion]:
    """Cruza las migraciones locales con las registradas en la base de datos."""
    migraciones = migraciones_disponibles(directorio)
    with _conectar(dsn or dsn_por_defecto()) as conexion:
        _asegurar_tabla_control(conexion)
        registradas = _aplicadas(conexion)
    return [
        EstadoMigracion(
            version=migracion.version,
            nombre=migracion.nombre,
            aplicada=migracion.version in registradas,
            checksum_local=migracion.checksum,
            checksum_aplicado=registradas.get(migracion.version, (None, None))[0],
            aplicada_en=registradas.get(migracion.version, (None, None))[1],
        )
        for migracion in migraciones
    ]


def verificar_dimension_embedding(
    dsn: str | None = None, esperado: int | None = None
) -> list[dict[str, Any]]:
    """Compara ``EMBED_DIM`` con la dimensión real de cada columna ``vector(N)``.

    Cambiar de modelo de embeddings cambia la dimensión y **obliga a reindexar** todo
    el corpus. Esta comprobación convierte ese error silencioso (vectores de 768 y de
    1536 conviviendo) en un aviso explícito al arrancar.

    Returns:
        Una fila por columna vectorial con ``tabla``, ``columna``, ``dimension``,
        ``esperado`` y ``coincide``.
    """
    dim_esperada = esperado or int(os.getenv("EMBED_DIM", DIM_EMBEDDING_POR_DEFECTO))
    consulta = """
        SELECT c.relname AS tabla, a.attname AS columna, a.atttypmod AS dimension
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_type  t ON t.oid = a.atttypid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE t.typname = 'vector'
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND c.relkind = 'r'
           AND n.nspname NOT IN ('pg_catalog', 'information_schema')
         ORDER BY c.relname, a.attname
    """
    with _conectar(dsn or dsn_por_defecto()) as conexion, conexion.cursor() as cursor:
        cursor.execute(consulta)
        filas = cursor.fetchall()
    return [
        {
            "tabla": tabla,
            "columna": columna,
            "dimension": dimension,
            "esperado": dim_esperada,
            "coincide": dimension == dim_esperada,
        }
        for tabla, columna, dimension in filas
    ]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _construir_parser() -> argparse.ArgumentParser:
    """Define los argumentos de la línea de órdenes."""
    parser = argparse.ArgumentParser(
        prog="db.migrar",
        description="Aplicador idempotente de migraciones SQL de recibo-claro.",
    )
    parser.add_argument("--dsn", default=None, help="cadena de conexión (por defecto $DATABASE_URL)")
    parser.add_argument("--dir", dest="directorio", default=None, help="carpeta de migraciones")
    parser.add_argument("--hasta", default=None, help='última versión a aplicar, p. ej. "002"')
    parser.add_argument("--dry-run", action="store_true", help="enseña el plan sin ejecutarlo")
    parser.add_argument("--estado", action="store_true", help="muestra qué está aplicado")
    parser.add_argument(
        "--listar", action="store_true", help="lista las migraciones locales sin conectar"
    )
    parser.add_argument(
        "--verificar-dim",
        action="store_true",
        help="compara EMBED_DIM con la dimensión real de las columnas vector(N)",
    )
    parser.add_argument(
        "--aceptar-cambio-checksum",
        action="store_true",
        help="acepta que una migración aplicada haya cambiado (salida de emergencia)",
    )
    parser.add_argument("--json", action="store_true", help="salida en JSON para scripts")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de la línea de órdenes.

    Returns:
        ``0`` si todo fue bien, ``1`` si hubo un error o una comprobación falló.
    """
    args = _construir_parser().parse_args(argv)

    try:
        if args.listar:
            migraciones = migraciones_disponibles(args.directorio)
            if args.json:
                print(json.dumps(
                    [
                        {"version": m.version, "nombre": m.nombre, "checksum": m.checksum,
                         "bytes": len(m.sql)}
                        for m in migraciones
                    ],
                    ensure_ascii=False, indent=2,
                ))
            else:
                print(f"Migraciones locales ({len(migraciones)}):")
                for migracion in migraciones:
                    print(
                        f"  {migracion.etiqueta:<24} {migracion.checksum[:12]}…"
                        f"  {len(migracion.sql):>7} bytes"
                    )
            return 0

        if args.estado:
            estados = consultar_estado(args.dsn, directorio=args.directorio)
            if args.json:
                print(json.dumps([vars(e) | {"alterada": e.alterada} for e in estados],
                                 ensure_ascii=False, indent=2))
            else:
                print("Estado de las migraciones:")
                for estado in estados:
                    marca = "aplicada" if estado.aplicada else "PENDIENTE"
                    aviso = "  <- FICHERO ALTERADO" if estado.alterada else ""
                    fecha = f"  {estado.aplicada_en}" if estado.aplicada_en else ""
                    print(f"  {estado.version}_{estado.nombre:<20} {marca}{fecha}{aviso}")
            return 1 if any(estado.alterada for estado in estados) else 0

        if args.verificar_dim:
            filas = verificar_dimension_embedding(args.dsn)
            if args.json:
                print(json.dumps(filas, ensure_ascii=False, indent=2))
            else:
                if not filas:
                    print(
                        "No hay ninguna columna vector(N): o falta aplicar 002_rag.sql o la "
                        "extensión pgvector no está instalada. El retriever degradará a BM25 puro."
                    )
                for fila in filas:
                    estado = "ok" if fila["coincide"] else "DESAJUSTE"
                    print(
                        f"  {fila['tabla']}.{fila['columna']}: vector({fila['dimension']})"
                        f" vs EMBED_DIM={fila['esperado']}  [{estado}]"
                    )
                if any(not fila["coincide"] for fila in filas):
                    print(
                        "\nCambiar de modelo de embeddings obliga a migrar la columna y a "
                        "REINDEXAR todo el corpus (ver cabecera de 002_rag.sql)."
                    )
            # Sin columnas vectoriales tampoco es "todo correcto": es una degradación.
            return 0 if filas and all(fila["coincide"] for fila in filas) else 1

        if not args.json:
            print(f"Aplicando migraciones sobre {args.dsn or dsn_por_defecto()}")
        resumen = aplicar(
            args.dsn,
            directorio=args.directorio,
            hasta=args.hasta,
            dry_run=args.dry_run,
            aceptar_cambio_checksum=args.aceptar_cambio_checksum,
            verboso=not args.json,
        )
        if args.json:
            print(json.dumps(resumen.a_dict(), ensure_ascii=False, indent=2))
        else:
            verbo = "se aplicarían" if resumen.dry_run else "aplicadas"
            print(
                f"{len(resumen.aplicadas)} {verbo}, {len(resumen.omitidas)} ya estaban, "
                f"{resumen.duracion_ms} ms"
            )
        return 0

    except Exception as error:  # la CLI reporta y sale con código 1
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
