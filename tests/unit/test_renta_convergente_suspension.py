"""REGRESIÓN: una renta suspendida no es un cambio de plan.

El defecto, tal y como apareció
------------------------------
Al ampliar la suite golden de 34 a más de 200 casos, el muestreo llegó por primera vez a
cuentas **CONVERGENTE** (Movistar Total) a las que les habían cortado el servicio por
morosidad. En esas cuentas la renta baja porque los días suspendidos no se cobran, y el
recibo decía esto::

    Cambio de plan: -S/ 49.01
    Reconexiones:    S/ 25.00

El cliente nunca cambió de plan. La aritmética cerraba —el invariante daba residual 0— y
la explicación era falsa, que es exactamente el mismo defecto que ya se había corregido
en la atribución del fin de descuento, pero por otra puerta: no por la heurística, sino
por una fila incompleta de ``regla_concepto_causa``.

``RENTA_MOVISTAR_TOTAL`` era el único concepto de renta que **no** admitía ``SUSPENSION``
entre sus causas posibles. Sin causa permitida, la línea se quedaba con ``causa = None``
y confianza 0,30 y heredaba la ``causa_oficial`` que el catálogo declara para el concepto
—``CAMBIO_DE_PLAN``, que es la correcta cuando la renta convergente se mueve por un
cambio de plan y una mentira cuando se mueve por una suspensión.

Eran 9 cuentas de 300 y ninguna caía en los 34 casos originales. Por eso se amplía una
suite: no para que el número quede más grande, sino para llegar a las esquinas.

Qué fija este módulo
--------------------
1. La **propiedad general**, no el caso concreto: toda renta recurrente puede verse
   afectada por una suspensión, así que todo concepto ``RENTA_*`` tiene que admitir
   ``SUSPENSION``. Escrito así, el test protege también a la renta que alguien añada
   mañana.
2. La atribución concreta, construida en código y sin depender del dataset generado.
3. Que el arreglo no se pasa de frenada: con un cambio de plan y una suspensión en el
   mismo ciclo sigue mandando el cambio de plan, porque ``SUSPENSION`` va la última de la
   fila y el orden de ``regla_concepto_causa`` es la preferencia declarada.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from packages.core_domain.enums import (
    CausaOficial,
    ClaseDelta,
    FamiliaConcepto,
    TipoMovimiento,
)
from packages.core_domain.esquemas.factset import LineaDelta
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.reglas import cargar_reglas
from packages.facts_engine.atribucion import atribuir

CUENTA = "C-TEST-MT"


def _renta_convergente(*, actual: int = 14_089, previo: int = 18_990) -> LineaDelta:
    """La renta de Movistar Total, que baja porque hubo días sin servicio."""
    return LineaDelta(
        concepto_id="RENTA_MOVISTAR_TOTAL",
        nombre_comercial="Movistar Total",
        clase=LineaDelta.clasificar(actual, previo),
        monto_actual_cent=actual,
        monto_previo_cent=previo,
        delta_cent=actual - previo,
        confianza=1.0,
        familia=FamiliaConcepto.RECURRENTE,
    )


def _movimiento(tipo: TipoMovimiento, identificador: int, dia: int) -> MovementEvent:
    """Movimiento del ciclo de julio de 2026."""
    detalle: dict[str, object] = {}
    if tipo is TipoMovimiento.CAMBIO_PLAN:
        detalle = {
            "plan_anterior": "Movistar Total Plus",
            "plan_nuevo": "Movistar Total Basico",
            "tarifa_anterior_cent": 18_990,
            "tarifa_nueva_cent": 14_990,
        }
    return MovementEvent(
        movimiento_id=identificador,
        cuenta_id=CUENTA,
        tipo=tipo,
        ocurrido_en=datetime(2026, 7, dia, 10, 0),
        detalle=detalle,
    )


@pytest.mark.parametrize(
    "concepto_id",
    sorted(
        concepto
        for concepto in cargar_reglas().regla_concepto_causa
        if concepto.startswith("RENTA_")
    ),
)
def test_toda_renta_admite_la_suspension(concepto_id: str) -> None:
    """Propiedad general: cualquier renta recurrente puede bajar por días sin servicio.

    Es la forma correcta de escribir esta regresión. Comprobar solo
    ``RENTA_MOVISTAR_TOTAL`` habría dejado el mismo agujero abierto para la próxima renta
    que se añada al catálogo.
    """
    permitidas = set(cargar_reglas().causas_permitidas(concepto_id))
    assert TipoMovimiento.SUSPENSION in permitidas, (
        f"{concepto_id} no admite SUSPENSION: una renta sin esa causa se queda sin "
        "atribuir cuando cortan el servicio y hereda la causa oficial del catálogo, "
        "que dice «cambio de plan»"
    )


def test_la_renta_convergente_suspendida_se_atribuye_a_la_suspension() -> None:
    """La causa es la suspensión, con causa oficial y confianza de causa única."""
    reglas = cargar_reglas()
    movimientos = [
        _movimiento(TipoMovimiento.SUSPENSION, 40_058_121, 17),
        _movimiento(TipoMovimiento.RECONEXION, 40_058_122, 25),
    ]

    (linea,) = atribuir([_renta_convergente()], movimientos, reglas, dias_ciclo=31)

    assert linea.clase is ClaseDelta.BAJO
    assert linea.causa is TipoMovimiento.SUSPENSION
    assert linea.causa_oficial is CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION
    assert linea.movimiento_id == 40_058_121
    assert linea.confianza == reglas.confianza.causa_unica


def test_la_renta_convergente_suspendida_no_se_narra_como_cambio_de_plan() -> None:
    """La frase exacta del defecto: la causa oficial no puede ser CAMBIO_DE_PLAN.

    Se comprueba por separado de la aserción anterior porque es la que le importa al
    cliente: la etiqueta que acaba impresa en su recibo.
    """
    reglas = cargar_reglas()
    (linea,) = atribuir(
        [_renta_convergente()],
        [_movimiento(TipoMovimiento.SUSPENSION, 1, 17)],
        reglas,
        dias_ciclo=31,
    )

    assert linea.causa_oficial is not CausaOficial.CAMBIO_DE_PLAN
    assert reglas.etiqueta_cliente(linea.concepto_id, linea.causa) == (
        "ajustes por días de suspensión"
    )


def test_con_cambio_de_plan_y_suspension_manda_el_cambio_de_plan() -> None:
    """El arreglo no invierte la prioridad: ``SUSPENSION`` va la última de la fila.

    Si en el mismo ciclo hubo cambio de plan y suspensión, lo que explica la renta nueva
    es el plan nuevo; el corte se explica con su propio ajuste. Es el mismo orden que
    tienen las demás rentas desde el principio, y este test impide "arreglar" el defecto
    convirtiendo toda bajada de renta en una suspensión.
    """
    reglas = cargar_reglas()
    movimientos = [
        _movimiento(TipoMovimiento.CAMBIO_PLAN, 10, 5),
        _movimiento(TipoMovimiento.SUSPENSION, 11, 17),
    ]

    (linea,) = atribuir([_renta_convergente()], movimientos, reglas, dias_ciclo=31)

    assert linea.causa is TipoMovimiento.CAMBIO_PLAN
    assert linea.causa_oficial is CausaOficial.CAMBIO_DE_PLAN
    assert linea.confianza == reglas.confianza.multiples_candidatos
    assert "mov:11" in linea.evidencia, "el movimiento descartado tiene que quedar citado"
