"""Prorrateo en las dos modalidades de renta (secciones 4.2 y 4.3).

El corazón del proyecto está aquí. En **VENCIDA** el recibo del ciclo *k* cobra el ciclo
*k* que ya cerró y el prorrateo se ve directamente en la renta. En **ADELANTADA** el
recibo cobra la renta del ciclo *k+1* y corrige el ciclo *k* con un ajuste retroactivo:
**conviven dos rentas en el mismo documento**, y de ahí sale el insight central del
proyecto —el recibo puede subir aunque el plan nuevo sea más barato—, que tiene su
propia prueba al final del archivo.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from packages.core_domain.enums import ConvencionProrrateo, TipoMovimiento
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.facts_engine.prorrateo import (
    ajuste_por_suspension,
    ajuste_retroactivo,
    denominador_ciclo,
    dias_30_360,
    dias_para_prorrateo,
    distribuir_renta_por_tramos,
    renta_del_ciclo,
    total_adelantada,
    total_vencida,
)
from packages.facts_engine.tramos import construir_tramos

CICLO_INICIO = date(2026, 7, 1)
CICLO_FIN = date(2026, 8, 1)
DIAS_CICLO = 31


def _cambio_plan(dia: int, anterior_cent: int, nueva_cent: int) -> MovementEvent:
    """Cambio de plan el día indicado del ciclo de julio de 2026."""
    return MovementEvent(
        movimiento_id=100 + dia,
        cuenta_id="C-TEST",
        tipo=TipoMovimiento.CAMBIO_PLAN,
        ocurrido_en=datetime(2026, 7, dia, 12, 0),
        detalle={
            "plan_anterior": "anterior",
            "plan_nuevo": "nuevo",
            "tarifa_anterior_cent": anterior_cent,
            "tarifa_nueva_cent": nueva_cent,
        },
    )


# --------------------------------------------------------------------------- #
# Convenciones de días
# --------------------------------------------------------------------------- #
class TestConvenciones:
    """``actual`` usa días reales; ``30_360`` fuerza meses de 30 **[POR VALIDAR]**."""

    @pytest.mark.parametrize(
        "inicio,fin,dias_reales",
        [
            (date(2026, 2, 1), date(2026, 3, 1), 28),
            (date(2024, 2, 1), date(2024, 3, 1), 29),
            (date(2026, 4, 1), date(2026, 5, 1), 30),
            (date(2026, 7, 1), date(2026, 8, 1), 31),
        ],
    )
    def test_actual_cuenta_dias_reales(self, inicio: date, fin: date, dias_reales: int) -> None:
        assert dias_para_prorrateo(inicio, fin) == dias_reales
        assert denominador_ciclo(dias_reales) == dias_reales

    def test_treinta_360_normaliza_todos_los_meses(self) -> None:
        assert dias_30_360(date(2026, 2, 1), date(2026, 3, 1)) == 30
        assert dias_30_360(date(2026, 7, 1), date(2026, 8, 1)) == 30
        assert denominador_ciclo(31, ConvencionProrrateo.TREINTA_360) == 30
        assert denominador_ciclo(28, ConvencionProrrateo.TREINTA_360) == 30

    def test_ciclo_de_cero_dias_es_imposible(self) -> None:
        with pytest.raises(ValueError):
            denominador_ciclo(0)


# --------------------------------------------------------------------------- #
# Renta VENCIDA
# --------------------------------------------------------------------------- #
class TestRentaVencida:
    """``T_k = RENTA_k + CONSUMO_k + CUOTAS_k + CARGOS_k − CREDITOS_k``."""

    def test_ciclo_completo_sin_eventos(self) -> None:
        tramos = construir_tramos(CICLO_INICIO, CICLO_FIN, [], 12_000, None)
        assert renta_del_ciclo(tramos, DIAS_CICLO) == 12_000

    def test_cambio_de_plan_a_mitad_de_ciclo_suma_los_dos_tramos(self) -> None:
        """El cliente pagó doce días de un plan y diecinueve del otro."""
        tramos = construir_tramos(
            CICLO_INICIO, CICLO_FIN, [_cambio_plan(13, 12_000, 9_900)], 12_000, None
        )
        renta = renta_del_ciclo(tramos, DIAS_CICLO)
        assert renta == sum(tramo.monto_prorrateado_cent for tramo in tramos)
        # Entre las dos tarifas: ni una ni otra completa.
        assert 9_900 < renta < 12_000

    def test_bajar_de_plan_baja_la_renta_del_ciclo(self) -> None:
        caro = construir_tramos(CICLO_INICIO, CICLO_FIN, [], 12_000, None)
        mixto = construir_tramos(
            CICLO_INICIO, CICLO_FIN, [_cambio_plan(13, 12_000, 9_900)], 12_000, None
        )
        assert renta_del_ciclo(mixto, DIAS_CICLO) < renta_del_ciclo(caro, DIAS_CICLO)

    def test_total_vencida_compone_todos_los_sumandos(self) -> None:
        total = total_vencida(
            renta_ciclo_cent=10_000,
            consumo_cent=640,
            cuotas_cent=12_900,
            cargos_cent=2_500,
            creditos_cent=1_000,
        )
        assert total == 10_000 + 640 + 12_900 + 2_500 - 1_000


class TestAjustePorSuspension:
    """Sección 4.3: el ajuste por días de suspensión **nunca es positivo**."""

    def test_devuelve_lo_no_cobrado_con_signo_negativo(self) -> None:
        suspension = MovementEvent(
            movimiento_id=1,
            cuenta_id="C-TEST",
            tipo=TipoMovimiento.SUSPENSION,
            ocurrido_en=datetime(2026, 7, 10, 9, 0),
            detalle={"motivo": "MOROSIDAD"},
        )
        reconexion = MovementEvent(
            movimiento_id=2,
            cuenta_id="C-TEST",
            tipo=TipoMovimiento.RECONEXION,
            ocurrido_en=datetime(2026, 7, 19, 9, 0),
            detalle={"cargo_cent": 2_500, "dias_suspendido": 9},
        )
        tramos = construir_tramos(CICLO_INICIO, CICLO_FIN, [suspension, reconexion], 12_000, None)
        ajuste = ajuste_por_suspension(tramos, DIAS_CICLO)
        assert ajuste < 0
        assert renta_del_ciclo(tramos, DIAS_CICLO) + abs(ajuste) == 12_000

    def test_sin_suspension_no_hay_ajuste(self) -> None:
        tramos = construir_tramos(CICLO_INICIO, CICLO_FIN, [], 12_000, None)
        assert ajuste_por_suspension(tramos, DIAS_CICLO) == 0


# --------------------------------------------------------------------------- #
# Renta ADELANTADA
# --------------------------------------------------------------------------- #
class TestRentaAdelantada:
    """``AJUSTE_RETRO = (P_new − P_old) · d_new / D``, con reverso y recobro separados."""

    def test_bajar_de_plan_da_ajuste_retroactivo_negativo(self) -> None:
        ajuste = ajuste_retroactivo(
            tarifa_anterior_cent=12_000,
            tarifa_nueva_cent=9_900,
            dias_nuevo_plan=20,
            dias_ciclo=30,
        )
        assert ajuste.reverso_cent == -8_000  # 12000 · 20/30 devuelto
        assert ajuste.recobro_cent == 6_600  # 9900 · 20/30 cobrado
        assert ajuste.total_cent == -1_400
        assert ajuste.total_cent == ajuste.reverso_cent + ajuste.recobro_cent

    def test_subir_de_plan_da_ajuste_retroactivo_positivo(self) -> None:
        ajuste = ajuste_retroactivo(
            tarifa_anterior_cent=9_900,
            tarifa_nueva_cent=12_000,
            dias_nuevo_plan=20,
            dias_ciclo=30,
        )
        assert ajuste.total_cent == 1_400

    def test_sin_dias_en_el_plan_nuevo_no_hay_ajuste(self) -> None:
        ajuste = ajuste_retroactivo(
            tarifa_anterior_cent=12_000, tarifa_nueva_cent=9_900, dias_nuevo_plan=0, dias_ciclo=30
        )
        assert ajuste.total_cent == 0

    def test_total_adelantada_suma_renta_anticipada_y_ajuste(self) -> None:
        assert (
            total_adelantada(renta_anticipada_cent=9_900, ajuste_retro_cent=600, consumo_cent=0)
            == 10_500
        )

    def test_insight_central_el_recibo_sube_con_un_plan_mas_barato(self) -> None:
        """EL CASO QUE VENDE EL PROYECTO.

        Plan viejo: lista S/ 120.00 con promoción de −S/ 30.00 ⇒ efectiva S/ 90.00.
        Plan nuevo: lista S/ 99.00 **sin** promoción (la promo muere con el plan).
        Ciclo de 30 días, cambio el día 11 ⇒ 20 días con el plan nuevo.

        El mes anterior el cliente pagó S/ 90.00. Este mes paga la renta anticipada del
        plan nuevo (S/ 99.00) más el ajuste retroactivo, que sale **positivo** porque la
        tarifa efectiva subió. Resultado: S/ 105.00. El recibo sube S/ 15.00 con un plan
        de lista más barato, que es exactamente lo que el cliente no entiende y por lo
        que llama al 104.
        """
        previo_cent = 9_000  # lo pagado el mes anterior (tarifa efectiva con promoción)
        ajuste = ajuste_retroactivo(
            tarifa_anterior_cent=9_000,  # efectiva: 12000 de lista − 3000 de promoción
            tarifa_nueva_cent=9_900,  # lista del plan nuevo, sin promoción
            dias_nuevo_plan=20,
            dias_ciclo=30,
        )
        actual_cent = total_adelantada(
            renta_anticipada_cent=9_900, ajuste_retro_cent=ajuste.total_cent
        )

        assert ajuste.reverso_cent == -6_000
        assert ajuste.recobro_cent == 6_600
        assert ajuste.total_cent == 600
        assert actual_cent == 10_500
        assert actual_cent > previo_cent, "el recibo tiene que SUBIR con el plan más barato"
        assert actual_cent - previo_cent == 1_500


# --------------------------------------------------------------------------- #
# Reparto de la renta entre tramos
# --------------------------------------------------------------------------- #
class TestDistribucion:
    """El reparto usa mayor resto: Σ partes == total, siempre."""

    def test_la_suma_de_las_partes_es_el_total(self) -> None:
        tramos = construir_tramos(
            CICLO_INICIO, CICLO_FIN, [_cambio_plan(13, 12_000, 9_900)], 12_000, None
        )
        for total in (10_000, 12_345, 1, 0):
            partes = distribuir_renta_por_tramos(total, tramos)
            assert sum(partes) == total
            assert len(partes) == len(tramos)
