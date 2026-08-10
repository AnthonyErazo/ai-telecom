"""Cuota de equipo financiado por sistema francés (sección 4.4).

Dos invariantes que no admiten excepción y una regla de negocio:

* ``B_n == 0`` — el cronograma cierra en cero. Si no, el cliente termina pagando de más
  o de menos, y ninguna de las dos cosas es aceptable en un recibo.
* ``Σ amortizaciones == K`` — el capital se devuelve entero, ni un céntimo más.
* **La cuota NUNCA se prorratea.** Es la confusión más común del cliente cuando cambia
  de plan a mitad de mes: cree que la cuota del equipo también se parte, y no.

Se prueban los dos regímenes: ``i = 0`` (financiamiento sin intereses, el habitual en
las campañas de Movistar) e ``i > 0``.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from packages.facts_engine.prorrateo import cronograma_frances, cuota_equipo_financiado


# --------------------------------------------------------------------------- #
# i = 0
# --------------------------------------------------------------------------- #
class TestSinIntereses:
    """``A = K / n`` cuando la tasa es cero."""

    def test_cuota_constante_y_cronograma_que_cierra(self) -> None:
        plan = cronograma_frances("Samsung Galaxy serie S", 232_200, 0, 18)

        assert len(plan.cronograma) == 18
        assert all(cuota.monto_cent == 12_900 for cuota in plan.cronograma)
        assert all(cuota.interes_cent == 0 for cuota in plan.cronograma)
        assert plan.cronograma[-1].saldo_final_cent == 0
        assert sum(cuota.amortizacion_cent for cuota in plan.cronograma) == 232_200

    def test_capital_no_divisible_la_ultima_cuota_absorbe_el_centimo(self) -> None:
        """``B_n == 0`` obliga a que la última cuota cargue con el resto."""
        plan = cronograma_frances("Equipo con resto", 100_000, 0, 3)

        assert plan.cronograma[-1].saldo_final_cent == 0
        assert sum(cuota.amortizacion_cent for cuota in plan.cronograma) == 100_000
        assert sum(cuota.monto_cent for cuota in plan.cronograma) == 100_000
        # Las cuotas intermedias son iguales; solo la última se ajusta.
        assert plan.cronograma[0].monto_cent == plan.cronograma[1].monto_cent

    def test_una_sola_cuota_es_el_capital_entero(self) -> None:
        plan = cronograma_frances("Equipo al contado diferido", 45_000, 0, 1)
        assert plan.cronograma[0].monto_cent == 45_000
        assert plan.cronograma[0].saldo_final_cent == 0
        assert plan.cronograma[0].es_ultima is True


# --------------------------------------------------------------------------- #
# i > 0
# --------------------------------------------------------------------------- #
class TestConIntereses:
    """``A = K·i / (1 − (1+i)^(−n))`` y ``B_m = B_{m−1}·(1+i) − A``."""

    def test_cronograma_cierra_en_cero_y_devuelve_el_capital(self) -> None:
        plan = cronograma_frances("Equipo financiado", 120_000, 200, 12)  # 2 % mensual

        assert len(plan.cronograma) == 12
        assert plan.cronograma[-1].saldo_final_cent == 0
        assert sum(cuota.amortizacion_cent for cuota in plan.cronograma) == 120_000

    def test_el_interes_es_positivo_y_decreciente(self) -> None:
        """Con saldo decreciente, el interés de cada cuota baja mes a mes."""
        plan = cronograma_frances("Equipo financiado", 120_000, 200, 12)
        intereses = [cuota.interes_cent for cuota in plan.cronograma]

        assert all(interes > 0 for interes in intereses[:-1])
        assert intereses == sorted(intereses, reverse=True)
        assert sum(intereses) > 0

    def test_el_total_pagado_es_capital_mas_intereses(self) -> None:
        plan = cronograma_frances("Equipo financiado", 120_000, 200, 12)
        pagado = sum(cuota.monto_cent for cuota in plan.cronograma)
        intereses = sum(cuota.interes_cent for cuota in plan.cronograma)
        assert pagado == 120_000 + intereses

    def test_el_saldo_encadena_cuota_a_cuota(self) -> None:
        """``saldo_inicial`` de la cuota *m* es ``saldo_final`` de la *m−1*."""
        plan = cronograma_frances("Equipo financiado", 120_000, 200, 12)
        assert plan.cronograma[0].saldo_inicial_cent == 120_000
        for anterior, siguiente in zip(plan.cronograma, plan.cronograma[1:], strict=False):
            assert siguiente.saldo_inicial_cent == anterior.saldo_final_cent

    @pytest.mark.parametrize(
        "tasa",
        [200, Fraction(2, 100), Decimal("0.02"), "0.02"],
        ids=["puntos-basicos", "fraction", "decimal", "texto"],
    )
    def test_todas_las_formas_de_expresar_la_tasa_dan_lo_mismo(self, tasa: object) -> None:
        """``int`` son puntos básicos; ``Fraction``/``Decimal``/``str`` son la tasa directa."""
        plan = cronograma_frances("Equipo", 120_000, tasa, 12)  # type: ignore[arg-type]
        assert plan.cronograma[0].monto_cent == 11_347
        assert plan.cronograma[-1].saldo_final_cent == 0


# --------------------------------------------------------------------------- #
# Reglas de negocio y de tipos
# --------------------------------------------------------------------------- #
class TestReglasDelFinanciamiento:
    """Lo que el cliente pregunta y lo que el sistema no puede permitirse."""

    def test_la_coma_flotante_se_rechaza_por_tipo(self) -> None:
        """Regla innegociable nº 1: nada de ``float`` en la aritmética del dinero."""
        with pytest.raises(TypeError, match="coma flotante"):
            cronograma_frances("Equipo", 120_000, 0.02, 12)  # type: ignore[arg-type]

    def test_la_etiqueta_explicativa_es_cuota_n_de_m(self) -> None:
        """*"cuota 3 de 18"* es la clave explicativa de la sección 4.4."""
        plan = cronograma_frances("Samsung Galaxy serie S", 232_200, 0, 18)
        assert plan.cronograma[2].etiqueta == "cuota 3 de 18"
        assert plan.cronograma[2].numero == 3
        assert plan.cronograma[2].de_total == 18
        assert plan.cronograma[-1].es_ultima is True
        assert plan.cronograma[0].es_ultima is False

    def test_la_cuota_no_se_prorratea_nunca(self) -> None:
        """La cuota del mes es la misma tenga el ciclo 28 o 31 días.

        El sistema francés no conoce el calendario: la cuota depende del número de
        cuota, no de los días del ciclo. Es la diferencia con la renta del plan y el
        origen del malentendido más frecuente cuando hay cambio de plan a mitad de mes.
        """
        cuota_m3, _saldo = cuota_equipo_financiado(232_200, 0, 18, 3)
        assert cuota_m3 == 12_900
        for mes in range(1, 18):
            assert cuota_equipo_financiado(232_200, 0, 18, mes)[0] == 12_900

    def test_cuota_equipo_financiado_devuelve_cuota_y_saldo(self) -> None:
        cuota, saldo = cuota_equipo_financiado(120_000, 200, 12, 1)
        plan = cronograma_frances("Equipo", 120_000, 200, 12)
        assert cuota == plan.cronograma[0].monto_cent
        assert saldo == plan.cronograma[0].saldo_final_cent

    def test_la_ultima_cuota_deja_el_saldo_en_cero(self) -> None:
        for capital, tasa, cuotas in ((232_200, 0, 18), (120_000, 200, 12), (99_990, 150, 6)):
            _cuota, saldo = cuota_equipo_financiado(capital, tasa, cuotas, cuotas)
            assert saldo == 0, f"B_n != 0 con K={capital}, i={tasa}, n={cuotas}"
