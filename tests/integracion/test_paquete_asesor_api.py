"""``GET /v1/asesor/paquete/{context_ref}`` de extremo a extremo, contra la app real.

Cruza tres fronteras que la prueba de unidad simula: HTTP, el JWT de nivel
``LOA_ASESOR`` y el fichero de bitácora en disco. Es la única forma de comprobar lo que
de verdad importa aquí: que un asesor, con su propio token y sin ningún estado en
memoria compartido con el turno del cliente, obtiene el contexto completo del caso.

El recorrido es el de la demostración: el cliente pregunta por su recibo, pide una
persona, y el asesor recoge el expediente.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integracion, pytest.mark.lento]

CUENTA = "C-DEMO-01"


@pytest.fixture(scope="module")
def cliente(exige_dataset: None):
    """Aplicación real en memoria; se omite si ``apps/api`` no está disponible."""
    pytest.importorskip("fastapi.testclient", reason="fastapi no disponible")
    from fastapi.testclient import TestClient

    try:
        from apps.api.main import app  # type: ignore[import-not-found]
    except Exception as error:  # pragma: no cover - entorno incompleto
        pytest.skip(f"apps/api no disponible ({type(error).__name__}: {error})")
    with TestClient(app) as sesion:
        yield sesion


def _token(cliente, **cuerpo) -> dict[str, str]:
    """Emite un token de desarrollo y devuelve la cabecera ``Authorization``."""
    if not any(getattr(item, "path", None) == "/dev/token" for item in cliente.app.routes):
        pytest.skip("la aplicación no monta /dev/token (ENTORNO != dev)")
    respuesta = cliente.post("/dev/token", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return {"Authorization": f"Bearer {respuesta.json()['access_token']}"}


@pytest.fixture(scope="module")
def expediente(cliente) -> dict[str, str]:
    """Un caso real derivado: explicación entregada y luego petición de asesor."""
    titular = _token(cliente, cuenta_id=CUENTA, nivel="LOA2")

    explicacion = cliente.post(
        "/v1/explicar",
        json={"cuenta_id": CUENTA, "periodo": "2026-07", "utterance": "por que subio mi recibo"},
        headers=titular,
    )
    assert explicacion.status_code == 200, explicacion.text
    conversacion = explicacion.json()["conversation_id"]

    derivacion = cliente.post(
        "/v1/derivacion",
        json={
            "cuenta_id": CUENTA,
            "conversation_id": conversacion,
            "periodo": "2026-07",
            "motivo_codigo": "PETICION_HUMANO",
            "utterance": "quiero hablar con una persona",
        },
        headers=titular,
    )
    assert derivacion.status_code == 200, derivacion.text
    return {
        "context_ref": derivacion.json()["context_ref"],
        "conversation_id": conversacion,
        "trace_explicacion": explicacion.json()["trace_id"],
    }


def test_el_asesor_recibe_el_paquete_completo(cliente, expediente) -> None:
    """Con su token y el ``context_ref``, el asesor tiene todo para retomar el caso."""
    cabecera = _token(
        cliente, cuenta_id="ASESOR-01", nivel="LOA_ASESOR", acting_on_behalf_of=CUENTA
    )

    respuesta = cliente.get(
        f"/v1/asesor/paquete/{expediente['context_ref']}", headers=cabecera
    )

    assert respuesta.status_code == 200, respuesta.text
    paquete = respuesta.json()
    assert paquete["cuenta_id"] == CUENTA
    assert paquete["delta_total_cent"] is not None
    assert paquete["lineas"], "el asesor tiene que ver las líneas que componen el delta"
    assert paquete["ya_explicado"]["hubo_explicacion"], (
        "la explicación se entregó en el turno anterior del mismo caso"
    )
    assert paquete["ya_explicado"]["texto"], "sin el texto, el asesor se repetiría"
    assert paquete["motivo_codigo"] == "PETICION_HUMANO"
    assert paquete["evidencia"]["cadena_valida"] is True
    assert paquete["evidencia"]["consulta_auditoria"].startswith("GET /v1/auditoria")


def test_el_brief_que_recibe_el_asesor_viene_verificado(cliente, expediente) -> None:
    """La garantía de cero cifras sin anclar cubre también el texto que lee el asesor."""
    cabecera = _token(
        cliente, cuenta_id="ASESOR-01", nivel="LOA_ASESOR", acting_on_behalf_of=CUENTA
    )

    paquete = cliente.get(
        f"/v1/asesor/paquete/{expediente['context_ref']}", headers=cabecera
    ).json()

    verificacion = paquete["verificacion_brief"]
    assert verificacion["veredicto"] == "PASS", (
        f"cifras sin anclar en el brief: {verificacion['no_ancladas']}\n{paquete['brief']}"
    )
    assert verificacion["no_ancladas"] == []
    assert paquete["brief"].startswith("CLIENTE")


def test_el_mismo_paquete_se_sirve_en_texto_plano(cliente, expediente) -> None:
    """El canal que solo admite texto recibe el mismo contenido, sin recalcular nada."""
    cabecera = _token(
        cliente, cuenta_id="ASESOR-01", nivel="LOA_ASESOR", acting_on_behalf_of=CUENTA
    )

    respuesta = cliente.get(
        f"/v1/asesor/paquete/{expediente['context_ref']}/texto", headers=cabecera
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.headers["content-type"].startswith("text/plain")
    assert "CLIENTE" in respuesta.text
    assert "PENDIENTE" in respuesta.text


def test_pedir_el_paquete_dos_veces_devuelve_el_mismo_caso(cliente, expediente) -> None:
    """Recargar la pantalla no puede vaciar el expediente.

    Regresión: el acceso del asesor se audita, y ese evento nombraba la referencia. La
    segunda consulta se encontraba a sí misma y devolvía un paquete sin recibo.
    """
    cabecera = _token(
        cliente, cuenta_id="ASESOR-01", nivel="LOA_ASESOR", acting_on_behalf_of=CUENTA
    )
    ruta = f"/v1/asesor/paquete/{expediente['context_ref']}"

    primero = cliente.get(ruta, headers=cabecera).json()
    segundo = cliente.get(ruta, headers=cabecera).json()

    assert segundo["delta_total_cent"] == primero["delta_total_cent"]
    assert segundo["evidencia"]["trace_id"] == primero["evidencia"]["trace_id"]
    assert segundo["lineas"], "el segundo paquete no puede venir vacío"


def test_el_titular_no_puede_leer_el_paquete_del_asesor(cliente, expediente) -> None:
    """El brief es una herramienta interna: lleva confianzas, hipótesis y tareas."""
    titular = _token(cliente, cuenta_id=CUENTA, nivel="LOA2")

    respuesta = cliente.get(f"/v1/asesor/paquete/{expediente['context_ref']}", headers=titular)

    assert respuesta.status_code == 403


def test_un_asesor_no_lee_el_expediente_de_otra_cuenta(cliente, expediente) -> None:
    """Defensa en profundidad: el nivel abre la puerta, la cuenta decide la habitación."""
    cabecera = _token(
        cliente, cuenta_id="ASESOR-01", nivel="LOA_ASESOR", acting_on_behalf_of="C-DEMO-02"
    )

    respuesta = cliente.get(
        f"/v1/asesor/paquete/{expediente['context_ref']}", headers=cabecera
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "CUENTA_NO_AUTORIZADA"


def test_una_referencia_inexistente_es_un_404(cliente) -> None:
    """Nada de paquetes vacíos: si no hay expediente auditado, se dice."""
    cabecera = _token(
        cliente, cuenta_id="ASESOR-01", nivel="LOA_ASESOR", acting_on_behalf_of=CUENTA
    )

    respuesta = cliente.get("/v1/asesor/paquete/ctx-no-existe", headers=cabecera)

    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "CONTEXTO_NO_ENCONTRADO"
