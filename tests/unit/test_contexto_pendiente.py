"""El turno corto conserva el concepto que el propio asistente dejó pendiente."""

from packages.core_domain.enums import ModalidadRenta
from packages.core_domain.esquemas.factset import FactSet, Invariante, LineaDelta
from packages.facts_engine.confianza import Turno
from packages.facts_engine.intencion import (
    Intencion,
    clasificar_intencion,
    resolver_intencion_contextual,
)
from packages.llm_layer.generador import enfocar_resumen_consulta
from packages.llm_layer.providers.base import ExplicacionLLM


def _factset(concepto_id: str) -> FactSet:
    linea = LineaDelta(
        concepto_id=concepto_id,
        nombre_comercial=concepto_id,
        clase=LineaDelta.clasificar(1_200, 0),
        monto_actual_cent=1_200,
        monto_previo_cent=0,
        delta_cent=1_200,
        confianza=1,
    )
    return FactSet(
        factset_id="99999999-8888-7777-6666-555555555555",
        cuenta_id="C-CONTEXTO",
        modalidad_renta=ModalidadRenta.ADELANTADA,
        periodo_actual="2026-07",
        periodo_previo="2026-06",
        dias_ciclo=30,
        total_actual_cent=1_200,
        total_previo_cent=0,
        delta_total_cent=1_200,
        lineas=[linea],
        invariante=Invariante(
            ok=True,
            residual_cent=0,
            suma_deltas_cent=1_200,
            delta_total_cent=1_200,
        ),
        confianza_global=1,
        rules_version="contexto-test",
    )


def _explicacion() -> ExplicacionLLM:
    return ExplicacionLLM(
        resumen="Resumen genérico.",
        causas=[],
        siguiente_paso="VER_DETALLE",
        cifras_usadas=[],
    )


def test_pregunta_aplicada_abre_el_recibo_sin_pedir_aclaracion() -> None:
    resultado = clasificar_intencion("¿Tuve prorrateo?")

    assert resultado.intencion is Intencion.EXPLICAR_RECIBO
    assert resultado.explica_recibo is True
    assert resultado.patron == "concepto aplicado:prorrateo"


def test_si_recupera_el_concepto_del_par_de_turnos_anterior() -> None:
    historial = [
        Turno(utterance="prorrateo", rol="cliente"),
        Turno(
            utterance="¿Prefiere que se lo muestre aplicado a su último recibo?",
            rol="asistente",
        ),
    ]

    resultado = resolver_intencion_contextual("sí", historial)

    assert resultado.intencion.intencion is Intencion.EXPLICAR_RECIBO
    assert resultado.intencion.patron == "contexto pendiente:prorrateo"
    assert resultado.utterance_original == "sí"
    assert resultado.utterance_efectiva == "¿Se aplicó prorrateo en mi último recibo?"
    assert resultado.contexto_aplicado is True


def test_si_sin_concepto_pendiente_conserva_el_flujo_general() -> None:
    resultado = resolver_intencion_contextual("sí", [])

    assert resultado.intencion.intencion is Intencion.EXPLICAR_RECIBO
    assert resultado.intencion.patron == "afirmación"
    assert resultado.utterance_efectiva == "sí"
    assert resultado.contexto_aplicado is False


def test_un_concepto_antiguo_no_contamina_una_nueva_pregunta() -> None:
    historial = [
        Turno(utterance="prorrateo", rol="cliente"),
        Turno(utterance="¿Lo revisamos en su recibo?", rol="asistente"),
        Turno(utterance="gracias", rol="cliente"),
        Turno(utterance="Con gusto.", rol="asistente"),
    ]

    resultado = resolver_intencion_contextual("sí", historial)

    assert resultado.intencion.patron == "afirmación"
    assert resultado.contexto_aplicado is False


def test_respuesta_aplicada_tambien_recupera_el_concepto() -> None:
    historial = [
        Turno(utterance="nota de crédito", rol="cliente"),
        Turno(utterance="¿En general o aplicada a su recibo?", rol="asistente"),
    ]

    resultado = resolver_intencion_contextual("aplicada a mi recibo", historial)

    assert resultado.intencion.patron == "contexto pendiente:nota de crédito"
    assert "nota de crédito" in resultado.utterance_efectiva


def test_plantilla_responde_que_no_hay_prorrateo_si_el_factset_no_lo_contiene() -> None:
    enfocada = enfocar_resumen_consulta(
        _explicacion(),
        _factset("NOTA_CREDITO"),
        "¿Se aplicó prorrateo en mi último recibo?",
    )

    assert enfocada.resumen.startswith("No identifico prorrateo")


def test_plantilla_confirma_prorrateo_solo_si_el_factset_lo_contiene() -> None:
    enfocada = enfocar_resumen_consulta(
        _explicacion(),
        _factset("PRORRATEO_PLAN"),
        "¿Se aplicó prorrateo en mi último recibo?",
    )

    assert enfocada.resumen == "Sí. En el recibo consultado aparece prorrateo."
