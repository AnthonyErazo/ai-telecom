"""Aritmética monetaria: céntimos enteros y reparto por mayor resto (sección 4.5).

La propiedad que se defiende aquí es la que sostiene todo lo demás: **la suma de las
partes es exactamente el total, siempre**. Si el reparto perdiera un céntimo, el
invariante de conciliación fallaría, el sistema derivaría al asesor y la explicación no
llegaría al cliente. Un céntimo aquí no es un detalle estético.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from packages.core_domain.dinero import (
    CENTIMOS_POR_SOL,
    a_centimos,
    aplicar_porcentaje,
    formatear_numero,
    formatear_soles,
    prorratear,
    redondear_banca,
    repartir_mayor_resto,
    variantes_monto,
)


# --------------------------------------------------------------------------- #
# Reparto por mayor resto
# --------------------------------------------------------------------------- #
class TestRepartirMayorResto:
    """``c_i = floor(x_i)``; el residuo va a los de mayor parte fraccionaria."""

    def test_reparto_exacto_sin_residuo(self) -> None:
        assert repartir_mayor_resto(300, [1, 1, 1]) == [100, 100, 100]

    def test_el_residuo_va_a_las_mayores_partes_fraccionarias(self) -> None:
        # 100 entre tres partes iguales: 33.33 cada una, sobra 1 céntimo.
        partes = repartir_mayor_resto(100, [1, 1, 1])
        assert sum(partes) == 100
        assert sorted(partes) == [33, 33, 34]

    def test_empates_se_resuelven_por_indice_ascendente(self) -> None:
        """Determinismo: con pesos idénticos el céntimo sobrante va al primero.

        Sin esta regla la demo no sería byte-reproducible.
        """
        assert repartir_mayor_resto(100, [1, 1, 1]) == [34, 33, 33]
        assert repartir_mayor_resto(10, [1, 1, 1, 1]) == [3, 3, 2, 2]

    def test_pesos_desiguales(self) -> None:
        partes = repartir_mayor_resto(10_000, [7, 2, 1])
        assert sum(partes) == 10_000
        assert partes == [7000, 2000, 1000]

    @pytest.mark.parametrize(
        "total,pesos",
        [
            (12_490, [1, 1, 1, 1, 1, 1, 1]),
            (1, [1, 1, 1]),
            (0, [3, 5, 7]),
            (-1_000, [2, 3, 5]),
            (-7, [1, 1, 1]),
            (999_999, [11, 13, 17, 19]),
            (100, [0, 1, 0]),
        ],
    )
    def test_la_suma_siempre_es_el_total(self, total: int, pesos: list[int]) -> None:
        """El invariante nuclear: no se pierde ni se inventa un céntimo."""
        partes = repartir_mayor_resto(total, pesos)
        assert sum(partes) == total
        assert len(partes) == len(pesos)
        assert all(isinstance(parte, int) for parte in partes)

    def test_admite_pesos_fraccionarios_y_decimales(self) -> None:
        assert sum(repartir_mayor_resto(100, [Fraction(1, 3), Fraction(2, 3)])) == 100
        assert sum(repartir_mayor_resto(100, [Decimal("0.1"), Decimal("0.2")])) == 100

    def test_sin_pesos_solo_admite_total_cero(self) -> None:
        assert repartir_mayor_resto(0, []) == []
        with pytest.raises(ValueError, match="sin pesos"):
            repartir_mayor_resto(100, [])

    def test_rechaza_pesos_negativos_y_suma_cero(self) -> None:
        with pytest.raises(ValueError, match="peso negativo"):
            repartir_mayor_resto(100, [1, -1])
        with pytest.raises(ValueError, match="mayor que cero"):
            repartir_mayor_resto(100, [0, 0])


# --------------------------------------------------------------------------- #
# Redondeo bancario y prorrateo
# --------------------------------------------------------------------------- #
class TestRedondeoBancario:
    """Mitad al par: no introduce sesgo al prorratear millones de recibos."""

    @pytest.mark.parametrize(
        "numerador,denominador,esperado",
        [
            (5, 2, 2),  # 2.5 -> 2 (par)
            (7, 2, 4),  # 3.5 -> 4 (par)
            (1, 2, 0),  # 0.5 -> 0 (par)
            (3, 2, 2),  # 1.5 -> 2 (par)
            (10, 3, 3),
            (11, 3, 4),
            (-5, 2, -2),
            (-7, 2, -4),
            (5, -2, -2),
        ],
    )
    def test_mitad_al_par(self, numerador: int, denominador: int, esperado: int) -> None:
        assert redondear_banca(numerador, denominador) == esperado

    def test_denominador_cero(self) -> None:
        with pytest.raises(ZeroDivisionError):
            redondear_banca(1, 0)


class TestProrrateo:
    """``P · len / D`` con aritmética entera."""

    def test_medio_ciclo(self) -> None:
        assert prorratear(12_000, 15, 30) == 6_000

    def test_ciclo_completo_devuelve_el_monto(self) -> None:
        for dias in (28, 29, 30, 31):
            assert prorratear(9_990, dias, dias) == 9_990

    def test_cero_dias_no_cobra(self) -> None:
        assert prorratear(9_990, 0, 31) == 0

    def test_funciona_con_montos_negativos(self) -> None:
        """Los ajustes y descuentos también se prorratean."""
        assert prorratear(-12_000, 15, 30) == -6_000

    def test_rechaza_entradas_imposibles(self) -> None:
        with pytest.raises(ValueError, match="días negativos"):
            prorratear(100, -1, 30)
        with pytest.raises(ValueError, match="días de ciclo"):
            prorratear(100, 10, 0)


class TestPorcentaje:
    """IGV peruano en puntos básicos: 18 % = 1800 bp."""

    def test_igv_del_dieciocho_por_ciento(self) -> None:
        assert aplicar_porcentaje(10_000, 1800) == 1_800
        assert aplicar_porcentaje(12_490, 1800) == 2_248

    def test_cero_por_ciento(self) -> None:
        assert aplicar_porcentaje(12_490, 0) == 0


# --------------------------------------------------------------------------- #
# Parseo y formato
# --------------------------------------------------------------------------- #
class TestParseoDeImportes:
    """Las cuatro escrituras que aparecen en recibos y en mensajes de clientes."""

    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("S/ 1,234.50", 123_450),
            ("1.234,50", 123_450),
            ("1234.50", 123_450),
            ("124,90", 12_490),
            ("S/. 124.90", 12_490),
            ("PEN 99.90", 9_990),
            ("-12.30", -1_230),
            ("(12.30)", -1_230),
            ("0.01", 1),
        ],
    )
    def test_formatos_reconocidos(self, texto: str, esperado: int) -> None:
        assert a_centimos(texto) == esperado

    def test_los_numeros_se_interpretan_como_soles(self) -> None:
        assert a_centimos(120) == 120 * CENTIMOS_POR_SOL
        assert a_centimos(Decimal("99.90")) == 9_990

    def test_rechaza_lo_que_no_es_un_importe(self) -> None:
        with pytest.raises(ValueError):
            a_centimos("no es plata")
        with pytest.raises(TypeError):
            a_centimos(True)


class TestFormato:
    """Formato canónico peruano: el signo delante del símbolo."""

    @pytest.mark.parametrize(
        "centimos,esperado",
        [
            (123_450, "S/ 1,234.50"),
            (12_490, "S/ 124.90"),
            (0, "S/ 0.00"),
            (-123_450, "-S/ 1,234.50"),
            (5, "S/ 0.05"),
        ],
    )
    def test_formatear_soles(self, centimos: int, esperado: str) -> None:
        assert formatear_soles(centimos) == esperado

    def test_formatear_numero_sin_simbolo(self) -> None:
        assert formatear_numero(123_450) == "1,234.50"

    def test_ida_y_vuelta(self) -> None:
        """Formatear y volver a parsear no puede mover el importe."""
        for centimos in (0, 1, 999, 12_490, 123_450, 1_000_000):
            assert a_centimos(formatear_soles(centimos)) == centimos

    def test_variantes_cubren_las_escrituras_del_verificador(self) -> None:
        variantes = variantes_monto(123_450)
        assert "S/ 1,234.50" in variantes
        assert "1.234,50" in variantes
        assert "1234.50" in variantes
        # Siempre del valor absoluto: el signo lo maneja el verificador aparte.
        assert variantes_monto(-123_450) == variantes
