"""Comparación de recibos: FULL OUTER JOIN por ``concepto_id`` (sección 4.6).

Cinco clases y ninguna más: ``NUEVO``, ``DESAPARECIDO``, ``SUBIO``, ``BAJO``, ``IGUAL``.
Los ``IGUAL`` no se explican —decirle al cliente que su plan cuesta lo mismo que el mes
pasado es ruido—, pero **sí se calculan**, porque son los que garantizan que el diff
recorrió todos los conceptos y no se dejó ninguno fuera.
"""

from __future__ import annotations

import pytest

from packages.core_domain.enums import ClaseDelta, FamiliaConcepto
from packages.core_domain.esquemas.factset import LineaDelta
from packages.core_domain.esquemas.recibo import LineaRecibo
from packages.facts_engine.diff import (
    agrupar_por_concepto,
    comparar,
    comparar_detallado,
    contar_clases,
)


def linea(
    concepto_id: str,
    monto_cent: int,
    *,
    familia: FamiliaConcepto = FamiliaConcepto.RECURRENTE,
    linea_id: int = 1,
    periodo: str = "2026-07",
) -> LineaRecibo:
    """Línea de recibo mínima para las pruebas del diff."""
    return LineaRecibo(
        linea_id=linea_id,
        concepto_id=concepto_id,
        nombre_comercial=concepto_id.replace("_", " ").capitalize(),
        familia=familia,
        monto_cent=monto_cent,
        periodo=periodo,
    )


ACTUAL = [
    linea("RENTA_PLAN_MOVIL", 12_000, linea_id=1),
    linea("PAQUETE_DATOS_ADICIONAL", 2_000, familia=FamiliaConcepto.UNICO, linea_id=2),
    linea("IGV", 2_520, familia=FamiliaConcepto.IMPUESTO, linea_id=3),
]
PREVIO = [
    linea("RENTA_PLAN_MOVIL", 10_000, linea_id=1),
    linea("CARGO_RECONEXION", 2_500, familia=FamiliaConcepto.UNICO, linea_id=2),
    linea("IGV", 2_520, familia=FamiliaConcepto.IMPUESTO, linea_id=3),
]


# --------------------------------------------------------------------------- #
# Clasificación
# --------------------------------------------------------------------------- #
class TestClasificacion:
    """La clase la decide ``LineaDelta.clasificar``, nunca el llamador a mano."""

    @pytest.mark.parametrize(
        "actual,previo,esperada",
        [
            (2_000, 0, ClaseDelta.NUEVO),
            (0, 2_500, ClaseDelta.DESAPARECIDO),
            (12_000, 10_000, ClaseDelta.SUBIO),
            (10_000, 12_000, ClaseDelta.BAJO),
            (2_520, 2_520, ClaseDelta.IGUAL),
            (0, 0, ClaseDelta.IGUAL),
            (-1_000, 0, ClaseDelta.NUEVO),
            (-2_000, -1_000, ClaseDelta.BAJO),
        ],
    )
    def test_las_cinco_clases(self, actual: int, previo: int, esperada: ClaseDelta) -> None:
        assert LineaDelta.clasificar(actual, previo) is esperada

    def test_la_linea_valida_su_propia_clase_y_su_delta(self) -> None:
        """``extra="forbid"`` y validadores: una clase inventada falla ruidosamente."""
        with pytest.raises(ValueError):
            LineaDelta(
                concepto_id="RENTA_PLAN_MOVIL",
                nombre_comercial="Plan",
                clase=ClaseDelta.BAJO,  # mentira: 12000 > 10000
                monto_actual_cent=12_000,
                monto_previo_cent=10_000,
                delta_cent=2_000,
                confianza=1.0,
            )
        with pytest.raises(ValueError):
            LineaDelta(
                concepto_id="RENTA_PLAN_MOVIL",
                nombre_comercial="Plan",
                clase=ClaseDelta.SUBIO,
                monto_actual_cent=12_000,
                monto_previo_cent=10_000,
                delta_cent=9_999,  # delta que no cuadra con los montos
                confianza=1.0,
            )


