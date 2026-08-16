"""E2E opt-in: nota real de Supabase -> FactSet -> explicación HTTP verificada.

Se ejecuta con ``RUN_SUPABASE_E2E=1 python -m pytest -q``. La cuenta se descubre
internamente y nunca se imprime: el reporte solo expone estados y aserciones técnicas.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integracion, pytest.mark.lento]


def _candidatos(dsn: str) -> list[tuple[str, str]]:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conexion:
        return conexion.execute(
            """
            WITH claves AS (
                SELECT DISTINCT financial_account_key AS cuenta,
                                customer_key, subscriber_key, ciclo
                FROM cargo_facturado
                WHERE subscriber_key IS NOT NULL AND grupo <> 'NO CONSIDERAR'
            ), candidatas AS (
                SELECT DISTINCT k.cuenta, n.ciclo
                FROM nota_credito AS n
                JOIN claves AS k
                  ON k.customer_key = n.receiver_customer
                 AND k.subscriber_key = n.service_receiver_id
                 AND k.ciclo = n.ciclo
                WHERE n.cancel_charge_type = 'CRD'
                  AND EXISTS (
                      SELECT 1 FROM cargo_facturado AS previo
                      WHERE previo.financial_account_key = k.cuenta
                        AND previo.ciclo < n.ciclo
                        AND previo.grupo <> 'NO CONSIDERAR'
                  )
            )
            SELECT cuenta, ciclo FROM candidatas ORDER BY ciclo DESC LIMIT 30
            """
        ).fetchall()


@pytest.mark.skipif(
    os.environ.get("RUN_SUPABASE_E2E") != "1",
    reason="requiere RUN_SUPABASE_E2E=1 y SUPABASE_DB_URL",
)
def test_nota_credito_real_llega_a_hechos_y_explicar() -> None:
    os.environ["ORIGEN_RECIBOS"] = "supabase"
    os.environ["LLM_MODE"] = "mock"
    os.environ["ORQUESTADOR"] = "directo"
    os.environ["GEMINI_EMBED_MODEL"] = ""

    from fastapi.testclient import TestClient

    from apps.api.main import app
    from apps.api.settings import obtener_ajustes
    from packages.core_domain.esquemas.factset import FactSet
    from packages.core_domain.esquemas.respuesta import RespuestaCanalAgnostica

    dsn = obtener_ajustes().supabase_db_url
    assert dsn, "SUPABASE_DB_URL no está configurada"
    resultado: tuple[
        FactSet,
        RespuestaCanalAgnostica,
        RespuestaCanalAgnostica,
    ] | None = None

    with TestClient(app) as cliente:
        for cuenta, ciclo in _candidatos(dsn):
            token = cliente.post("/dev/token", json={"cuenta_id": cuenta, "nivel": "LOA2"})
            if token.status_code != 200:
                continue
            cabecera = {"Authorization": f"Bearer {token.json()['access_token']}"}
            periodo = f"{ciclo[:4]}-{ciclo[4:6]}"
            hechos_http = cliente.get(
                "/v1/hechos",
                params={"cuenta_id": cuenta, "periodo": periodo},
                headers=cabecera,
            )
            if hechos_http.status_code != 200:
                continue
            factset = FactSet.model_validate(hechos_http.json())
            notas = [linea for linea in factset.lineas if linea.concepto_id == "NOTA_CREDITO"]
            if not notas:
                continue
            explicar_http = cliente.post(
                "/v1/explicar",
                json={
                    "cuenta_id": cuenta,
                    "periodo": periodo,
                    "utterance": "¿Por qué me vino más caro este mes?",
                    "verbosidad": "CORTO",
                    "canal": "APP",
                },
                headers=cabecera,
            )
            if explicar_http.status_code != 200:
                continue
            detalle_http = cliente.post(
                "/v1/explicar",
                json={
                    "cuenta_id": cuenta,
                    "periodo": periodo,
                    "utterance": "¿Cuáles son mis servicios y cargos facturados?",
                    "verbosidad": "CORTO",
                    "canal": "APP",
                },
                headers=cabecera,
            )
            if detalle_http.status_code != 200:
                continue
            resultado = (
                factset,
                RespuestaCanalAgnostica.model_validate(explicar_http.json()),
                RespuestaCanalAgnostica.model_validate(detalle_http.json()),
            )
            break

    assert resultado is not None, "no se encontró una nota real apta para el E2E"
    factset, respuesta, detalle = resultado
    nota = next(linea for linea in factset.lineas if linea.concepto_id == "NOTA_CREDITO")
    assert str(nota.causa) == "NOTA_CREDITO"
    assert nota.movimiento_id is not None and nota.movimiento_id < 0
    assert nota.confianza == 0.98
    assert factset.invariante.ok is True
    assert factset.invariante.residual_cent == 0
    assert str(respuesta.gobernanza.verificacion_numerica) == "PASS"
    assert respuesta.gobernanza.aserciones_no_ancladas == 0
    assert "céntim" not in respuesta.texto.lower()

    tabla = next(
        bloque
        for bloque in detalle.bloques
        if bloque.tipo == "tabla" and bloque.titulo == "Cargos de su recibo actual"
    )
    assert tabla.filas
    assert str(detalle.gobernanza.verificacion_numerica) == "PASS"
    assert detalle.gobernanza.aserciones_no_ancladas == 0
