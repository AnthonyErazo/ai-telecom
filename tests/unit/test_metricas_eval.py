"""El evaluador también se evalúa (``eval/metricas.py``).

Una métrica rota es peor que no tener métrica: reporta 100 % y nadie mira más. Estas
pruebas construyen observaciones sintéticas con el resultado conocido de antemano y
comprueban que las tres métricas oficiales **detectan el fallo cuando lo hay**:

* que una respuesta con un solo campo mal no cuente como exacta (capa C);
* que ``TA_respuesta`` cuente respuestas y no aserciones, que es lo que la hace exigente;
* que el hand-off distinga falso negativo de falso positivo y que ``F2`` castigue más el
  primero, coherentemente con la asimetría del daño.
"""

from __future__ import annotations

import pytest

from eval.metricas import (
    ADVERTENCIA_CIRCULARIDAD,
    CAMPOS_HANDOFF,
    CampoRecuperacion,
    ObservacionCaso,
    agregar,
    metricas_alucinacion,
    metricas_apoyo,
    metricas_handoff,
    metricas_recuperacion,
)
from packages.core_domain.enums import (
    Canal,
    ModalidadRenta,
    ModoGeneracion,
    Verbosidad,
    VeredictoVerificacion,
)


def observacion(
    caso_id: str,
    *,
    campos: list[CampoRecuperacion] | None = None,
    recall_at_1: bool | None = True,
    aserciones: int = 10,
    no_ancladas: int = 0,
    veredicto: VeredictoVerificacion = VeredictoVerificacion.PASS,
    prohibidos: tuple[str, ...] = (),
    debe_derivar: bool = False,
    derivo: bool = False,
    turnos: int | None = None,
    campos_handoff: int = 0,
    residual_cent: int = 0,
    causas_acertadas: int = 1,
    causas_evaluadas: int = 1,
    modo: ModoGeneracion = ModoGeneracion.LLM,
    escenarios: tuple[str, ...] = ("CAMBIO_PLAN_MEDIO_CICLO",),
) -> ObservacionCaso:
    """Observación sintética con todo lo que las métricas necesitan."""
    if campos is None:
        campos = [CampoRecuperacion("delta_total_cent", 100, 100, "prueba")]
    return ObservacionCaso(
        caso_id=caso_id,
        cuenta_id="C-TEST",
        periodo="2026-07",
        escenarios=escenarios,
        modalidad_renta=ModalidadRenta.VENCIDA,
        verbosidad=Verbosidad.CORTO,
        canal=Canal.APP,
        campos=tuple(campos),
        recall_at_1=recall_at_1,
        aserciones_totales=aserciones,
        aserciones_no_ancladas=no_ancladas,
        veredicto=veredicto,
        fragmentos_prohibidos=prohibidos,
        debe_derivar=debe_derivar,
        derivo=derivo,
        motivo_derivacion="PETICION_HUMANO" if derivo else None,
        turnos_hasta_derivar=turnos,
        handoff_campos_presentes=campos_handoff,
        handoff_campos_totales=len(CAMPOS_HANDOFF),
        residual_cent=residual_cent,
        causas_acertadas=causas_acertadas,
        causas_evaluadas=causas_evaluadas,
        modo_generacion=modo,
        latencia_ms=10,
    )


