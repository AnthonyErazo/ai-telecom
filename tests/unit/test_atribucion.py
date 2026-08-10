"""Atribución de causa y confianza (sección 4.7).

La tabla ``regla_concepto_causa`` decide qué movimientos **pueden** explicar cada
concepto; la ventana del ciclo decide cuáles **están disponibles**. De ahí salen las
tres ramas de confianza, y la que importa de verdad es la tercera:

* 1 candidato   → ``causa``, confianza 0.98
* 0 candidatos  → ``causa = None``, confianza 0.30
* >1 candidato  → el más reciente, confianza **0.65**, y los descartados quedan en la
  evidencia

La rama ambigua es la razón de ser de este módulo. Con dos cambios de plan en el mismo
ciclo, una atribución ingenua elegiría uno y lo contaría como certeza; aquí se elige el
más cercano en el tiempo, se **baja la confianza** y se deja constancia del otro, que es
lo que permite al umbral de incomprensión decidir si esto se explica o se deriva.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from packages.core_domain.enums import ClaseDelta, FamiliaConcepto, TipoMovimiento
from packages.core_domain.esquemas.factset import LineaDelta
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.esquemas.recibo import Tramo
from packages.facts_engine.atribucion import (
    CONCEPTOS_DERIVADOS,
    EVIDENCIA_DERIVADO,
    EVIDENCIA_PREFERENCIA,
    EVIDENCIA_REGLA,
    atribuir,
    candidatos_para,
    elegir_candidato,
    esta_atribuida,
)


def delta_renta(
    *, actual: int = 12_000, previo: int = 10_000, tramos: list[Tramo] | None = None
) -> LineaDelta:
    """Variación de la renta del plan móvil, el concepto prorrateable por excelencia."""
    return LineaDelta(
        concepto_id="RENTA_PLAN_MOVIL",
        nombre_comercial="Plan móvil",
        clase=LineaDelta.clasificar(actual, previo),
        monto_actual_cent=actual,
        monto_previo_cent=previo,
        delta_cent=actual - previo,
        confianza=1.0,
        familia=FamiliaConcepto.RECURRENTE,
        tramos=tramos,
    )


def cambio_plan(movimiento_id: int, dia: int) -> MovementEvent:
    """Cambio de plan el día indicado del ciclo de julio de 2026."""
    return MovementEvent(
        movimiento_id=movimiento_id,
        cuenta_id="C-TEST",
        tipo=TipoMovimiento.CAMBIO_PLAN,
        ocurrido_en=datetime(2026, 7, dia, 12, 0),
        detalle={
            "plan_anterior": "A",
            "plan_nuevo": "B",
            "tarifa_anterior_cent": 10_000,
            "tarifa_nueva_cent": 12_000,
        },
    )


def delta_descuento(*, actual: int, previo: int = -4_990) -> LineaDelta:
    """Variación de un descuento promocional (los créditos van en negativo).

    ``actual = 0`` es la promoción que se cae entera (DESAPARECIDO); un ``actual``
    negativo pero menor en valor absoluto es la que se prorratea porque venció a mitad
    de ciclo (SUBIO); un ``actual`` más negativo es un descuento que **creció** (BAJO),
    que no es un fin de promoción y no debe disparar la regla de concepto.
    """
    return LineaDelta(
        concepto_id="DESCUENTO_PROMOCIONAL",
        nombre_comercial="Descuento por permanencia",
        clase=LineaDelta.clasificar(actual, previo),
        monto_actual_cent=actual,
        monto_previo_cent=previo,
        delta_cent=actual - previo,
        confianza=1.0,
        familia=FamiliaConcepto.CREDITO,
    )


def fin_descuento(movimiento_id: int, dia: int) -> MovementEvent:
    """Orden de fin de promoción emitida por el CRM."""
    return MovementEvent(
        movimiento_id=movimiento_id,
        cuenta_id="C-TEST",
        tipo=TipoMovimiento.FIN_DESCUENTO,
        ocurrido_en=datetime(2026, 7, dia, 12, 0),
        detalle={
            "promocion_id": "PROMO_FIDELIDAD",
            "nombre": "Descuento por permanencia",
            "descuento_cent": 4_990,
        },
    )


def alta_paquete(movimiento_id: int, dia: int) -> MovementEvent:
    """Alta de paquete: un movimiento que la tabla NO admite para la renta del plan."""
    return MovementEvent(
        movimiento_id=movimiento_id,
        cuenta_id="C-TEST",
        tipo=TipoMovimiento.ALTA_PAQUETE,
        ocurrido_en=datetime(2026, 7, dia, 12, 0),
        detalle={"paquete_id": "P1", "nombre": "Paquete", "monto_cent": 2_000},
    )


# --------------------------------------------------------------------------- #
# Las tres ramas de confianza
# --------------------------------------------------------------------------- #
class TestRamasDeConfianza:
    """Sección 4.7 al pie de la letra."""

    def test_un_candidato_da_causa_y_confianza_alta(self, reglas) -> None:
        resultado = atribuir([delta_renta()], [cambio_plan(1, 5)], reglas)[0]

        assert resultado.causa is TipoMovimiento.CAMBIO_PLAN
        assert resultado.confianza == pytest.approx(reglas.confianza.causa_unica)
        assert resultado.movimiento_id == 1
        assert "mov:1" in resultado.evidencia
        assert EVIDENCIA_REGLA in resultado.evidencia

    def test_sin_candidatos_no_se_inventa_una_causa(self, reglas) -> None:
        """Preferimos decir "no sé" antes que atribuir una causa plausible pero falsa."""
        resultado = atribuir([delta_renta()], [], reglas)[0]

        assert resultado.causa is None
        assert resultado.movimiento_id is None
        assert resultado.confianza == pytest.approx(reglas.confianza.sin_candidato)

    def test_varios_candidatos_gana_el_mas_reciente_con_confianza_baja(self, reglas) -> None:
        """LA RAMA QUE ROMPE LA ATRIBUCIÓN INGENUA.

        Dos cambios de plan en el mismo ciclo: se elige el más cercano en el tiempo,
        pero la confianza baja a 0.65 y el descartado queda registrado en la evidencia
        para que el asesor pueda verlo.
        """
        movimientos = [cambio_plan(1, 5), cambio_plan(2, 20)]
        resultado = atribuir([delta_renta()], movimientos, reglas)[0]

        assert resultado.causa is TipoMovimiento.CAMBIO_PLAN
        assert resultado.movimiento_id == 2, "debe ganar el más reciente"
        assert resultado.confianza == pytest.approx(reglas.confianza.multiples_candidatos)
        assert resultado.confianza < reglas.confianza.causa_unica
        assert "mov:1" in resultado.evidencia, "el descartado no puede desaparecer"
        assert "mov:2" in resultado.evidencia

    def test_el_orden_de_entrada_no_altera_el_ganador(self, reglas) -> None:
        adelante = atribuir([delta_renta()], [cambio_plan(1, 5), cambio_plan(2, 20)], reglas)[0]
        al_reves = atribuir([delta_renta()], [cambio_plan(2, 20), cambio_plan(1, 5)], reglas)[0]
        assert adelante.movimiento_id == al_reves.movimiento_id == 2


class TestTablaReglaConceptoCausa:
    """Un movimiento solo explica un concepto si la tabla lo autoriza."""

    def test_un_movimiento_no_permitido_no_es_candidato(self, reglas) -> None:
        """Un alta de paquete no explica la variación de la renta del plan."""
        candidatos = candidatos_para("RENTA_PLAN_MOVIL", [alta_paquete(9, 5)], reglas)
        assert candidatos == []

        resultado = atribuir([delta_renta()], [alta_paquete(9, 5)], reglas)[0]
        assert resultado.causa is None
        assert resultado.confianza == pytest.approx(reglas.confianza.sin_candidato)

    def test_filtra_por_tipo_aunque_haya_ruido_en_la_ventana(self, reglas) -> None:
        movimientos = [alta_paquete(9, 3), cambio_plan(1, 5), alta_paquete(10, 28)]
        candidatos = candidatos_para("RENTA_PLAN_MOVIL", movimientos, reglas)
        assert [movimiento.movimiento_id for movimiento in candidatos] == [1]


# --------------------------------------------------------------------------- #
# Preferencia de causa: la regla de concepto y el orden de la tabla
# --------------------------------------------------------------------------- #
class TestPreferenciaDeCausa:
    """EL DEFECTO DE ATRIBUCIÓN CAUSAL, fijado por sus dos extremos.

    Un cambio de plan que cancela una promoción atada al plan anterior deja **un solo**
    movimiento en la ventana: el ``CAMBIO_PLAN``. Atribuir por cercanía temporal
    etiquetaba la desaparición del descuento como cambio de plan, y el cliente leía que
    su recibo subió por haber cambiado de plan cuando el cambio de plan, por sí solo, se
    lo bajó. La aritmética cerraba; la narrativa mentía.

    ``preferencia_causa`` de ``rules.yaml`` lo resuelve por lo que la variación **es**,
    no por lo que hay cerca. Y el caso inverso importa lo mismo: la regla no puede
    convertir cualquier aumento en un fin de promoción.
    """

    def test_descuento_que_desaparece_es_fin_de_descuento_aunque_solo_haya_cambio_plan(
        self, reglas
    ) -> None:
        """El caso exacto de C-DEMO-01: sin orden de FIN_DESCUENTO, la regla igual manda."""
        resultado = atribuir([delta_descuento(actual=0)], [cambio_plan(1, 13)], reglas)[0]

        assert resultado.causa is TipoMovimiento.FIN_DESCUENTO
        assert resultado.causa is not TipoMovimiento.CAMBIO_PLAN, (
            "la desaparición de un descuento no la explica el cambio de plan más cercano"
        )
        assert resultado.confianza == pytest.approx(reglas.confianza.regla_concepto)
        assert resultado.movimiento_id is None, "no hay orden del CRM que citar"
        assert any(evidencia.startswith(EVIDENCIA_PREFERENCIA) for evidencia in resultado.evidencia)
        assert "mov:1" in resultado.evidencia, (
            "el cambio de plan descartado tiene que seguir visible para el asesor"
        )

    def test_con_orden_de_fin_de_descuento_la_confianza_es_la_de_causa_unica(self, reglas) -> None:
        """Con las dos órdenes en la ventana gana la preferida y se cita su movimiento."""
        movimientos = [cambio_plan(1, 13), fin_descuento(2, 13)]
        resultado = atribuir([delta_descuento(actual=0)], movimientos, reglas)[0]

        assert resultado.causa is TipoMovimiento.FIN_DESCUENTO
        assert resultado.movimiento_id == 2
        assert resultado.confianza == pytest.approx(reglas.confianza.causa_unica)
        assert "mov:1" in resultado.evidencia
        assert "mov:2" in resultado.evidencia

    def test_descuento_prorrateado_tambien_es_fin_de_descuento(self, reglas) -> None:
        """La promoción que vence a mitad de ciclo encoge la línea en vez de borrarla."""
        resultado = atribuir(
            [delta_descuento(actual=-1_355, previo=-3_000)], [cambio_plan(1, 9)], reglas
        )[0]

        assert resultado.clase is ClaseDelta.SUBIO
        assert resultado.causa is TipoMovimiento.FIN_DESCUENTO

    def test_un_descuento_que_crece_no_dispara_la_regla(self, reglas) -> None:
        """CASO INVERSO. La regla está acotada por clase: un crédito mayor no es un fin.

        Si la tabla se aplicara a cualquier variación del concepto, un descuento que
        aumenta pasaría a explicarse como "se le terminó la promoción", que es lo
        contrario de lo que ocurrió.
        """
        resultado = atribuir(
            [delta_descuento(actual=-6_000, previo=-4_990)], [cambio_plan(1, 9)], reglas
        )[0]

        assert resultado.clase is ClaseDelta.BAJO
        assert resultado.causa is TipoMovimiento.CAMBIO_PLAN
        assert not any(
            evidencia.startswith(EVIDENCIA_PREFERENCIA) for evidencia in resultado.evidencia
        )

    def test_un_concepto_sin_preferencia_declarada_no_cambia_de_comportamiento(
        self, reglas
    ) -> None:
        """CASO INVERSO. La renta del plan sigue explicándose por el cambio de plan."""
        resultado = atribuir([delta_renta()], [cambio_plan(1, 13)], reglas)[0]

        assert resultado.causa is TipoMovimiento.CAMBIO_PLAN
        assert resultado.confianza == pytest.approx(reglas.confianza.causa_unica)
        assert EVIDENCIA_REGLA in resultado.evidencia

    def test_el_orden_de_la_tabla_desempata_antes_que_la_cercania_temporal(self, reglas) -> None:
        """``regla_concepto_causa`` es una lista de PRIORIDAD, no un conjunto.

        Con ``DESCUENTO_PROMOCIONAL: [FIN_DESCUENTO, CAMBIO_PLAN]``, un fin de descuento
        del día 5 gana a un cambio de plan del día 20 aunque este sea más reciente. Es el
        mecanismo general: para que otro concepto resuelva distinto basta reordenar su
        fila del YAML, sin tocar una línea de código.
        """
        movimientos = [fin_descuento(5, 5), cambio_plan(6, 20)]
        elegido = elegir_candidato("DESCUENTO_PROMOCIONAL", movimientos, reglas)

        assert elegido is not None
        assert elegido.tipo is TipoMovimiento.FIN_DESCUENTO
        assert elegido.movimiento_id == 5

    def test_dentro_de_la_misma_causa_sigue_ganando_el_mas_reciente(self, reglas) -> None:
        """La cercanía temporal no desaparece: pasa a ser el desempate de segundo nivel."""
        elegido = elegir_candidato(
            "RENTA_PLAN_MOVIL", [cambio_plan(1, 5), cambio_plan(2, 20)], reglas
        )

        assert elegido is not None
        assert elegido.movimiento_id == 2

    def test_la_preferencia_solo_puede_nombrar_causas_ya_permitidas(self, reglas) -> None:
        """Invariante de configuración: la preferencia prioriza, nunca inventa."""
        for concepto_id, preferencias in reglas.preferencia_causa.items():
            permitidas = set(reglas.causas_permitidas(concepto_id))
            for clase, causa in preferencias.items():
                assert causa in permitidas, f"{concepto_id}[{clase}] -> {causa} no permitida"


# --------------------------------------------------------------------------- #
# Prorrateo inconsistente y conceptos derivados
# --------------------------------------------------------------------------- #
class TestProrrateoInconsistente:
    """Si la tabla de tramos no reproduce el importe, la confianza se topa."""

    def test_desvio_mayor_que_la_tolerancia_topa_la_confianza(self, reglas) -> None:
        tramos = [
            Tramo(
                inicio=date(2026, 7, 1),
                fin=date(2026, 7, 13),
                dias=12,
                tarifa_mensual_cent=12_000,
                monto_prorrateado_cent=4_645,
                etiqueta="del 1 al 12 de julio",
            ),
            Tramo(
                inicio=date(2026, 7, 13),
                fin=date(2026, 8, 1),
                dias=19,
                tarifa_mensual_cent=9_900,
                monto_prorrateado_cent=6_068,
                etiqueta="del 13 al 31 de julio",
            ),
        ]
        # El importe facturado (99 999) no es la suma de los tramos (10 713).
        linea = delta_renta(actual=99_999, previo=10_000, tramos=tramos)
        resultado = atribuir([linea], [cambio_plan(1, 13)], reglas, dias_ciclo=31)[0]

        assert resultado.causa is TipoMovimiento.CAMBIO_PLAN
        assert resultado.confianza <= reglas.confianza.tope_prorrateo_inconsistente
        assert any(
            evidencia.startswith("regla:prorrateo_inconsistente")
            for evidencia in resultado.evidencia
        )

    def test_tramos_coherentes_conservan_la_confianza_alta(self, reglas) -> None:
        tramos = [
            Tramo(
                inicio=date(2026, 7, 1),
                fin=date(2026, 7, 13),
                dias=12,
                tarifa_mensual_cent=12_000,
                monto_prorrateado_cent=4_645,
                etiqueta="del 1 al 12 de julio",
            ),
            Tramo(
                inicio=date(2026, 7, 13),
                fin=date(2026, 8, 1),
                dias=19,
                tarifa_mensual_cent=9_900,
                monto_prorrateado_cent=6_068,
                etiqueta="del 13 al 31 de julio",
            ),
        ]
        linea = delta_renta(actual=10_713, previo=10_000, tramos=tramos)
        resultado = atribuir([linea], [cambio_plan(1, 13)], reglas, dias_ciclo=31)[0]
        assert resultado.confianza == pytest.approx(reglas.confianza.causa_unica)


class TestConceptosDerivados:
    """El IGV no tiene causa propia: es consecuencia aritmética del resto del recibo."""

    def test_el_igv_no_recibe_causa_aunque_haya_candidatos(self, reglas) -> None:
        igv = LineaDelta(
            concepto_id="IGV",
            nombre_comercial="IGV",
            clase=ClaseDelta.SUBIO,
            monto_actual_cent=2_500,
            monto_previo_cent=2_000,
            delta_cent=500,
            confianza=1.0,
            familia=FamiliaConcepto.IMPUESTO,
        )
        resultado = atribuir([igv], [cambio_plan(1, 5)], reglas)[0]

        assert "IGV" in CONCEPTOS_DERIVADOS
        assert resultado.causa is None
        assert resultado.confianza == pytest.approx(reglas.confianza.causa_unica)
        assert EVIDENCIA_DERIVADO in resultado.evidencia


# --------------------------------------------------------------------------- #
# Contrato del módulo
# --------------------------------------------------------------------------- #
class TestContrato:
    """La atribución es una función pura: no toca lo que recibe."""

    def test_no_muta_la_entrada(self, reglas) -> None:
        entrada = delta_renta()
        salida = atribuir([entrada], [cambio_plan(1, 5)], reglas)

        assert entrada.causa is None, "la línea original no puede quedar modificada"
        assert entrada.confianza == 1.0
        assert salida[0] is not entrada
        assert salida[0].causa is TipoMovimiento.CAMBIO_PLAN

    def test_esta_atribuida_respeta_la_confianza_minima(self, reglas) -> None:
        """Quién cuenta como "explicado" depende del **umbral de confianza**, no de la causa.

        Una línea sin candidatos conserva la ``causa_oficial`` que el catálogo asigna al
        concepto (RENTA_PLAN_MOVIL ⇒ CAMBIO_DE_PLAN), así que con el umbral por defecto
        de 0.0 seguiría contando como explicada. Lo que la deja fuera es su confianza de
        0.30, por debajo de ``confianza.minima_para_explicar`` (0.35), que es
        precisamente el umbral con el que ``confianza._cobertura`` calcula ``s1``.

        Dicho de otro modo: la cobertura del delta se apoya en el umbral, no en la
        presencia de causa. Bajar ``minima_para_explicar`` por debajo de
        ``sin_candidato`` haría que las líneas sin atribuir contaran como explicadas y
        el sistema derivaría menos de lo que debe.
        """
        atribuida = atribuir([delta_renta()], [cambio_plan(1, 5)], reglas)[0]
        huerfana = atribuir([delta_renta()], [], reglas)[0]
        minima = reglas.confianza.minima_para_explicar

        assert huerfana.causa is None
        assert huerfana.confianza < minima < atribuida.confianza
        assert esta_atribuida(atribuida, minima) is True
        assert esta_atribuida(huerfana, minima) is False
        assert esta_atribuida(atribuida, 0.99) is False
        assert reglas.confianza.sin_candidato < minima, (
            "si sin_candidato >= minima_para_explicar, una línea sin causa contaría "
            "como explicada y el umbral de incomprensión dejaría de derivar"
        )

    def test_lista_vacia_devuelve_lista_vacia(self, reglas) -> None:
        assert atribuir([], [cambio_plan(1, 5)], reglas) == []
