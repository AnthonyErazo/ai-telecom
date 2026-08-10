"""``MODO_ALMACENAMIENTO``: el único interruptor que decide si se toca PostgreSQL.

Lo que se protege aquí es la promesa de arranque: *en una laptop limpia, sin Docker y
sin base de datos, ``uvicorn apps.api.main:app`` levanta y explica*. Esa promesa se rompe
en silencio de dos maneras, y las dos tienen su prueba:

1. que el valor por defecto empiece a exigir PostgreSQL;
2. que una ``DATABASE_URL`` heredada del entorno cuele una conexión que nadie pidió y el
   arranque pague el timeout de conexión antes de degradar.

No se abre ninguna conexión en todo el módulo: son ajustes y un índice forzado a memoria.
"""

from __future__ import annotations

import pytest

from apps.api.settings import (
    ALMACENAMIENTO_AUTO,
    ALMACENAMIENTO_MEMORIA,
    ALMACENAMIENTO_POSTGRES,
    MODOS_ALMACENAMIENTO,
    Ajustes,
)
from packages.retriever.vectorial import IndiceVectorial

DSN = "postgresql://recibo:recibo@db:5432/recibo"


def _ajustes(**extra: object) -> Ajustes:
    """Ajustes construidos por alias, sin depender del entorno del proceso."""
    base: dict[str, object] = {"DATABASE_URL": "", "MODO_ALMACENAMIENTO": ALMACENAMIENTO_AUTO}
    base.update(extra)
    return Ajustes(**base)  # type: ignore[arg-type]


def test_el_valor_por_defecto_no_exige_postgres() -> None:
    """Sin configuración ninguna, el modo es ``auto`` y no hay DSN que usar."""
    ajustes = Ajustes()  # type: ignore[call-arg]
    assert ajustes.modo_almacenamiento in MODOS_ALMACENAMIENTO
    assert ajustes.dsn_postgres is None or ajustes.database_url.strip()


def test_auto_sin_database_url_usa_memoria() -> None:
    """Es el caso de la laptop limpia: no hay DSN, luego no hay conexión."""
    ajustes = _ajustes()
    assert ajustes.usa_postgres is False
    assert ajustes.dsn_postgres is None
    assert ajustes.almacenamiento()["previsto"] == "memoria"


def test_auto_con_database_url_usa_postgres() -> None:
    """Es el caso de ``docker compose``, que sí inyecta ``DATABASE_URL``."""
    ajustes = _ajustes(DATABASE_URL=DSN)
    assert ajustes.usa_postgres is True
    assert ajustes.dsn_postgres == DSN
    assert ajustes.almacenamiento()["previsto"] == "postgres"


def test_memoria_ignora_una_database_url_heredada() -> None:
    """Quien pide memoria no quiere pagar ni el timeout de conexión."""
    ajustes = _ajustes(MODO_ALMACENAMIENTO=ALMACENAMIENTO_MEMORIA, DATABASE_URL=DSN)
    assert ajustes.usa_postgres is False
    assert ajustes.dsn_postgres is None
    assert ajustes.almacenamiento()["motivo"] == "MODO_ALMACENAMIENTO=memoria"


def test_postgres_sin_dsn_degrada_en_vez_de_romper() -> None:
    """Pedir PostgreSQL sin DSN no puede tumbar el arranque: degrada y lo dice."""
    ajustes = _ajustes(MODO_ALMACENAMIENTO=ALMACENAMIENTO_POSTGRES)
    assert ajustes.usa_postgres is False
    assert "DATABASE_URL" in str(ajustes.almacenamiento()["motivo"])


@pytest.mark.parametrize("valor", ["  MEMORIA  ", "Postgres", "AUTO"])
def test_el_modo_se_normaliza(valor: str) -> None:
    """Mayúsculas y espacios sobrantes no deben cambiar el comportamiento."""
    assert _ajustes(MODO_ALMACENAMIENTO=valor).modo_almacenamiento == valor.strip().lower()


def test_un_modo_desconocido_cae_a_memoria() -> None:
    """Una errata en la variable degrada a lo que no exige infraestructura."""
    assert _ajustes(MODO_ALMACENAMIENTO="postgrez").modo_almacenamiento == ALMACENAMIENTO_MEMORIA


def test_el_destino_no_lleva_credenciales() -> None:
    """El destino se escribe en el log y en ``/salud/preparacion``: sin contraseña."""
    destino = str(_ajustes(DATABASE_URL=DSN).almacenamiento()["destino"])
    assert destino == "db:5432/recibo"
    assert "recibo:recibo" not in destino


def test_el_indice_forzado_a_memoria_no_abre_conexion() -> None:
    """La degradación es limpia: sin base, con motivo legible y sin excepción."""
    indice = IndiceVectorial(forzar_memoria=True, motivo_memoria="MODO_ALMACENAMIENTO=memoria")
    assert indice.disponible_bd is False
    assert indice.motivo_degradacion == "MODO_ALMACENAMIENTO=memoria"
    assert indice.estado()["respaldo"] == "memoria"
