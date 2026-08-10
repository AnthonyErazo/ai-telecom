"""Partición del ciclo en tramos (sección 4.1). **La tabla de tramos ES la explicación.**

Un solo algoritmo tiene que cubrir los cinco escenarios críticos de la ficha, y tiene
que hacerlo con meses de 28, 29, 30 y 31 días sin perder un céntimo. Lo que se prueba
aquí no es la aritmética (eso es ``test_prorrateo``) sino que la partición **cierra**:
tramos disjuntos, contiguos, que cubren el ciclo entero y cuya suma reproduce la renta.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from packages.core_domain.enums import ConvencionProrrateo, EstadoServicio, TipoMovimiento
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.facts_engine.tramos import (
    DescuentoVigente,
    construir_tramos,
    describir_tramos,
    dias_facturables,
    dias_suspendidos,
    validar_particion,
)

TARIFA = 12_000  # S/ 120.00


def _movimiento(tipo: TipoMovimiento, dia: int, detalle: dict) -> MovementEvent:
    """Movimiento de la cuenta de prueba en el día indicado de julio de 2026."""
    return MovementEvent(
        movimiento_id=dia,
        cuenta_id="C-TEST",
        tipo=tipo,
        ocurrido_en=datetime(2026, 7, dia, 10, 0),
        detalle=detalle,
    )


CAMBIO_PLAN = _movimiento(
    TipoMovimiento.CAMBIO_PLAN,
    13,
    {
        "plan_anterior": "Plan Movil Ilimitado",
        "plan_nuevo": "Plan Movil Max 50GB",
        "tarifa_anterior_cent": 12_000,
        "tarifa_nueva_cent": 9_900,
    },
)
SUSPENSION = _movimiento(TipoMovimiento.SUSPENSION, 10, {"motivo": "MOROSIDAD"})
RECONEXION = _movimiento(TipoMovimiento.RECONEXION, 19, {"cargo_cent": 2_500, "dias_suspendido": 9})


# --------------------------------------------------------------------------- #
# Meses de 28, 29, 30 y 31 días
# --------------------------------------------------------------------------- #
#: Los cuatro largos de ciclo que existen, incluido febrero bisiesto (2024).
CICLOS = [
    pytest.param(date(2026, 2, 1), date(2026, 3, 1), 28, id="febrero-28"),
    pytest.param(date(2024, 2, 1), date(2024, 3, 1), 29, id="febrero-bisiesto-29"),
    pytest.param(date(2026, 4, 1), date(2026, 5, 1), 30, id="abril-30"),
    pytest.param(date(2026, 7, 1), date(2026, 8, 1), 31, id="julio-31"),
]


class TestLargoDeCiclo:
    """El motor no puede tener casos especiales por mes."""

    @pytest.mark.parametrize("inicio,fin,dias", CICLOS)
    def test_ciclo_sin_eventos_da_un_solo_tramo_por_la_tarifa_completa(
        self, inicio: date, fin: date, dias: int
    ) -> None:
        tramos = construir_tramos(inicio, fin, [], TARIFA, None)
        assert len(tramos) == 1
        assert tramos[0].dias == dias
        assert tramos[0].monto_prorrateado_cent == TARIFA
        validar_particion(tramos, inicio, fin)

    @pytest.mark.parametrize("inicio,fin,dias", CICLOS)
    def test_cambio_de_plan_parte_el_ciclo_y_la_suma_cierra(
        self, inicio: date, fin: date, dias: int
    ) -> None:
        """Dos tramos, contiguos, sin solapamiento y con Σ días == D."""
        corte = _movimiento(TipoMovimiento.CAMBIO_PLAN, 13, CAMBIO_PLAN.detalle)
        corte = corte.model_copy(
            update={"ocurrido_en": datetime(inicio.year, inicio.month, 13, 10, 0)}
        )
        tramos = construir_tramos(inicio, fin, [corte], TARIFA, None)

        assert len(tramos) == 2
        assert sum(tramo.dias for tramo in tramos) == dias
        assert tramos[0].fin == tramos[1].inicio  # contiguos, sin hueco ni solape
        assert tramos[0].tarifa_mensual_cent == 12_000
        assert tramos[1].tarifa_mensual_cent == 9_900
        validar_particion(tramos, inicio, fin)

    @pytest.mark.parametrize("inicio,fin,dias", CICLOS)
    def test_fin_exclusivo_en_todos_los_ciclos(self, inicio: date, fin: date, dias: int) -> None:
        """Convención ``[inicio, fin)``: el último día facturado es ``fin - 1``."""
        tramos = construir_tramos(inicio, fin, [], TARIFA, None)
        assert tramos[-1].fin == fin
        assert tramos[-1].fin_inclusivo == date.fromordinal(fin.toordinal() - 1)
        assert (fin - inicio).days == dias


# --------------------------------------------------------------------------- #
# Suspensión y reconexión
# --------------------------------------------------------------------------- #
class TestSuspension:
    """Sección 4.3: los días suspendidos no se cobran (política por defecto)."""

    def test_tres_tramos_activo_suspendido_activo(self) -> None:
        tramos = construir_tramos(
            date(2026, 7, 1), date(2026, 8, 1), [SUSPENSION, RECONEXION], TARIFA, None
        )
        assert [tramo.estado for tramo in tramos] == [
            EstadoServicio.ACTIVO,
            EstadoServicio.SUSPENDIDO,
            EstadoServicio.ACTIVO,
        ]
        assert dias_suspendidos(tramos) == 9
        assert dias_facturables(tramos) == 22
        assert dias_facturables(tramos) + dias_suspendidos(tramos) == 31
        validar_particion(tramos, date(2026, 7, 1), date(2026, 8, 1))

    def test_el_tramo_suspendido_no_se_cobra(self) -> None:
        tramos = construir_tramos(
            date(2026, 7, 1), date(2026, 8, 1), [SUSPENSION, RECONEXION], TARIFA, None
        )
        suspendido = next(tramo for tramo in tramos if tramo.estado is EstadoServicio.SUSPENDIDO)
        assert suspendido.facturable is False
        assert suspendido.monto_prorrateado_cent == 0

    def test_con_cobro_en_suspension_si_se_cobra(self) -> None:
        """``COBRO_EN_SUSPENSION`` es un parámetro **[POR VALIDAR con Movistar]**."""
        tramos = construir_tramos(
            date(2026, 7, 1),
            date(2026, 8, 1),
            [SUSPENSION, RECONEXION],
            TARIFA,
            None,
            cobrar_en_suspension=True,
        )
        suspendido = next(tramo for tramo in tramos if tramo.estado is EstadoServicio.SUSPENDIDO)
        assert suspendido.facturable is True
        assert suspendido.monto_prorrateado_cent > 0


# --------------------------------------------------------------------------- #
# Descuentos, fusión y validación
# --------------------------------------------------------------------------- #
class TestTarifaEfectiva:
    """La tarifa del tramo es la **efectiva**: lista menos descuentos vigentes."""

    def test_descuento_vigente_todo_el_ciclo(self) -> None:
        descuento = DescuentoVigente(descuento_id="P1", nombre="Promo", monto_cent=3_000)
        tramos = construir_tramos(date(2026, 7, 1), date(2026, 8, 1), [], TARIFA, [descuento])
        assert len(tramos) == 1
        assert tramos[0].tarifa_mensual_cent == 9_000
        assert tramos[0].descuento_cent == 3_000

    def test_el_descuento_no_deja_la_tarifa_en_negativo(self) -> None:
        descuento = DescuentoVigente(descuento_id="P2", nombre="Promo", monto_cent=99_999)
        tramos = construir_tramos(date(2026, 7, 1), date(2026, 8, 1), [], TARIFA, [descuento])
        assert tramos[0].tarifa_mensual_cent == 0
        assert tramos[0].monto_prorrateado_cent == 0

    def test_descuento_que_vence_a_media_ciclo_parte_el_tramo(self) -> None:
        descuento = DescuentoVigente(
            descuento_id="P3",
            nombre="Promo",
            monto_cent=3_000,
            desde=date(2026, 7, 1),
            hasta=date(2026, 7, 16),  # hasta EXCLUSIVO
        )
        tramos = construir_tramos(date(2026, 7, 1), date(2026, 8, 1), [], TARIFA, [descuento])
        assert len(tramos) == 2
        assert tramos[0].tarifa_mensual_cent == 9_000
        assert tramos[1].tarifa_mensual_cent == 12_000
        assert sum(tramo.dias for tramo in tramos) == 31


class TestParticion:
    """La partición es el contrato: si no cierra, no se explica."""

    def test_validar_particion_detecta_un_hueco(self) -> None:
        tramos = construir_tramos(date(2026, 7, 1), date(2026, 8, 1), [CAMBIO_PLAN], TARIFA, None)
        with pytest.raises(ValueError):
            validar_particion(tramos[:1], date(2026, 7, 1), date(2026, 8, 1))

    def test_tramos_contiguos_del_mismo_perfil_se_fusionan(self) -> None:
        """Dos cambios que dejan la misma tarifa no deben producir tramos redundantes."""
        vuelta = _movimiento(
            TipoMovimiento.CAMBIO_PLAN,
            20,
            {
                "plan_anterior": "Plan Movil Max 50GB",
                "plan_nuevo": "Plan Movil Max 50GB",
                "tarifa_anterior_cent": 9_900,
                "tarifa_nueva_cent": 9_900,
            },
        )
        tramos = construir_tramos(
            date(2026, 7, 1), date(2026, 8, 1), [CAMBIO_PLAN, vuelta], TARIFA, None
        )
        assert len(tramos) == 2
        assert sum(tramo.dias for tramo in tramos) == 31

    def test_describir_tramos_es_texto_de_cliente(self) -> None:
        tramos = construir_tramos(date(2026, 7, 1), date(2026, 8, 1), [CAMBIO_PLAN], TARIFA, None)
        descripcion = describir_tramos(tramos)
        assert "del 1 al 12 de julio" in descripcion
        assert "del 13 al 31 de julio" in descripcion


class TestConvencionTreintaTrescientosSesenta:
    """``CONVENCION_PRORRATEO`` **[POR VALIDAR]**: los días reales no cambian."""

    def test_los_dias_siguen_siendo_reales_solo_cambia_el_importe(self) -> None:
        tramos = construir_tramos(
            date(2026, 7, 1),
            date(2026, 8, 1),
            [CAMBIO_PLAN],
            TARIFA,
            None,
            convencion=ConvencionProrrateo.TREINTA_360,
            dias_base_30_360=30,
        )
        assert sum(tramo.dias for tramo in tramos) == 31  # días reales, siempre
        validar_particion(tramos, date(2026, 7, 1), date(2026, 8, 1))
