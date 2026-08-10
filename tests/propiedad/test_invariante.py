"""Propiedad nuclear: **para cualquier par de recibos, Σ deltas == Δ total**.

Una sola prueba, miles de casos. Es la que sostiene la promesa del proyecto: si la suma
de las variaciones por concepto no reprodujera exactamente la diferencia entre los dos
totales, la explicación tendría un agujero, y un agujero en una explicación financiera
es una mentira con otro nombre. Por eso, cuando el residual se sale de ±1 céntimo, el
sistema **no explica: deriva** (sección 4.6).

Hypothesis genera pares de recibos arbitrarios —conceptos que aparecen, desaparecen,
suben, bajan o no se mueven; importes positivos, negativos y cero; ciclos de 28 a 31
días— y comprueba la propiedad sobre el FactSet real que construye el motor, no sobre
una reimplementación de la fórmula.

El número de ejemplos se controla con ``HYPOTHESIS_MAX_EXAMPLES`` (400 por defecto,
suficiente para la suite local; en integración conviene subirlo a varios miles).
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from packages.core_domain.enums import FamiliaConcepto, ModalidadRenta
from packages.core_domain.esquemas.factset import TOLERANCIA_RESIDUAL_CENT, Invariante
from packages.core_domain.esquemas.recibo import LineaRecibo, Recibo
from packages.core_domain.reglas import cargar_reglas
from packages.facts_engine.invariante import residual_cent, verificar_conciliacion
from packages.facts_engine.motor import construir_factset

pytestmark = pytest.mark.propiedad

MAX_EJEMPLOS = int(os.getenv("HYPOTHESIS_MAX_EXAMPLES", "400"))

#: Conceptos reales del catálogo, con su familia, para que los recibos generados sean
#: verosímiles y el diff atraviese las mismas ramas que en producción.
_REGLAS = cargar_reglas()
CONCEPTOS: tuple[str, ...] = tuple(
    sorted(
        concepto.concepto_id
        for concepto in _REGLAS.catalogo
        if concepto.familia
        in (
            FamiliaConcepto.RECURRENTE,
            FamiliaConcepto.UNICO,
            FamiliaConcepto.AJUSTE,
            FamiliaConcepto.IMPUESTO,
        )
    )
)

#: Ciclos de los cuatro largos posibles, incluido febrero bisiesto.
CICLOS: tuple[tuple[date, date], ...] = (
    (date(2026, 2, 1), date(2026, 3, 1)),
    (date(2024, 2, 1), date(2024, 3, 1)),
    (date(2026, 4, 1), date(2026, 5, 1)),
    (date(2026, 7, 1), date(2026, 8, 1)),
)


def _lineas(detalle: dict[str, int]) -> list[LineaRecibo]:
    """Convierte ``{concepto_id: monto_cent}`` en líneas de recibo válidas."""
    return [
        LineaRecibo(
            linea_id=indice,
            concepto_id=concepto,
            nombre_comercial=concepto.replace("_", " ").capitalize(),
            familia=_REGLAS.familia(concepto) or FamiliaConcepto.UNICO,
            monto_cent=monto,
            periodo="2026-07",
        )
        for indice, (concepto, monto) in enumerate(sorted(detalle.items()), start=1)
    ]


def _recibo(detalle: dict[str, int], periodo: str, ciclo: tuple[date, date]) -> Recibo:
    """Recibo internamente consistente: ``Σ líneas == total_cent`` por construcción."""
    inicio, fin = ciclo
    anio, mes = (int(parte) for parte in periodo.split("-"))
    lineas = _lineas(detalle)
    inicio_periodo = inicio.replace(year=anio, month=mes)
    fin_periodo = fin.replace(year=anio if mes < 12 else anio + 1, month=mes % 12 + 1)
    return Recibo(
        recibo_id=f"R-PROP-{periodo}",
        cuenta_id="C-PROP-01",
        periodo=periodo,
        modalidad_renta=ModalidadRenta.VENCIDA,
        ciclo_inicio=inicio_periodo,
        ciclo_fin=fin_periodo,
        dias_ciclo=(fin_periodo - inicio_periodo).days,
        fecha_emision=fin_periodo,
        fecha_vencimiento=fin_periodo + timedelta(days=12),
        lineas=lineas,
        total_cent=sum(linea.monto_cent for linea in lineas),
    )


#: Un recibo es un mapa concepto → importe en céntimos enteros. Se permiten importes
#: negativos (notas de crédito, ajustes por suspensión) y cero.
detalle_recibo = st.dictionaries(
    keys=st.sampled_from(CONCEPTOS),
    values=st.integers(min_value=-500_000, max_value=500_000),
    min_size=0,
    max_size=8,
)


@settings(
    max_examples=MAX_EJEMPLOS,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    actual=detalle_recibo,
    previo=detalle_recibo,
    ciclo=st.sampled_from(CICLOS),
)
def test_la_suma_de_deltas_es_el_delta_total(
    actual: dict[str, int], previo: dict[str, int], ciclo: tuple[date, date]
) -> None:
    """Para **cualquier** par de recibos, la conciliación cierra exactamente.

    Se comprueban tres cosas sobre el FactSet que produce el motor:

    1. ``Σ delta_cent == total_actual − total_previo`` (residual exactamente 0; la
       tolerancia de ±1 existe para descuadres de origen, no para errores nuestros).
    2. ``invariante.ok`` es cierto y sus tres campos son coherentes entre sí.
    3. Ninguna línea con variación se queda fuera del reparto: los ``IGUAL`` aportan 0.
    """
    recibo_previo = _recibo(previo, "2026-06", ciclo)
    recibo_actual = _recibo(actual, "2026-07", ciclo)

    factset = construir_factset(recibo_actual, [recibo_previo], (), _REGLAS)

    suma = sum(linea.delta_cent for linea in factset.lineas)
    esperado = recibo_actual.total_cent - recibo_previo.total_cent

    assert factset.delta_total_cent == esperado
    assert suma == esperado, f"la conciliación no cierra: Σ deltas = {suma}, Δ total = {esperado}"
    assert factset.invariante.residual_cent == 0
    assert factset.invariante.ok is True
    assert factset.invariante.suma_deltas_cent == suma
    assert factset.invariante.delta_total_cent == esperado
    assert all(linea.delta_cent != 0 for linea in factset.lineas), (
        "las líneas sin variación no deben viajar en el FactSet"
    )


# --------------------------------------------------------------------------- #
# Propiedades del evaluador de conciliación, sin construir recibos
# --------------------------------------------------------------------------- #
@settings(max_examples=max(MAX_EJEMPLOS * 5, 1_000), deadline=None)
@given(
    total_actual=st.integers(min_value=-10_000_000, max_value=10_000_000),
    total_previo=st.integers(min_value=-10_000_000, max_value=10_000_000),
    deltas=st.lists(st.integers(min_value=-1_000_000, max_value=1_000_000), max_size=20),
)
def test_el_veredicto_del_invariante_es_consistente(
    total_actual: int, total_previo: int, deltas: list[int]
) -> None:
    """``ok`` ⟺ ``|residual| <= tolerancia``, y el residual es siempre el mismo número.

    Es la propiedad que la base de datos replica como ``CHECK``
    (``invariante_ok = (abs(residual_cent) <= 1)``): si Python y PostgreSQL discreparan,
    habría FactSets aceptados por un lado y rechazados por el otro.
    """
    veredicto = verificar_conciliacion(total_actual, total_previo, deltas)
    calculado = residual_cent(total_actual, total_previo, deltas)

    assert veredicto.residual_cent == calculado
    assert veredicto.residual_cent == (total_actual - total_previo) - sum(deltas)
    assert veredicto.suma_deltas_cent == sum(deltas)
    assert veredicto.delta_total_cent == total_actual - total_previo
    assert veredicto.ok is (abs(veredicto.residual_cent) <= TOLERANCIA_RESIDUAL_CENT)
    assert veredicto == Invariante.evaluar(total_actual - total_previo, deltas)
