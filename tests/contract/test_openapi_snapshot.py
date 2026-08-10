"""Snapshot versionado del OpenAPI y del esquema de salida del LLM (sección 11).

Un contrato de API solo sirve si **cambiarlo cuesta**. Estos tests congelan dos
superficies y hacen que cualquier modificación tenga que pasar por una revisión
explícita del snapshot:

* ``tests/contract/openapi.snapshot.json`` — la API HTTP que consumen la App Mi
  Movistar, el Bot Lucía y WhatsApp. Si aún no existe ``apps/api``, el test se **omite**
  con un motivo legible en vez de fallar: el contrato no puede exigir lo que todavía no
  se ha escrito, pero en cuanto exista quedará congelado.
* ``tests/contract/explicacion_v1.snapshot.json`` — el JSON Schema con el que se le pide
  la salida al modelo generativo. Cambiarlo altera lo que el LLM puede decir y lo que el
  verificador espera recibir, así que no puede moverse por accidente.

Para regenerar un snapshot tras un cambio deliberado::

    ACTUALIZAR_SNAPSHOTS=1 pytest tests/contract -q

y **revisar el diff antes de comprometerlo**. Ese diff es la lista de cambios que hay
que comunicar a los canales.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.contrato

DIRECTORIO = Path(__file__).resolve().parent
SNAPSHOT_OPENAPI = DIRECTORIO / "openapi.snapshot.json"
SNAPSHOT_EXPLICACION = DIRECTORIO / "explicacion_v1.snapshot.json"

#: Rutas mínimas que la especificación (sección 9) exige a la API.
RUTAS_EXIGIDAS = (
    "/salud",
    "/v1/hechos",
    "/v1/explicar",
    "/v1/derivacion",
    "/v1/auditoria",
)


def _actualizar() -> bool:
    """``True`` si se pidió regenerar los snapshots."""
    return os.getenv("ACTUALIZAR_SNAPSHOTS", "").strip().lower() in {"1", "true", "si", "sí"}


def _canonico(datos: Any) -> str:
    """Serialización estable: mismo contenido ⇒ mismo texto, siempre."""
    return json.dumps(datos, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _comparar_con_snapshot(datos: Any, ruta: Path, nombre: str) -> None:
    """Compara contra el snapshot; lo crea la primera vez y omite con instrucciones."""
    actual = _canonico(datos)

    if _actualizar() or not ruta.exists():
        ruta.write_text(actual, encoding="utf-8")
        if not _actualizar():
            pytest.skip(
                f"snapshot de {nombre} creado en {ruta.name}: revíselo, cométalo y vuelva "
                "a ejecutar la suite para que quede congelado"
            )
        return

    esperado = ruta.read_text(encoding="utf-8")
    assert actual == esperado, (
        f"el contrato de {nombre} cambió respecto de {ruta.name}.\n"
        "Si el cambio es intencionado, regenere con "
        "`ACTUALIZAR_SNAPSHOTS=1 pytest tests/contract -q`, revise el diff y avise a los "
        "canales que consumen la API."
    )


def _aplicacion():
    """Devuelve la app de FastAPI, u omite el test si todavía no existe."""
    try:
        from apps.api.main import app  # type: ignore[import-not-found]
    except Exception as error:
        pytest.skip(f"apps/api todavía no está disponible ({type(error).__name__}: {error})")
    return app


#: Prefijos que **no** forman parte del contrato con los canales y que además solo
#: existen con ``ENTORNO=dev``. Incluirlos en el snapshot lo haría depender del entorno
#: en el que se ejecuta la suite, que es justo lo que un snapshot no puede permitirse.
PREFIJOS_NO_CONTRACTUALES = ("/dev",)


def _superficie_publica(documento: dict[str, Any]) -> dict[str, Any]:
    """Documento OpenAPI sin los endpoints de desarrollo."""
    rutas = {
        ruta: definicion
        for ruta, definicion in documento.get("paths", {}).items()
        if not ruta.startswith(PREFIJOS_NO_CONTRACTUALES)
    }
    return {**documento, "paths": rutas}


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_el_openapi_coincide_con_el_snapshot() -> None:
    """El contrato HTTP no cambia sin que alguien revise el diff.

    Se congela solo la **superficie pública** (``/salud`` y ``/v1/*``): los endpoints de
    ``/dev`` son andamiaje de pruebas y no viajan a ningún canal.
    """
    documento = _superficie_publica(_aplicacion().openapi())
    _comparar_con_snapshot(documento, SNAPSHOT_OPENAPI, "OpenAPI")


def test_el_openapi_expone_las_rutas_de_la_especificacion() -> None:
    """Sección 9: las rutas que los canales necesitan tienen que estar publicadas."""
    documento = _aplicacion().openapi()
    rutas = set(documento.get("paths", {}))

    faltantes = [
        ruta
        for ruta in RUTAS_EXIGIDAS
        if ruta not in rutas and not any(publicada.startswith(ruta) for publicada in rutas)
    ]
    assert faltantes == [], f"faltan rutas de la especificación: {faltantes}"


def test_el_openapi_declara_la_respuesta_canal_agnostica() -> None:
    """``POST /v1/explicar`` tiene que documentar el esquema que devuelve."""
    documento = _aplicacion().openapi()
    esquemas = documento.get("components", {}).get("schemas", {})
    assert "RespuestaCanalAgnostica" in esquemas, (
        "la respuesta principal debe aparecer en components.schemas para que los "
        "canales puedan generar sus tipos"
    )


def test_el_openapi_documenta_el_error_de_invariante() -> None:
    """El 409 ``INVARIANTE_FALLIDO`` es parte del contrato, no un detalle interno."""
    documento = _aplicacion().openapi()
    hechos = documento.get("paths", {}).get("/v1/hechos", {}).get("get", {})
    respuestas = set(hechos.get("responses", {}))
    assert "409" in respuestas, (
        "GET /v1/hechos debe documentar el 409 INVARIANTE_FALLIDO: es la señal de que "
        "el recibo no concilia y hay que derivar"
    )


# --------------------------------------------------------------------------- #
# Esquema de salida del LLM
# --------------------------------------------------------------------------- #
def test_el_esquema_explicacion_v1_coincide_con_el_snapshot() -> None:
    """``explicacion_v1`` fija qué puede decir el modelo y qué audita el verificador."""
    from packages.llm_layer.providers.base import ESQUEMA_EXPLICACION_V1

    _comparar_con_snapshot(ESQUEMA_EXPLICACION_V1, SNAPSHOT_EXPLICACION, "explicacion_v1")


def test_el_esquema_explicacion_v1_pide_los_montos_como_enteros() -> None:
    """*"Pedir ``monto_cent_citado`` como entero hace trivial el verificador"* (5.2).

    Si este campo fuera un texto o un número decimal, el verificador tendría que
    interpretar la escritura del modelo antes de poder compararla, y ahí es donde se
    cuelan los errores.
    """
    from packages.llm_layer.providers.base import ESQUEMA_EXPLICACION_V1

    propiedades = ESQUEMA_EXPLICACION_V1["properties"]
    assert set(propiedades) >= {"resumen", "causas", "siguiente_paso", "cifras_usadas"}

    causa = propiedades["causas"]["items"]["properties"]
    assert causa["monto_cent_citado"]["type"] == "integer"
    assert propiedades["cifras_usadas"]["items"]["type"] == "integer"


def test_el_esquema_explicacion_v1_limita_la_longitud_del_resumen() -> None:
    """``resumen <= 180`` caracteres: cabe en una tarjeta de la App y en un turno del Bot."""
    from packages.llm_layer.providers.base import ESQUEMA_EXPLICACION_V1

    assert ESQUEMA_EXPLICACION_V1["properties"]["resumen"]["maxLength"] == 180
