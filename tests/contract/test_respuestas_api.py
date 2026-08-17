"""Contrato vivo: lo que la API devuelve por HTTP valida contra su esquema (sección 11).

``test_esquemas_respuesta.py`` prueba los modelos; esto prueba **el cable**. Se levanta
la aplicación en memoria, se pide un token de desarrollo y se recorren los endpoints de
la sección 9 comprobando dos cosas por respuesta: que el cuerpo valida contra el modelo
Pydantic declarado y que se cumplen las invariantes de gobernanza y de nivel de
aseguramiento.

Todo el módulo se **omite** si ``apps/api`` aún no está disponible, para que la suite de
motor y verificador siga corriendo mientras la API se termina.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import RespuestaCanalAgnostica

pytestmark = pytest.mark.contrato


@pytest.fixture(scope="module")
def cliente(exige_dataset: None):
    """Cliente HTTP en memoria contra la aplicación real.

    ``ENTORNO=dev`` lo fija ``conftest.pytest_configure`` antes de que nadie importe la
    aplicación: los ajustes se resuelven una sola vez y, si el entorno no fuera ``dev``,
    el router ``/dev`` no se montaría y estas pruebas se saltarían sin avisar.
    """
    pytest.importorskip("fastapi.testclient", reason="fastapi no disponible")
    from fastapi.testclient import TestClient

    try:
        from apps.api.main import app  # type: ignore[import-not-found]
    except Exception as error:
        pytest.skip(f"apps/api no disponible ({type(error).__name__}: {error})")
    with TestClient(app) as sesion:
        yield sesion


def _ruta_registrada(cliente, ruta: str) -> bool:
    """``True`` si la aplicación tiene esa ruta montada."""
    return any(getattr(item, "path", None) == ruta for item in cliente.app.routes)


def _cabecera(cliente, nivel: str = "LOA2", cuenta_id: str = "C-DEMO-01") -> dict[str, str]:
    """Emite un token de desarrollo del nivel pedido y arma la cabecera Bearer.

    Solo se omite si el endpoint **no existe** en la aplicación. Si existe y falla, el
    test **falla**: un 404 aquí significa que la app se construyó con otro entorno y que
    toda la suite de contrato se estaría saltando en silencio, que es peor que un rojo.
    """
    if not _ruta_registrada(cliente, "/dev/token"):
        pytest.skip("la aplicación no monta /dev/token (ENTORNO != dev)")

    respuesta = cliente.post("/dev/token", json={"cuenta_id": cuenta_id, "nivel": nivel})
    assert respuesta.status_code == 200, (
        f"POST /dev/token devolvió {respuesta.status_code}: {respuesta.text[:300]}"
    )
    return {"Authorization": f"Bearer {respuesta.json()['access_token']}"}


def _valida_json_schema(instancia: Any, esquema: dict[str, Any]) -> None:
    """Refuerzo con ``jsonschema`` cuando está instalado."""
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema no instalado")
    jsonschema.validate(instance=instancia, schema=esquema)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def test_salud_responde(cliente) -> None:
    """``GET /salud`` es el liveness del contenedor: sin token y sin base de datos."""
    respuesta = cliente.get("/salud")
    assert respuesta.status_code == 200
    assert isinstance(respuesta.json(), dict)


def test_hechos_devuelve_un_factset_valido_y_sellado(cliente) -> None:
    """``GET /v1/hechos`` devuelve el FactSet, con su SHA-256 verificable."""
    respuesta = cliente.get(
        "/v1/hechos",
        params={"cuenta_id": "C-DEMO-01", "periodo": "2026-07"},
        headers=_cabecera(cliente),
    )
    assert respuesta.status_code == 200, respuesta.text

    cuerpo = respuesta.json()
    factset = FactSet.model_validate(cuerpo)
    assert factset.verificar_sha256() is True
    assert factset.invariante.ok is True
    _valida_json_schema(cuerpo, FactSet.model_json_schema())


def test_explicar_devuelve_la_respuesta_canal_agnostica(cliente) -> None:
    """``POST /v1/explicar`` valida contra ``RespuestaCanalAgnostica`` y llega anclada."""
    respuesta = cliente.post(
        "/v1/explicar",
        json={
            "cuenta_id": "C-DEMO-01",
            "periodo": "2026-07",
            "utterance": "¿por qué me vino más caro este mes?",
            "verbosidad": "CORTO",
            "canal": "APP",
        },
        headers=_cabecera(cliente),
    )
    assert respuesta.status_code == 200, respuesta.text

    cuerpo = respuesta.json()
    modelo = RespuestaCanalAgnostica.model_validate(cuerpo)
    _valida_json_schema(cuerpo, RespuestaCanalAgnostica.model_json_schema())

    assert modelo.gobernanza.verificacion_numerica == "PASS"
    assert modelo.gobernanza.aserciones_no_ancladas == 0
    assert modelo.gobernanza.anclado is True
    assert modelo.bloques and modelo.acciones
    assert modelo.telemetria.get("silence_probe_id")


def test_la_respuesta_no_contiene_cifras_fuera_del_factset(cliente) -> None:
    """La garantía del proyecto tiene que sobrevivir al viaje por HTTP.

    Es el mismo test golden, pero sobre el cuerpo que sale por el socket: si un
    serializador reformateara un importe o la API añadiera una cifra propia, aquí se ve.
    """
    from packages.llm_layer import extraer_numeros

    cabecera = _cabecera(cliente)
    hechos = cliente.get(
        "/v1/hechos", params={"cuenta_id": "C-DEMO-01", "periodo": "2026-07"}, headers=cabecera
    )
    factset = FactSet.model_validate(hechos.json())

    explicacion = cliente.post(
        "/v1/explicar",
        json={
            "cuenta_id": "C-DEMO-01",
            "periodo": "2026-07",
            "utterance": "¿por qué me vino más caro este mes?",
            "verbosidad": "DETALLE",
            "canal": "APP",
        },
        headers=cabecera,
    )
    modelo = RespuestaCanalAgnostica.model_validate(explicacion.json())

    infractores = extraer_numeros(modelo.texto) - factset.tokens_permitidos()
    assert infractores == set(), f"Alucinación numérica en la respuesta HTTP: {infractores}"


def test_sin_token_no_se_entrega_informacion_de_la_cuenta(cliente) -> None:
    """*"No mostrar información sensible sin autenticación"* (ficha B.9)."""
    respuesta = cliente.get("/v1/hechos", params={"cuenta_id": "C-DEMO-01", "periodo": "2026-07"})
    assert respuesta.status_code in {401, 403}, respuesta.text


def test_el_nivel_loa1_no_entrega_importes(cliente) -> None:
    """Matriz de la sección 9: LOA1 (WhatsApp) conoce la dirección del cambio, no el monto."""
    respuesta = cliente.post(
        "/v1/explicar",
        json={
            "cuenta_id": "C-DEMO-01",
            "periodo": "2026-07",
            "utterance": "¿por qué me vino más caro?",
            "canal": "WHATSAPP",
        },
        headers=_cabecera(cliente, nivel="LOA1"),
    )
    if respuesta.status_code in {401, 403}:
        pytest.skip("la política de LOA1 rechaza el endpoint en vez de degradar la respuesta")
    assert respuesta.status_code == 200, respuesta.text

    modelo = RespuestaCanalAgnostica.model_validate(respuesta.json())
    assert "S/" not in modelo.texto, (
        "en LOA1 no puede viajar ningún importe: solo existencia y dirección del cambio"
    )
    # La garantía dura del nivel, la que promete la sección 6.4 del README y comprueba
    # `scripts/probar_e2e.py` paso 13: ni un dígito. Se afirma aquí además de allí porque
    # el aviso de LOA1 lleva una URL, y una URL es exactamente la clase de texto donde un
    # dígito se cuela sin que nadie lo note.
    assert not any(caracter.isdigit() for caracter in modelo.texto), (
        f"LOA1 entregó un dígito: {modelo.texto!r}"
    )


def test_loa1_dice_donde_esta_la_app_que_recomienda(cliente) -> None:
    """Si el canal manda al cliente a la App, tiene que decirle dónde está.

    En ``LOA1`` la única salida que se le ofrece al cliente es «ingrese a la App Mi
    Movistar». Sin el enlace, esa instrucción es un callejón: el cliente está en WhatsApp
    y no sabe qué app es ni de dónde bajarla. Se comprueba que el enlace viaja y que es
    **el de la ficha oficial**, no una URL cualquiera.
    """
    from apps.api.security import URL_APP_MI_MOVISTAR

    respuesta = cliente.post(
        "/v1/explicar",
        json={
            "cuenta_id": "C-DEMO-01",
            "periodo": "2026-07",
            "utterance": "¿por qué me vino más caro?",
            "canal": "WHATSAPP",
        },
        headers=_cabecera(cliente, nivel="LOA1"),
    )
    if respuesta.status_code in {401, 403}:
        pytest.skip("la política de LOA1 rechaza el endpoint en vez de degradar la respuesta")
    assert respuesta.status_code == 200, respuesta.text

    modelo = RespuestaCanalAgnostica.model_validate(respuesta.json())
    assert URL_APP_MI_MOVISTAR in modelo.texto, (
        "el aviso de LOA1 manda a la App Mi Movistar: tiene que llevar su ficha de Google Play"
    )
    assert "tdp.app.col" in URL_APP_MI_MOVISTAR, "el paquete de la App Mi Movistar es tdp.app.col"
    assert not any(caracter.isdigit() for caracter in URL_APP_MI_MOVISTAR), (
        "la URL de la ficha entra en un texto LOA1: no puede llevar dígitos "
        "(por eso se omite el parámetro &pli=1 de la URL que copia el navegador)"
    )


def test_el_error_declara_su_codigo(cliente) -> None:
    """Un error de la API es un ``RespuestaError`` con código, no un texto suelto."""
    from packages.core_domain.esquemas.respuesta import RespuestaError

    respuesta = cliente.get(
        "/v1/hechos",
        params={"cuenta_id": "C-NO-EXISTE", "periodo": "2026-07"},
        headers=_cabecera(cliente, cuenta_id="C-NO-EXISTE"),
    )
    assert respuesta.status_code >= 400

    cuerpo = respuesta.json()
    detalle = cuerpo.get("detail", cuerpo)
    if not isinstance(detalle, dict) or "codigo" not in detalle:
        pytest.skip(f"la API aún no normaliza este error: {json.dumps(cuerpo)[:200]}")
    RespuestaError.model_validate(detalle)
