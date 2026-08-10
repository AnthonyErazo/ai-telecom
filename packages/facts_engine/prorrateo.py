"""Prorrateo, modalidades de renta y financiamiento de equipos (secciones 4.2 a 4.4).

Todo el módulo trabaja con enteros y ``Fraction``: **no existe un solo ``float`` en la
aritmética monetaria**. Las divisiones se cierran siempre con redondeo bancario sobre
enteros (``dinero.redondear_banca``), de modo que dos ejecuciones dan el mismo céntimo.

Contenido:

* Convenciones de días (``actual`` y ``30/360``) — el parámetro está **[POR VALIDAR]**.
* ``renta_del_ciclo``: la fórmula de tramos ``Σ P_j · len_j / D · facturable(e_j)``.
* Las dos modalidades como funciones separadas: ``total_vencida`` y ``total_adelantada``.
* El ajuste retroactivo de la renta adelantada, con sus dos componentes visibles.
* El sistema francés de las cuotas de equipo, que **nunca** se prorratea.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

from packages.core_domain.dinero import Centimos, prorratear, redondear_banca, repartir_mayor_resto
from packages.core_domain.enums import ConvencionProrrateo, EstadoServicio
from packages.core_domain.esquemas.movimiento import CuotaFinanciamiento, PlanFinanciamiento
from packages.core_domain.esquemas.recibo import Tramo

__all__ = [
    "AjusteRetroactivo",
    "ajuste_por_suspension",
    "ajuste_retroactivo",
    "ajuste_retroactivo_desde_tramos",
    "cronograma_frances",
    "cuota_equipo_financiado",
    "denominador_ciclo",
    "dias_30_360",
    "dias_para_prorrateo",
    "distribuir_renta_por_tramos",
    "renta_del_ciclo",
    "total_adelantada",
    "total_vencida",
]


# --------------------------------------------------------------------------- #
# Convención de días  [POR VALIDAR con el equipo de facturación de Movistar]
# --------------------------------------------------------------------------- #
def dias_30_360(inicio: date, fin: date, dias_base: int = 30) -> int:
    """Días entre ``[inicio, fin)`` con la convención 30/360 (base bonos, US).

    Regla clásica: todo mes tiene 30 días y todo año 360. Se ajustan los extremos
    (``d1 = 31 -> 30``; ``d2 = 31 -> 30`` solo si ``d1`` ya era 30 o 31), de modo que
    un mes natural completo siempre mide exactamente ``dias_base`` días.

    ``dias_30_360(date(2026,1,31), date(2026,3,1)) == 30``.
    """
    dia_inicio = min(inicio.day, dias_base)
    dia_fin = fin.day
    if dia_fin > dias_base and dia_inicio >= dias_base:
        dia_fin = dias_base
    return (
        360 * (fin.year - inicio.year)
        + dias_base * (fin.month - inicio.month)
        + (dia_fin - dia_inicio)
    )


def dias_para_prorrateo(
    inicio: date,
    fin: date,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
) -> int:
    """Numerador del prorrateo de un tramo ``[inicio, fin)`` según la convención.

    Con ``actual`` son los días reales (28/29/30/31); con ``30_360`` se cuenta con
    meses de ``dias_base_30_360`` días.
    """
    if convencion is ConvencionProrrateo.TREINTA_360:
        return dias_30_360(inicio, fin, dias_base_30_360)
    return (fin - inicio).days


def denominador_ciclo(
    dias_reales: int,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
) -> int:
    """Denominador ``D`` del prorrateo: días reales del ciclo o los 30 de la convención.

    Raises:
        ValueError: si ``dias_reales`` no es positivo.
    """
    if dias_reales <= 0:
        raise ValueError(f"un ciclo no puede tener {dias_reales} días")
    if convencion is ConvencionProrrateo.TREINTA_360:
        return dias_base_30_360
    return dias_reales


# --------------------------------------------------------------------------- #
# 4.1 — Renta del ciclo a partir de la tabla de tramos
# --------------------------------------------------------------------------- #
def _tramo_se_cobra(tramo: Tramo, cobrar_en_suspension: bool) -> bool:
    """Aplica la política de cobro en suspensión al ``facturable`` del tramo."""
    if tramo.estado is EstadoServicio.SUSPENDIDO:
        return cobrar_en_suspension
    return tramo.facturable


def renta_del_ciclo(
    tramos: Sequence[Tramo],
    dias_ciclo: int,
    cobrar_en_suspension: bool = False,
    *,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
) -> Centimos:
    """Renta devengada en el ciclo sumando los tramos (fórmula 4.1).

    ``RENTA_ciclo = Σ_j  P_j · len_j / D · facturable(e_j)``

    donde ``P_j`` es la tarifa mensual vigente en el tramo (con el descuento ya
    aplicado), ``len_j`` sus días y ``D`` los días del ciclo. Cada término se redondea
    a céntimo por separado, que es lo que hace el facturador y lo que permite que la
    **tabla de tramos sea la explicación**: cada fila del recibo cuadra con su tramo.

    Los tramos SUSPENDIDOS no se cobran salvo que ``cobrar_en_suspension`` sea ``True``
    (``politica.cobro_en_suspension`` de ``rules.yaml``, **[POR VALIDAR]**).

    Ejemplo (cambio de plan el día 11 de un ciclo de 30 días)::

        [P=9000 del 1 al 10, P=9900 del 11 al 30]
        -> 9000·10/30 + 9900·20/30 = 3000 + 6600 = 9600 céntimos

    Raises:
        ValueError: si ``dias_ciclo`` no es positivo.
    """
    if dias_ciclo <= 0:
        raise ValueError(f"días de ciclo inválidos: {dias_ciclo}")
    total = 0
    for tramo in tramos:
        if not _tramo_se_cobra(tramo, cobrar_en_suspension):
            continue
        dias = dias_para_prorrateo(tramo.inicio, tramo.fin, convencion, dias_base_30_360)
        total += prorratear(tramo.tarifa_mensual_cent, dias, dias_ciclo)
    return total


def ajuste_por_suspension(
    tramos: Sequence[Tramo],
    dias_ciclo: int,
    *,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
) -> Centimos:
    """Ajuste **negativo** por los días de suspensión (fórmula 4.3).

    ``Ajuste_susp = − Σ_{j: SUSPENDIDO} P_j · len_j / D``

    Es lo que se devuelve al cliente por los días en que no pudo usar el servicio.
    Devuelve 0 si no hubo tramos suspendidos. En renta ADELANTADA este importe aparece
    como ajuste retroactivo en el recibo del ciclo siguiente.
    """
    if dias_ciclo <= 0:
        raise ValueError(f"días de ciclo inválidos: {dias_ciclo}")
    devolucion = 0
    for tramo in tramos:
        if tramo.estado is not EstadoServicio.SUSPENDIDO:
            continue
        dias = dias_para_prorrateo(tramo.inicio, tramo.fin, convencion, dias_base_30_360)
        devolucion += prorratear(tramo.tarifa_mensual_cent, dias, dias_ciclo)
    return -devolucion


def distribuir_renta_por_tramos(total_cent: Centimos, tramos: Sequence[Tramo]) -> list[Centimos]:
    """Reparte un total ya fijado entre los tramos, sin perder ni un céntimo.

    Los pesos son ``tarifa_mensual_cent · dias`` de cada tramo facturable. Se usa cuando
    el importe del recibo manda (viene del facturador) y hay que explicar cómo se
    reparte: ``sum(resultado) == total_cent`` siempre, por reparto de mayor resto (4.5).

    Los tramos no facturables reciben 0.
    """
    pesos = [
        (tramo.tarifa_mensual_cent * tramo.dias) if tramo.facturable else 0 for tramo in tramos
    ]
    if not pesos or sum(pesos) == 0:
        if total_cent != 0 and pesos:
            # Sin base de reparto, todo el importe va al primer tramo: no se pierde nada.
            return [total_cent] + [0] * (len(pesos) - 1)
        return [0] * len(pesos)
    return repartir_mayor_resto(total_cent, pesos)


# --------------------------------------------------------------------------- #
# 4.2 — Renta VENCIDA y renta ADELANTADA
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AjusteRetroactivo:
    """Las dos mitades del ajuste retroactivo de la renta adelantada.

    Se guardan por separado porque el recibo las muestra como dos líneas y el cliente
    necesita ver ambas: el reverso de lo que ya se le había cobrado y el recobro al
    precio nuevo. ``total_cent`` es lo que finalmente altera el recibo.
    """

    reverso_cent: Centimos
    """``− P_old · d_new / D``: devolución de lo cobrado por adelantado al plan anterior."""

    recobro_cent: Centimos
    """``+ P_new · d_new / D``: cobro de esos mismos días al plan nuevo."""

    total_cent: Centimos
    """Suma de ambos componentes: ``(P_new − P_old) · d_new / D``."""

    dias_nuevo_plan: int
    dias_ciclo: int
    tarifa_anterior_cent: Centimos
    tarifa_nueva_cent: Centimos


def ajuste_retroactivo(
    *,
    tarifa_anterior_cent: Centimos,
    tarifa_nueva_cent: Centimos,
    dias_nuevo_plan: int,
    dias_ciclo: int,
) -> AjusteRetroactivo:
    """Ajuste retroactivo de un cambio de plan en renta ADELANTADA (fórmula 4.2).

    ``AJUSTE_RETRO_k = − P_old·(d_new/D) + P_new·(d_new/D) = (P_new − P_old)·(d_new/D)``

    ``d_new`` son los días del ciclo *ya cobrado por adelantado* en los que estuvo
    vigente el plan nuevo. Las tarifas son **efectivas**: llevan el descuento vigente
    ya aplicado, igual que ``Tramo.tarifa_mensual_cent``.

    Se calcula por componentes (dos redondeos, uno por línea del recibo) en lugar de
    redondear la diferencia: así cada línea que ve el cliente cuadra exactamente con
    el importe que se le presenta, que es lo que exige la auditoría numérica.

    Raises:
        ValueError: si los días son incoherentes con el ciclo.
    """
    if dias_ciclo <= 0:
        raise ValueError(f"días de ciclo inválidos: {dias_ciclo}")
    if not 0 <= dias_nuevo_plan <= dias_ciclo:
        raise ValueError(
            f"días del plan nuevo fuera del ciclo: {dias_nuevo_plan} de {dias_ciclo}"
        )
    reverso = -prorratear(tarifa_anterior_cent, dias_nuevo_plan, dias_ciclo)
    recobro = prorratear(tarifa_nueva_cent, dias_nuevo_plan, dias_ciclo)
    return AjusteRetroactivo(
        reverso_cent=reverso,
        recobro_cent=recobro,
        total_cent=reverso + recobro,
        dias_nuevo_plan=dias_nuevo_plan,
        dias_ciclo=dias_ciclo,
        tarifa_anterior_cent=tarifa_anterior_cent,
        tarifa_nueva_cent=tarifa_nueva_cent,
    )


def ajuste_retroactivo_desde_tramos(
    tramos: Sequence[Tramo],
    tarifa_cobrada_cent: Centimos,
    dias_ciclo: int,
    *,
    cobrar_en_suspension: bool = False,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
) -> Centimos:
    """Generalización del ajuste retroactivo a cualquier número de eventos.

    ``AJUSTE_RETRO = RENTA_real_del_ciclo(tramos) − renta cobrada por adelantado``

    Con un único cambio de plan coincide con :func:`ajuste_retroactivo`; con varios
    cambios, una suspensión o una baja, sigue siendo correcto sin fórmulas nuevas.
    Ese es el motivo de modelar por tramos y no por escenario: un solo algoritmo cubre
    los cinco casos de la ficha.

    Ejemplo: ciclo de 30 días cobrado por adelantado a S/ 90.00; cambio de plan el día
    11 a una tarifa efectiva de S/ 99.00 ⇒ renta real ``3000 + 6600 = 9600`` céntimos,
    ajuste ``9600 − 9000 = +600`` céntimos.
    """
    real = renta_del_ciclo(
        tramos,
        dias_ciclo,
        cobrar_en_suspension,
        convencion=convencion,
        dias_base_30_360=dias_base_30_360,
    )
    return real - tarifa_cobrada_cent


def total_vencida(
    *,
    renta_ciclo_cent: Centimos,
    consumo_cent: Centimos = 0,
    cuotas_cent: Centimos = 0,
    cargos_cent: Centimos = 0,
    creditos_cent: Centimos = 0,
) -> Centimos:
    """Total del recibo en renta VENCIDA (fórmula 4.2).

    ``T_k = RENTA_ciclo_k + CONSUMO_k + CUOTAS_k + CARGOS_k − CREDITOS_k``

    El recibo del ciclo *k* cobra el ciclo *k* que **ya cerró**: todo lo que aparece
    corresponde a días pasados, así que un cambio de plan a mitad de ciclo se ve
    simplemente como la renta partida en dos tramos, sin ajustes de ningún tipo.

    ``creditos_cent`` se pasa como **magnitud positiva** (lo que resta del recibo).
    """
    return renta_ciclo_cent + consumo_cent + cuotas_cent + cargos_cent - creditos_cent


def total_adelantada(
    *,
    renta_anticipada_cent: Centimos,
    ajuste_retro_cent: Centimos = 0,
    consumo_cent: Centimos = 0,
    cuotas_cent: Centimos = 0,
    cargos_cent: Centimos = 0,
    creditos_cent: Centimos = 0,
) -> Centimos:
    """Total del recibo en renta ADELANTADA (fórmula 4.2).

    ``T_k = P_new + AJUSTE_RETRO_k + CONSUMO_k + CUOTAS_k + CARGOS_k − CREDITOS_k``

    El recibo del ciclo *k* cobra **por adelantado** la renta del ciclo *k+1* y a la vez
    **corrige** el ciclo *k*, que ya se había cobrado con la tarifa antigua. Por eso
    conviven dos rentas en el mismo documento.

    INSIGHT CENTRAL DEL PROYECTO — *el recibo sube aunque el plan nuevo sea más barato*.

    Caso real (es la consulta nº 1 del 104), con céntimos::

        Plan anterior "Max 120":  lista 12000, promoción vigente −3000  -> efectiva  9000
        Plan nuevo    "Ligero 99": lista  9900, sin promoción            -> efectiva  9900
        Ciclo de julio: D = 30 días. El cambio se hace el 11 de julio -> d_new = 20 días.

        Recibo de JUNIO (previo): renta anticipada de julio al plan anterior  =  9000
                                  TOTAL PREVIO                                  9000

        Recibo de JULIO (actual):
          renta anticipada de agosto, plan nuevo                            =  9900
          ajuste retroactivo de julio:
              reverso  − 9000 · 20/30                                      = −6000
              recobro  + 9900 · 20/30                                      = +6600
              AJUSTE_RETRO                                                 =  +600
                                  TOTAL ACTUAL                               10500

        Δ = +1500 céntimos (+16,7 %) con un plan cuyo precio de lista BAJÓ de
        S/ 120.00 a S/ 99.00.

    Dos mecanismos, ambos invisibles para el cliente, explican la subida:

    1. La promoción estaba atada al plan anterior y **murió con el cambio**: la tarifa
       *efectiva* pasó de S/ 90.00 a S/ 99.00 aunque la de *lista* bajara. El motor lo
       ve porque los tramos llevan la tarifa con descuento ya aplicado.
    2. El ajuste retroactivo, que el cliente espera *a su favor*, resulta **positivo**:
       los 20 días de julio que se le habían cobrado a S/ 90.00 se recobran a S/ 99.00.

    Con renta VENCIDA el mismo cambio se vería como una renta partida en dos tramos y
    sin ajuste: de ahí que la modalidad forme parte de la firma causal del FactSet.
    """
    return (
        renta_anticipada_cent
        + ajuste_retro_cent
        + consumo_cent
        + cuotas_cent
        + cargos_cent
        - creditos_cent
    )


# --------------------------------------------------------------------------- #
# 4.4 — Cuota de equipo financiado (sistema francés). NUNCA se prorratea.
# --------------------------------------------------------------------------- #
def _tasa_como_fraccion(tasa: int | Fraction | Decimal | str) -> Fraction:
    """Normaliza la tasa mensual a ``Fraction`` exacta.

    * ``int``  -> **puntos básicos** (``200`` = 2,00 % mensual), como en Amdocs.
    * ``Fraction`` / ``Decimal`` / ``str`` -> tasa decimal directa (``"0.02"`` = 2 %).

    Raises:
        TypeError: si se pasa un número de coma flotante (regla innegociable nº 1:
            una tasa binaria inexacta contaminaría todo el cronograma).
        ValueError: si la tasa es negativa.
    """
    if isinstance(tasa, bool):
        raise TypeError("un booleano no es una tasa")
    if isinstance(tasa, float):  # se rechaza el tipo a propósito
        raise TypeError(
            "la tasa no admite coma flotante: use puntos básicos (int), Fraction o Decimal"
        )
    if isinstance(tasa, int):
        valor = Fraction(tasa, 10_000)
    elif isinstance(tasa, Fraction):
        valor = tasa
    else:
        valor = Fraction(str(tasa))
    if valor < 0:
        raise ValueError(f"tasa negativa: {tasa!r}")
    return valor


def _redondear(valor: Fraction) -> int:
    """Redondeo bancario de una fracción exacta a céntimos enteros."""
    return redondear_banca(valor.numerator, valor.denominator)


def _cuota_teorica(capital_cent: Centimos, interes: Fraction, n_cuotas: int) -> Centimos:
    """Cuota constante del sistema francés, en céntimos.

    ``A = K · i / (1 − (1+i)^(−n))``, reescrito como ``K · i · (1+i)^n / ((1+i)^n − 1)``
    para operar solo con racionales exactos. Con ``i == 0`` degenera a ``A = K / n``.
    """
    if interes == 0:
        return _redondear(Fraction(capital_cent, n_cuotas))
    factor = (1 + interes) ** n_cuotas
    return _redondear(Fraction(capital_cent) * interes * factor / (factor - 1))


def _amortizar(
    capital_cent: Centimos, interes: Fraction, n_cuotas: int
) -> list[CuotaFinanciamiento]:
    """Construye el cronograma completo con saldos enteros y ``B_n == 0`` garantizado.

    ``B_m = B_{m−1}·(1+i) − A`` con todo en céntimos. La última cuota absorbe el
    céntimo residual: ``A_n = B_{n−1}·(1+i)``, de modo que el saldo cierra exacto y
    ``Σ amortizaciones == K``.
    """
    if n_cuotas <= 0:
        raise ValueError(f"un financiamiento necesita al menos una cuota: {n_cuotas}")
    if capital_cent < 0:
        raise ValueError(f"capital negativo: {capital_cent}")

    cuota = _cuota_teorica(capital_cent, interes, n_cuotas)
    cronograma: list[CuotaFinanciamiento] = []
    saldo = capital_cent

    for numero in range(1, n_cuotas + 1):
        interes_cent = _redondear(Fraction(saldo) * interes)
        if numero == n_cuotas:
            amortizacion = saldo
            importe = saldo + interes_cent
        else:
            amortizacion = cuota - interes_cent
            if amortizacion <= 0 and capital_cent > 0:
                raise ValueError(
                    f"la cuota {cuota} no cubre el interés {interes_cent}: "
                    "el saldo nunca se amortizaría"
                )
            if amortizacion > saldo:  # defensa ante redondeos en la penúltima cuota
                amortizacion = saldo
            importe = amortizacion + interes_cent
        siguiente = saldo - amortizacion
        cronograma.append(
            CuotaFinanciamiento(
                numero=numero,
                de_total=n_cuotas,
                monto_cent=importe,
                interes_cent=interes_cent,
                amortizacion_cent=amortizacion,
                saldo_inicial_cent=saldo,
                saldo_final_cent=siguiente,
            )
        )
        saldo = siguiente

    if saldo != 0:  # pragma: no cover - la última cuota lo garantiza por construcción
        raise ValueError(f"el saldo final debe ser 0 y quedó en {saldo}")
    return cronograma


def cuota_equipo_financiado(
    capital_cent: Centimos,
    tasa: int | Fraction | Decimal | str,
    n_cuotas: int,
    m_actual: int,
) -> tuple[Centimos, Centimos]:
    """Cuota del mes ``m_actual`` y saldo que queda tras pagarla (fórmula 4.4).

    Sistema francés::

        A = K · i / (1 − (1+i)^(−n))        si i > 0
        A = K / n                            si i == 0
        B_m = B_{m−1}·(1+i) − A,  B_0 = K,   invariante B_n == 0
        A_n = B_{n−1}·(1+i)                  (la última cuota absorbe el céntimo)

    La cuota **nunca se prorratea**: si el equipo se compró a mitad de mes se cobra
    igual, completa. Clave explicativa para el cliente: *"cuota 3 de 18"*.

    Args:
        capital_cent: capital financiado ``K``, en céntimos.
        tasa: mensual. ``int`` = puntos básicos (``200`` = 2 %); ``Fraction``/``Decimal``/
            ``str`` = tasa decimal. Se rechaza la coma flotante.
        n_cuotas: número total de cuotas ``n``.
        m_actual: cuota que se está facturando, 1-indexada.

    Returns:
        ``(cuota_cent, saldo_restante_cent)``. En la última cuota el saldo es 0, que es
        la señal que dispara el **efecto efervescente** ("su equipo queda pagado").

    Raises:
        ValueError: si ``m_actual`` está fuera de ``[1, n_cuotas]`` o los datos son
            incoherentes.
    """
    if not 1 <= m_actual <= n_cuotas:
        raise ValueError(f"cuota {m_actual} fuera de rango para un plan de {n_cuotas}")
    cronograma = _amortizar(capital_cent, _tasa_como_fraccion(tasa), n_cuotas)
    cuota = cronograma[m_actual - 1]
    return cuota.monto_cent, cuota.saldo_final_cent


def cronograma_frances(
    equipo: str,
    capital_cent: Centimos,
    tasa: int | Fraction | Decimal | str,
    n_cuotas: int,
    *,
    movimiento_id: int | None = None,
) -> PlanFinanciamiento:
    """Cronograma completo de un equipo financiado, listo para el FactSet.

    Devuelve un :class:`PlanFinanciamiento` ya validado: el saldo final de la última
    cuota es 0 y la suma de amortizaciones es exactamente el principal.
    """
    interes = _tasa_como_fraccion(tasa)
    cronograma = _amortizar(capital_cent, interes, n_cuotas)
    return PlanFinanciamiento(
        equipo=equipo,
        principal_cent=capital_cent,
        cuotas_totales=n_cuotas,
        tasa_mensual_bp=_redondear(interes * 10_000),
        cronograma=cronograma,
        movimiento_id=movimiento_id,
    )