# --------------------------------------------------------------------------- #
# 1. Precisión de Recuperación
# --------------------------------------------------------------------------- #
class TestPrecisionRecuperacion:
    """Tres capas, y la que manda es la más estricta."""

    def test_un_campo_mal_tumba_la_respuesta_entera(self) -> None:
        """Capa (C): ``RA_field = 0.75`` no es "casi exacta", es **no exacta**."""
        campos = [
            CampoRecuperacion("total_actual_cent", 21_637, 21_637, "golden"),
            CampoRecuperacion("total_previo_cent", 19_555, 19_555, "golden"),
            CampoRecuperacion("delta_total_cent", 2_082, 2_082, "golden"),
            CampoRecuperacion("delta_cent:IGV", 318, 319, "gt"),  # un céntimo de más
        ]
        casi = observacion("casi", campos=campos)

        assert casi.ra_field == pytest.approx(0.75)
        assert casi.exacta is False

        resumen = metricas_recuperacion([casi])
        assert resumen.strict == 0.0
        assert resumen.ra_field_micro == pytest.approx(0.75)
        assert resumen.campos_fallidos == (("casi", "delta_cent:IGV", 318, 319),)

    def test_un_centimo_de_diferencia_ya_es_un_fallo(self) -> None:
        """En céntimos enteros no existe la tolerancia: o es el dato exacto o no lo es."""
        assert CampoRecuperacion("x", 12_490, 12_490, "gt").correcto is True
        assert CampoRecuperacion("x", 12_490, 12_491, "gt").correcto is False
        assert CampoRecuperacion("x", 12_490, 12_491, "gt").desvio_cent == 1

    def test_micro_y_macro_difieren_cuando_los_casos_pesan_distinto(self) -> None:
        """El macro por escenario impide que un escenario con muchos campos domine."""
        gordo = observacion(
            "gordo",
            escenarios=("CAMBIO_PLAN_MEDIO_CICLO",),
            campos=[CampoRecuperacion(f"c{i}", 1, 1, "gt") for i in range(9)],
        )
        flaco = observacion(
            "flaco",
            escenarios=("ESTABLE",),
            campos=[CampoRecuperacion("c0", 1, 2, "gt")],  # único campo, y falla
        )
        resumen = metricas_recuperacion([gordo, flaco])

        assert resumen.ra_field_micro == pytest.approx(9 / 10)
        assert resumen.ra_field_macro == pytest.approx(0.5)  # (1.0 + 0.0) / 2
        assert resumen.ra_field_por_escenario["ESTABLE"] == 0.0
        assert resumen.casos_por_escenario["CAMBIO_PLAN_MEDIO_CICLO"] == 1

    def test_recall_at_1_excluye_los_casos_sin_documento_correcto(self) -> None:
        """Un recibo sin variación no tiene documento "correcto" que recuperar."""
        resumen = metricas_recuperacion(
            [
                observacion("a", recall_at_1=True),
                observacion("b", recall_at_1=False),
                observacion("estable", recall_at_1=None),
            ]
        )
        assert resumen.recall_at_1_evaluados == 2
        assert resumen.recall_at_1 == pytest.approx(0.5)

    def test_sin_casos_no_se_inventa_un_cien_por_ciento(self) -> None:
        resumen = metricas_recuperacion([])
        assert resumen.strict == 0.0
        assert resumen.ra_field_micro == 0.0
        assert resumen.recall_at_1 is None


# --------------------------------------------------------------------------- #
# 2. Tasa de Alucinación
# --------------------------------------------------------------------------- #
class TestTasaAlucinacion:
    """``TA_respuesta`` es la comprometida porque ``TA_asercion`` diluye."""

    def test_una_sola_cifra_inventada_contamina_toda_la_respuesta(self) -> None:
        resumen = metricas_alucinacion(
            [
                observacion("limpia", aserciones=40, no_ancladas=0),
                observacion(
                    "sucia",
                    aserciones=40,
                    no_ancladas=1,
                    veredicto=VeredictoVerificacion.FAIL,
                ),
            ]
        )
        # La media por aserción parece inocua...
        assert resumen.ta_asercion == pytest.approx(1 / 80)
        # ...pero la mitad de las respuestas son inservibles.
        assert resumen.ta_respuesta == pytest.approx(0.5)
        assert resumen.respuestas_con_alucinacion == 1
        assert resumen.compromiso_cumplido is False

    def test_el_veredicto_fail_cuenta_aunque_no_haya_no_ancladas(self) -> None:
        """Un error estructural (``Σ causas != delta``) también invalida la respuesta."""
        resumen = metricas_alucinacion(
            [observacion("estructural", no_ancladas=0, veredicto=VeredictoVerificacion.FAIL)]
        )
        assert resumen.ta_respuesta == 1.0
        assert resumen.compromiso_cumplido is False

    def test_los_fragmentos_prohibidos_se_cuentan_aparte(self) -> None:
        """Una fuga de contenido no es una alucinación numérica, pero también incumple."""
        resumen = metricas_alucinacion(
            [observacion("inyeccion", no_ancladas=0, prohibidos=("C-00002",))]
        )
        assert resumen.ta_respuesta == 0.0
        assert resumen.respuestas_con_fragmento_prohibido == 1
        assert resumen.fragmentos_detectados == (("inyeccion", "C-00002"),)
        assert resumen.compromiso_cumplido is False

    def test_todo_limpio_cumple_el_compromiso(self) -> None:
        resumen = metricas_alucinacion([observacion("a"), observacion("b")])
        assert resumen.ta_respuesta == 0.0
        assert resumen.ta_asercion == 0.0
        assert resumen.compromiso_cumplido is True
        assert resumen.veredictos == {"PASS": 2}


