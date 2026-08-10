"""Atribución de causa a cada variación (sección 4.7).

Emparejar una línea que subió con el movimiento del CRM que la explica es el paso donde
un sistema ingenuo alucina. Aquí no se adivina: la tabla estática ``regla_concepto_causa``
de ``rules.yaml`` dice qué movimientos **pueden** explicar cada concepto, y si en la
ventana del ciclo no hay ninguno, la causa queda en ``None`` con confianza baja y el
recibo se explica sin inventar el porqué.

Reglas de confianza (literales de 4.7):

* 1 candidato   -> ``causa``, ``confianza = 0.98``
* 0 candidatos  -> ``causa = None``, ``confianza = 0.30``
* >1 candidato  -> el más cercano en el tiempo, ``confianza = 0.65``
* prorrateable con recálculo que no cuadra en ±1 céntimo -> ``confianza = min(c, 0.50)``

Los valores viven en ``rules.yaml`` (``confianza:``), no en el código.

**Preferencia de causa (extensión de 4.7).** Elegir "el movimiento más cercano en el
tiempo" es una heurística de desempate, y aplicada a ciegas produce narrativas falsas:
si un cambio de plan cancela una promoción atada al plan anterior, el único movimiento
del ciclo es el ``CAMBIO_PLAN`` y la desaparición del descuento quedaba etiquetada como
*"cambio de plan"* — cuando el cambio de plan, por sí solo, **bajó** el recibo. Por eso
la atribución se resuelve en dos niveles, ambos declarados en ``rules.yaml`` y ninguno
codificado para un concepto concreto:

1. ``preferencia_causa[concepto][clase]`` — **regla de concepto**. Fija la causa por lo
   que la variación *es*, no por lo que hay cerca en el tiempo, y no exige que exista un
   movimiento que la respalde. Si lo hay, se cita y la confianza es la de causa única;
   si no, la confianza es ``confianza.regla_concepto``.
2. El **orden** de ``regla_concepto_causa[concepto]`` — desempate entre varios
   candidatos: primero por preferencia declarada, y solo dentro de la misma causa por
   cercanía temporal.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from packages.core_domain.dinero import prorratear
from packages.core_domain.enums import CausaOficial, FamiliaConcepto, TipoMovimiento
from packages.core_domain.esquemas.factset import LineaDelta
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas

__all__ = [
    "CONCEPTOS_DERIVADOS",
    "EVIDENCIA_DERIVADO",
    "EVIDENCIA_PREFERENCIA",
    "EVIDENCIA_REGLA",
    "atribuir",
    "candidatos_para",
    "elegir_candidato",
    "esta_atribuida",
]

#: Conceptos cuya variación es consecuencia aritmética del resto del recibo: no existe
#: ni puede existir un movimiento que los explique, y aun así están perfectamente
#: explicados (el IGV se mueve porque se movió la base afecta).
CONCEPTOS_DERIVADOS: frozenset[str] = frozenset({"IGV", "REDONDEO"})

#: Marca de evidencia de una línea derivada del propio recibo.
EVIDENCIA_DERIVADO = "regla:derivado_del_recibo"

#: Marca de evidencia de la tabla de atribución.
EVIDENCIA_REGLA = "regla:regla_concepto_causa"

#: Prefijo de evidencia de la preferencia de causa por concepto y clase de variación.
#: Se completa con ``:{concepto_id}:{clase}`` para que el asesor vea exactamente qué
#: fila de ``preferencia_causa`` decidió la atribución.
EVIDENCIA_PREFERENCIA = "regla:preferencia_causa"


def _servicio_del_movimiento(movimiento: MovementEvent) -> str | None:
    """``servicio_id`` del evento, mirando también dentro del detalle."""
    if movimiento.servicio_id:
        return movimiento.servicio_id
    valor = movimiento.detalle.get("servicio_id")
    return str(valor) if valor else None


def _clave_orden(movimiento: MovementEvent) -> tuple[object, int]:
    """Orden estable: por instante y, en empate, por identificador."""
    return (movimiento.ocurrido_en, movimiento.movimiento_id)


def candidatos_para(
    concepto_id: str,
    movimientos: Sequence[MovementEvent],
    reglas: ConfiguracionReglas,
    *,
    servicio_id: str | None = None,
) -> list[MovementEvent]:
    """Movimientos de la ventana que **pueden** explicar el concepto, en orden temporal.

    Filtra por ``regla_concepto_causa`` y, si se indica, por servicio. La ventana ya
    viene acotada al ciclo por el llamador (``motor.movimientos_del_ciclo``).
    """
    permitidas = set(reglas.causas_permitidas(concepto_id))
    if not permitidas:
        return []
    candidatos = [
        movimiento
        for movimiento in movimientos
        if movimiento.tipo in permitidas
        and (servicio_id is None or _servicio_del_movimiento(movimiento) in (None, servicio_id))
    ]
    candidatos.sort(key=_clave_orden)
    return candidatos


def elegir_candidato(
    concepto_id: str,
    candidatos: Sequence[MovementEvent],
    reglas: ConfiguracionReglas,
) -> MovementEvent | None:
    """Elige, entre varios candidatos, el que de verdad explica la variación.

    Criterio, en este orden:

    1. **Preferencia declarada**: la posición del tipo de movimiento dentro de
       ``regla_concepto_causa[concepto_id]``. La tabla deja de ser un conjunto y pasa a
       ser una lista de prioridad, lo que la hace extensible sin tocar código: para que
       otro concepto resuelva distinto basta reordenar su fila del YAML.
    2. **Cercanía en el tiempo** (sección 4.7), como desempate dentro de la misma causa:
       gana el último movimiento del ciclo, que es el que dejó el recibo como está.

    Returns:
        El movimiento ganador, o ``None`` si no hay candidatos.
    """
    if not candidatos:
        return None
    mejor = min(reglas.indice_preferencia(concepto_id, m.tipo) for m in candidatos)
    preferidos = [
        movimiento
        for movimiento in candidatos
        if reglas.indice_preferencia(concepto_id, movimiento.tipo) == mejor
    ]
    return max(preferidos, key=_clave_orden)


def _prorrateo_esperado(linea: LineaDelta, dias_ciclo: int) -> int | None:
    """Recalcula el importe que deberían dar los tramos de la línea, o ``None``.

    ``Σ_j P_j · len_j / D`` sobre los tramos facturables. Se recalcula desde la tarifa y
    los días —no se confía en ``monto_prorrateado_cent``— porque el objetivo es
    justamente detectar que el importe facturado no cuadra con su propia explicación.
    """
    if not linea.tramos or dias_ciclo <= 0:
        return None
    return sum(
        prorratear(tramo.tarifa_mensual_cent, tramo.dias, dias_ciclo)
        for tramo in linea.tramos
        if tramo.facturable
    )


def _dias_prorrateo(linea: LineaDelta) -> int | None:
    """Días efectivamente cobrados según los tramos, si la línea no los trae ya."""
    if linea.dias_prorrateo is not None:
        return linea.dias_prorrateo
    if not linea.tramos:
        return None
    return sum(tramo.dias for tramo in linea.tramos if tramo.facturable)


def _es_derivado(
    linea: LineaDelta, reglas: ConfiguracionReglas, conceptos_derivados: Collection[str]
) -> bool:
    """Si la línea es una consecuencia aritmética del recibo (IGV, redondeo)."""
    if linea.concepto_id in conceptos_derivados:
        return True
    familia = linea.familia or reglas.familia(linea.concepto_id)
    return familia is FamiliaConcepto.IMPUESTO


def atribuir(
    deltas: Sequence[LineaDelta],
    movimientos: Sequence[MovementEvent],
    reglas: ConfiguracionReglas | None = None,
    *,
    dias_ciclo: int | None = None,
    conceptos_derivados: Collection[str] = CONCEPTOS_DERIVADOS,
) -> list[LineaDelta]:
    """Asigna causa y confianza a cada variación (sección 4.7).

    Para cada línea se buscan los movimientos del ciclo compatibles con su concepto
    según ``regla_concepto_causa`` y se aplica la tabla de confianzas::

        len(candidatos) == 1  ->  causa = candidato,           confianza = causa_unica (0.98)
        len(candidatos) == 0  ->  causa = None,                confianza = sin_candidato (0.30)
        len(candidatos)  > 1  ->  causa = el más reciente,      confianza = multiples (0.65)

    y, si el concepto es prorrateable y la línea trae tramos, se recalcula el importe
    esperado: si difiere del facturado en más de ``tolerancia_prorrateo_cent``, la
    confianza se topa en ``tope_prorrateo_inconsistente`` (0.50). Un prorrateo que no
    reproduce el importe del recibo es exactamente el caso en el que **no** se debe
    afirmar nada al cliente.

    Antes de esa tabla se consulta ``preferencia_causa[concepto][clase]``: si el concepto
    declara una **regla de concepto** para esa clase de variación, esa causa gana sobre
    cualquier movimiento de la ventana y **no exige** que exista una orden que la
    respalde. Con orden que la respalde, la confianza es ``causa_unica``; sin ella,
    ``regla_concepto`` (0.90). Los candidatos descartados quedan en la evidencia.

    Es lo que impide la narrativa engañosa del cambio de plan que mata una promoción: la
    desaparición del ``DESCUENTO_PROMOCIONAL`` se atribuye a ``FIN_DESCUENTO`` aunque el
    único movimiento del ciclo sea el ``CAMBIO_PLAN``, de modo que el aumento se explica
    por el fin del descuento y el cambio de plan se narra —correctamente— como ahorro.

    Tres matices, documentados porque extienden 4.7 sin contradecirlo:

    * Conceptos **derivados** (IGV, redondeo, familia IMPUESTO): no existe movimiento
      que los explique y aun así están explicados por construcción. Se marcan con
      ``evidencia = [EVIDENCIA_DERIVADO]``, causa ``None`` y confianza máxima.
    * Conceptos **sin causas permitidas** en la tabla pero con causa oficial en el
      catálogo (consumo fuera de plan, larga distancia): la causa del CRM sigue siendo
      ``None`` —no hubo ninguna orden— pero se conserva la ``causa_oficial`` del
      catálogo (*"cargos adicionales"*) con confianza de causa única: el mapeo
      concepto -> causa oficial es 1 a 1 y no hay ambigüedad que resolver.

    Args:
        deltas: salida de ``diff.comparar``.
        movimientos: movimientos de la ventana del ciclo, en cualquier orden.
        reglas: configuración cargada; por defecto ``cargar_reglas()``.
        dias_ciclo: ``D`` para el recálculo del prorrateo. Sin él no se verifica.
        conceptos_derivados: conceptos tratados como consecuencia aritmética.

    Returns:
        Una lista nueva de ``LineaDelta``, en el mismo orden, con ``causa``,
        ``causa_oficial``, ``movimiento_id``, ``confianza`` y ``evidencia`` completados.
        Las líneas de entrada no se modifican.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    parametros = configuracion.confianza
    atribuidas: list[LineaDelta] = []

    for linea in deltas:
        datos = linea.model_dump()
        evidencia = list(linea.evidencia)
        causa: TipoMovimiento | None = None
        causa_oficial: CausaOficial | None = None
        movimiento_id = linea.movimiento_id

        if _es_derivado(linea, configuracion, conceptos_derivados):
            confianza = parametros.causa_unica
            if EVIDENCIA_DERIVADO not in evidencia:
                evidencia.append(EVIDENCIA_DERIVADO)
        else:
            candidatos = candidatos_para(linea.concepto_id, movimientos, configuracion)
            permitidas = configuracion.causas_permitidas(linea.concepto_id)
            preferida = configuracion.causa_preferida(linea.concepto_id, linea.clase)
            if preferida is not None:
                # REGLA DE CONCEPTO: la causa la fija lo que la variación *es*, no el
                # movimiento que más cerca quedó. Si el CRM emitió la orden, se cita y
                # la confianza es la de causa única; si no, la regla sigue valiendo (un
                # descuento que desaparece es una promoción terminada) pero se declara
                # con una confianza propia, más baja que la de una causa documentada.
                causa = preferida
                respaldo = [movimiento for movimiento in candidatos if movimiento.tipo is preferida]
                evidencia.append(f"{EVIDENCIA_PREFERENCIA}:{linea.concepto_id}:{linea.clase}")
                if respaldo:
                    elegido = respaldo[-1]
                    movimiento_id = elegido.movimiento_id
                    confianza = parametros.causa_unica
                else:
                    movimiento_id = None
                    confianza = parametros.regla_concepto
                # Los movimientos descartados no desaparecen: son justamente los que la
                # atribución ingenua habría elegido y el asesor tiene que poder verlos.
                evidencia.extend(
                    f"mov:{otro.movimiento_id}"
                    for otro in candidatos
                    if otro.movimiento_id != movimiento_id
                )
            elif len(candidatos) == 1:
                elegido = candidatos[0]
                causa = elegido.tipo
                movimiento_id = elegido.movimiento_id
                confianza = parametros.causa_unica
            elif len(candidatos) > 1:
                # Preferencia declarada primero y, dentro de la misma causa, "el más
                # cercano en el tiempo": el último movimiento del ciclo, que es el que
                # dejó el recibo como está.
                elegido = elegir_candidato(linea.concepto_id, candidatos, configuracion)
                if elegido is not None:  # siempre cierto: candidatos no está vacío
                    causa = elegido.tipo
                    movimiento_id = elegido.movimiento_id
                    confianza = parametros.multiples_candidatos
                    evidencia.extend(
                        f"mov:{otro.movimiento_id}" for otro in candidatos if otro is not elegido
                    )
            elif not permitidas:
                # Concepto no atribuible por diseño: no hay orden posible que lo explique.
                confianza = (
                    parametros.causa_unica
                    if configuracion.causa_oficial(linea.concepto_id) is not None
                    else parametros.sin_candidato
                )
            else:
                confianza = parametros.sin_candidato

            if causa is not None:
                if movimiento_id is not None:
                    evidencia.append(f"mov:{movimiento_id}")
                evidencia.append(EVIDENCIA_REGLA)
            causa_oficial = configuracion.causa_oficial(linea.concepto_id, causa)

        dias = _dias_prorrateo(linea)
        # El recálculo solo tiene sentido cuando el importe de la línea ES la suma de sus
        # tramos, es decir, en la renta recurrente. Un ajuste retroactivo o un descuento
        # también llevan tramos, pero su importe es una *diferencia* entre prorrateos:
        # compararlo con la suma daría un falso descuadre y bajaría la confianza sin motivo.
        familia = linea.familia or configuracion.familia(linea.concepto_id)
        if (
            familia is FamiliaConcepto.RECURRENTE
            and configuracion.es_prorrateable(linea.concepto_id)
            and dias_ciclo
        ):
            esperado = _prorrateo_esperado(linea, dias_ciclo)
            if esperado is not None:
                desvio = abs(esperado - linea.monto_actual_cent)
                if desvio > parametros.tolerancia_prorrateo_cent:
                    confianza = min(confianza, parametros.tope_prorrateo_inconsistente)
                    evidencia.append(f"regla:prorrateo_inconsistente:{desvio}")

        datos.update(
            causa=causa,
            causa_oficial=causa_oficial,
            movimiento_id=movimiento_id,
            confianza=round(min(max(confianza, 0.0), 1.0), 4),
            dias_prorrateo=dias,
            evidencia=sorted(dict.fromkeys(evidencia)),
        )
        atribuidas.append(LineaDelta.model_validate(datos))

    return atribuidas


def esta_atribuida(linea: LineaDelta, confianza_minima: float = 0.0) -> bool:
    """Si la variación de la línea cuenta como **explicada**.

    Lo está cuando tiene una causa del CRM, una causa oficial del catálogo o es una
    línea derivada del propio recibo, y además alcanza la confianza mínima exigida
    (``confianza.minima_para_explicar`` de ``rules.yaml``).

    Es el predicado que alimenta ``s1`` (cobertura del delta explicado) del umbral de
    incomprensión, de modo que la decisión de derivar use la misma definición de
    "explicado" que la narración.
    """
    if linea.confianza < confianza_minima:
        return False
    return (
        linea.causa is not None
        or linea.causa_oficial is not None
        or EVIDENCIA_DERIVADO in linea.evidencia
    )