# --------------------------------------------------------------------------- #
# FULL OUTER JOIN
# --------------------------------------------------------------------------- #
class TestComparar:
    """Se recorren los conceptos de ambos recibos, no solo los del actual."""

    def test_detecta_nuevo_desaparecido_y_subida(self) -> None:
        deltas = {item.concepto_id: item for item in comparar(ACTUAL, PREVIO)}

        assert deltas["PAQUETE_DATOS_ADICIONAL"].clase is ClaseDelta.NUEVO
        assert deltas["PAQUETE_DATOS_ADICIONAL"].delta_cent == 2_000
        assert deltas["CARGO_RECONEXION"].clase is ClaseDelta.DESAPARECIDO
        assert deltas["CARGO_RECONEXION"].delta_cent == -2_500
        assert deltas["RENTA_PLAN_MOVIL"].clase is ClaseDelta.SUBIO
        assert deltas["RENTA_PLAN_MOVIL"].delta_cent == 2_000

    def test_los_iguales_no_se_explican_pero_existen(self) -> None:
        sin_iguales = comparar(ACTUAL, PREVIO)
        con_iguales = comparar(ACTUAL, PREVIO, incluir_iguales=True)

        assert "IGV" not in {item.concepto_id for item in sin_iguales}
        igv = next(item for item in con_iguales if item.concepto_id == "IGV")
        assert igv.clase is ClaseDelta.IGUAL
        assert igv.delta_cent == 0
        assert igv.se_explica is False

    def test_la_suma_de_deltas_es_la_diferencia_de_totales(self) -> None:
        """Es el invariante de conciliación visto desde el diff."""
        total_actual = sum(item.monto_cent for item in ACTUAL)
        total_previo = sum(item.monto_cent for item in PREVIO)
        suma = sum(item.delta_cent for item in comparar(ACTUAL, PREVIO, incluir_iguales=True))
        assert suma == total_actual - total_previo

    def test_orden_determinista_por_impacto_absoluto(self) -> None:
        """Mayor ``|delta|`` primero; empates por ``concepto_id``. La demo es reproducible."""
        deltas = comparar(ACTUAL, PREVIO)
        claves = [(-abs(item.delta_cent), item.concepto_id) for item in deltas]
        assert claves == sorted(claves)

    def test_recibo_previo_vacio_todo_es_nuevo(self) -> None:
        deltas = comparar(ACTUAL, [])
        assert {item.clase for item in deltas} == {ClaseDelta.NUEVO}
        assert len(deltas) == len(ACTUAL)

    def test_recibo_actual_vacio_todo_desaparecio(self) -> None:
        deltas = comparar([], PREVIO)
        assert {item.clase for item in deltas} == {ClaseDelta.DESAPARECIDO}
        assert all(item.delta_cent < 0 for item in deltas)


class TestAgrupacion:
    """Varias líneas del mismo concepto se **suman** antes de comparar."""

    def test_dos_lineas_del_mismo_concepto_se_suman(self) -> None:
        lineas = [
            linea("PAQUETE_DATOS_ADICIONAL", 1_000, familia=FamiliaConcepto.UNICO, linea_id=1),
            linea("PAQUETE_DATOS_ADICIONAL", 1_500, familia=FamiliaConcepto.UNICO, linea_id=2),
        ]
        assert agrupar_por_concepto(lineas) == {"PAQUETE_DATOS_ADICIONAL": 2_500}

    def test_el_diff_compara_agregados_no_lineas_sueltas(self) -> None:
        actual = [
            linea("PAQUETE_DATOS_ADICIONAL", 1_000, familia=FamiliaConcepto.UNICO, linea_id=1),
            linea("PAQUETE_DATOS_ADICIONAL", 1_500, familia=FamiliaConcepto.UNICO, linea_id=2),
        ]
        previo = [linea("PAQUETE_DATOS_ADICIONAL", 2_000, familia=FamiliaConcepto.UNICO)]
        deltas = comparar(actual, previo)
        assert len(deltas) == 1
        assert deltas[0].delta_cent == 500


class TestResumenDetallado:
    """``comparar_detallado`` alimenta la auditoría y la regla dura de derivación."""

    def test_conteo_por_clase(self) -> None:
        resumen = comparar_detallado(ACTUAL, PREVIO)
        assert resumen.conteo == {
            "NUEVO": 1,
            "DESAPARECIDO": 1,
            "SUBIO": 1,
            "BAJO": 0,
            "IGUAL": 1,
        }
        assert contar_clases(resumen.todas) == resumen.conteo

    def test_detecta_conceptos_fuera_de_catalogo(self, reglas) -> None:
        """Un concepto sin ficha es una **regla dura** de derivación (sección 4.8)."""
        actual = [*ACTUAL, linea("CONCEPTO_QUE_NO_EXISTE", 500, familia=FamiliaConcepto.UNICO)]
        resumen = comparar_detallado(actual, PREVIO, reglas=reglas)
        assert "CONCEPTO_QUE_NO_EXISTE" in resumen.conceptos_fuera_catalogo

    def test_la_suma_de_deltas_del_resumen_cuadra(self) -> None:
        resumen = comparar_detallado(ACTUAL, PREVIO)
        assert resumen.suma_deltas_cent == sum(item.delta_cent for item in resumen.lineas)