# --------------------------------------------------------------------------- #
# 3. Precisión del Hand-off
# --------------------------------------------------------------------------- #
class TestPrecisionHandoff:
    """Errores asimétricos: el falso negativo pesa el doble."""

    @pytest.fixture
    def matriz(self) -> list[ObservacionCaso]:
        """VP 2 · FN 1 · FP 1 · VN 4."""
        return [
            observacion("vp1", debe_derivar=True, derivo=True, turnos=1, campos_handoff=7),
            observacion("vp2", debe_derivar=True, derivo=True, turnos=3, campos_handoff=7),
            observacion("fn1", debe_derivar=True, derivo=False, turnos=None),
            observacion("fp1", debe_derivar=False, derivo=True, campos_handoff=5),
            *[observacion(f"vn{i}", debe_derivar=False, derivo=False) for i in range(4)],
        ]

    def test_la_matriz_de_confusion(self, matriz) -> None:
        resumen = metricas_handoff(matriz)
        assert (resumen.verdaderos_positivos, resumen.falsos_negativos) == (2, 1)
        assert (resumen.falsos_positivos, resumen.verdaderos_negativos) == (1, 4)

    def test_recall_precision_y_atrapamiento(self, matriz) -> None:
        resumen = metricas_handoff(matriz)
        assert resumen.recall == pytest.approx(2 / 3)
        assert resumen.precision == pytest.approx(2 / 3)
        assert resumen.tasa_atrapamiento == pytest.approx(1 / 5)
        assert resumen.exactitud == pytest.approx(6 / 8)
        assert resumen.primaria == resumen.recall

    def test_f2_castiga_mas_el_falso_negativo_que_el_falso_positivo(self) -> None:
        """Con el mismo número de errores, perder una derivación duele más."""
        con_falso_negativo = metricas_handoff(
            [
                observacion("vp", debe_derivar=True, derivo=True),
                observacion("fn", debe_derivar=True, derivo=False),
                observacion("vn", debe_derivar=False, derivo=False),
                observacion("vn2", debe_derivar=False, derivo=False),
            ]
        )
        con_falso_positivo = metricas_handoff(
            [
                observacion("vp", debe_derivar=True, derivo=True),
                observacion("vp2", debe_derivar=True, derivo=True),
                observacion("fp", debe_derivar=False, derivo=True),
                observacion("vn", debe_derivar=False, derivo=False),
            ]
        )
        assert con_falso_negativo.f2 < con_falso_positivo.f2

    def test_la_mediana_de_turnos_solo_mira_las_derivaciones_debidas(self, matriz) -> None:
        resumen = metricas_handoff(matriz)
        assert resumen.turnos_observados == (1, 3)
        assert resumen.mediana_turnos_hasta_derivar == pytest.approx(2.0)

    def test_la_completitud_promedia_sobre_las_derivaciones_reales(self, matriz) -> None:
        """Siete campos por derivación: 7 + 7 + 5 sobre 21 posibles."""
        resumen = metricas_handoff(matriz)
        assert resumen.campos_esperados == 3 * len(CAMPOS_HANDOFF)
        assert resumen.campos_presentes == 19
        assert resumen.completitud == pytest.approx(19 / 21)

    def test_los_casos_problematicos_se_nombran(self, matriz) -> None:
        """Una métrica sin el detalle de qué falló no sirve para arreglar nada."""
        resumen = metricas_handoff(matriz)
        assert resumen.casos_no_derivados_que_debian == ("fn1",)
        assert resumen.casos_derivados_de_mas == ("fp1",)

    def test_el_payload_tiene_exactamente_siete_campos(self) -> None:
        assert len(CAMPOS_HANDOFF) == 7
        assert set(CAMPOS_HANDOFF) == {
            "requerida",
            "motivo",
            "motivo_codigo",
            "context_ref",
            "resumen_asesor",
            "senal_disparadora",
            "score_incomprension",
        }


# --------------------------------------------------------------------------- #
# Métricas de apoyo e informe
# --------------------------------------------------------------------------- #
class TestApoyo:
    """Residual, causa raíz y fallback."""

    def test_el_residual_se_promedia_en_valor_absoluto(self) -> None:
        resumen = metricas_apoyo(
            [
                observacion("a", residual_cent=0),
                observacion("b", residual_cent=-2),
                observacion("c", residual_cent=4),
            ]
        )
        assert resumen.residual_medio_cent == pytest.approx(2.0)
        assert resumen.residual_maximo_cent == 4
        assert resumen.casos_con_invariante_roto == 2  # |residual| > 1

    def test_precision_causa_raiz_agrega_por_concepto(self) -> None:
        resumen = metricas_apoyo(
            [
                observacion("a", causas_acertadas=3, causas_evaluadas=3),
                observacion("b", causas_acertadas=1, causas_evaluadas=2),
            ]
        )
        assert resumen.precision_causa_raiz == pytest.approx(4 / 5)

    def test_tasa_fallback_cuenta_las_plantillas(self) -> None:
        resumen = metricas_apoyo(
            [
                observacion("a", modo=ModoGeneracion.LLM),
                observacion("b", modo=ModoGeneracion.LLM_REINTENTO),
                observacion("c", modo=ModoGeneracion.PLANTILLA),
                observacion("d", modo=ModoGeneracion.PLANTILLA),
            ]
        )
        assert resumen.tasa_fallback == pytest.approx(0.5)
        assert resumen.modos == {"LLM": 1, "LLM_REINTENTO": 1, "PLANTILLA": 2}


class TestInforme:
    """El veredicto global y la advertencia que lo acompaña."""

    def test_una_suite_limpia_aprueba(self) -> None:
        informe = agregar([observacion("a"), observacion("b")], modo="mock")
        assert informe.aprobado is True

    def test_una_alucinacion_tumba_el_informe(self) -> None:
        informe = agregar([observacion("a"), observacion("mala", no_ancladas=1)], modo="mock")
        assert informe.aprobado is False

    def test_un_falso_negativo_de_handoff_tumba_el_informe(self) -> None:
        """Aunque no haya ni una cifra mal: dejar al cliente atrapado es el daño grave."""
        informe = agregar([observacion("fn", debe_derivar=True, derivo=False)], modo="mock")
        assert informe.handoff.falsos_negativos == 1
        assert informe.aprobado is False

    def test_un_invariante_roto_tumba_el_informe(self) -> None:
        informe = agregar([observacion("descuadre", residual_cent=15)], modo="mock")
        assert informe.aprobado is False

    def test_la_advertencia_de_circularidad_viaja_en_el_informe(self) -> None:
        """Requisito de la sección 10: la salida tiene que declararla siempre."""
        informe = agregar([observacion("a")], modo="mock")
        datos = informe.a_dict()

        assert informe.advertencia == ADVERTENCIA_CIRCULARIDAD
        assert "CIRCULARIDAD" in datos["advertencia_circularidad"]
        assert "COMPARTEN AUTOR" in datos["advertencia_circularidad"]
        assert "no demuestran" in datos["advertencia_circularidad"].lower()
        assert "datos reales" in datos["advertencia_circularidad"].lower()

    def test_el_informe_serializa_todo_lo_necesario_para_auditarlo(self) -> None:
        informe = agregar([observacion("a"), observacion("b")], modo="mock")
        datos = informe.a_dict()

        assert set(datos) >= {
            "precision_recuperacion",
            "tasa_alucinacion",
            "precision_handoff",
            "apoyo",
            "detalle_por_caso",
            "advertencia_circularidad",
        }
        assert len(datos["detalle_por_caso"]) == 2
        assert datos["precision_recuperacion"]["strict_answer_accuracy"] == 1.0
