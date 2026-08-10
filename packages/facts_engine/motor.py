"""Fachada del motor determinístico: de dos recibos a un ``FactSet`` sellado.

Este módulo orquesta las secciones 4.1 a 4.7 y es el **único** punto por el que el resto
del sistema construye hechos::

    fs = construir_factset(recibo_actual, recibos_previos, movimientos, reglas)

Lo que sale de aquí es la única fuente de cifras del proyecto: el LLM no calcula, el
verificador ancla contra ``fs.tokens_permitidos()`` y la auditoría guarda ``fs.sha256``.

**El motor no decide derivar.** Si el invariante no cierra, el FactSet se devuelve igual
con ``invariante.ok = False`` y todos sus datos: quien decide qué hacer con eso es la
capa superior (la API responde 409 ``INVARIANTE_FALLIDO`` y ``confianza.py`` marca la
regla dura). Separar el cálculo de la política es lo que permite auditar ambos.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

from packages.core_domain.dinero import repartir_mayor_resto
from packages.core_domain.enums import (
    CausaOficial,
    FamiliaConcepto,
    TipoMovimiento,
    etiqueta_causa_oficial,
)
from packages.core_domain.esquemas.factset import CausaAgregada, FactSet, LineaDelta
from packages.core_domain.esquemas.movimiento import (
    DetalleAltaEquipoFinanciado,
    MovementEvent,
    PlanFinanciamiento,
)
from packages.core_domain.esquemas.recibo import Recibo
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas
from packages.facts_engine.atribucion import atribuir, esta_atribuida
from packages.facts_engine.diff import comparar_detallado
from packages.facts_engine.invariante import verificar_conciliacion
from packages.facts_engine.prorrateo import cronograma_frances
from packages.facts_engine.tramos import DescuentoVigente, construir_tramos

__all__ = [
    "SinReciboPrevio",
    "agregar_causas",
    "confianza_global",
    "construir_factset",
    "movimientos_del_ciclo",
    "resumen_de_conciliacion",
    "seleccionar_recibo_previo",
]

_LOG = logging.getLogger(__name__)


class SinReciboPrevio(ValueError):
    """No hay ningún recibo anterior con el que comparar: no se puede explicar nada."""


# --------------------------------------------------------------------------- #
# Selección de entradas
# --------------------------------------------------------------------------- #
def seleccionar_recibo_previo(
    recibos_previos: Sequence[Recibo], periodo_actual: str, cuenta_id: str | None = None
) -> Recibo | None:
    """Devuelve el recibo inmediatamente anterior al periodo indicado.

    BrainyBill expone el recibo actual y los cinco previos; la comparación se hace
    siempre contra el **inmediatamente anterior**, que es el que el cliente recuerda.
    Los periodos ``YYYY-MM`` se ordenan como texto, que en ese formato es orden
    cronológico.
    """
    candidatos = [
        recibo
        for recibo in recibos_previos
        if recibo.periodo < periodo_actual
        and (cuenta_id is None or recibo.cuenta_id == cuenta_id)
    ]
    if not candidatos:
        return None
    return max(candidatos, key=lambda recibo: recibo.periodo)


def movimientos_del_ciclo(
    movimientos: Sequence[MovementEvent],
    cuenta_id: str,
    inicio: date,
    fin: date,
) -> list[MovementEvent]:
    """Movimientos de la cuenta ocurridos en ``[inicio, fin)``, ordenados por fecha.

    Es la "ventana" de la sección 4.7. Acotarla al ciclo es lo que evita que una orden
    de hace tres meses se cuele como explicación de la variación de este mes.
    """
    ventana = [
        movimiento
        for movimiento in movimientos
        if movimiento.cuenta_id == cuenta_id and inicio <= movimiento.fecha < fin
    ]
    ventana.sort(key=lambda movimiento: (movimiento.ocurrido_en, movimiento.movimiento_id))
    return ventana


# --------------------------------------------------------------------------- #
# Agregación de causas y confianza
# --------------------------------------------------------------------------- #
def agregar_causas(
    lineas: Sequence[LineaDelta],
    reglas: ConfiguracionReglas,
) -> list[CausaAgregada]:
    """Agrupa las variaciones por causa, en el vocabulario del cliente.

    La clave de agrupación es la **causa oficial de la ficha** (las nueve del enunciado)
    y no el ``TipoMovimiento`` del CRM: es lo que se narra y lo que evalúa el jurado.
    Las líneas derivadas del propio recibo (IGV, redondeo) se agrupan aparte, sin causa.

    ``participacion_bp`` reparte 10 000 puntos básicos (100 %) entre las causas por
    mayor resto sobre el impacto absoluto de cada una, de modo que las participaciones
    **suman exactamente 100 %** y ninguna se calcula con coma flotante.

    Los signos **no se compensan entre causas**: cada causa conserva su propio importe
    con signo. Un recibo puede subir S/ 20.82 con una causa de +S/ 49.90 y otra de
    −S/ 32.26, y agregarlas en un único "+17.64 por cambio de plan" sería aritmética
    correcta y explicación falsa. Mantenerlas separadas es lo que permite a la narrativa
    nombrar primero lo que subió y reconocer después lo que se ahorró.

    Returns:
        Las causas ordenadas por impacto absoluto descendente y, a igualdad, primero la
        que aumenta el recibo.
    """
    grupos: dict[tuple[CausaOficial | None, TipoMovimiento | None], dict] = {}
    for linea in lineas:
        if not linea.se_explica:
            continue
        clave = (linea.causa_oficial, linea.causa)
        grupo = grupos.setdefault(
            clave,
            {
                "monto": 0,
                "conceptos": [],
                "movimientos": [],
                "confianzas": [],
                "evidencia": [],
            },
        )
        grupo["monto"] += linea.delta_cent
        grupo["conceptos"].append(linea.concepto_id)
        if linea.movimiento_id is not None:
            grupo["movimientos"].append(linea.movimiento_id)
        grupo["confianzas"].append(linea.confianza)
        grupo["evidencia"].extend(linea.evidencia)

    if not grupos:
        return []

    claves = list(grupos)
    pesos = [abs(grupos[clave]["monto"]) for clave in claves]
    participaciones = (
        repartir_mayor_resto(10_000, pesos) if sum(pesos) > 0 else [0] * len(claves)
    )

    causas: list[CausaAgregada] = []
    for clave, participacion in zip(claves, participaciones, strict=True):
        causa_oficial, causa = clave
        datos = grupos[clave]
        confianzas = datos["confianzas"]
        etiqueta = etiqueta_causa_oficial(causa_oficial or causa)
        if causa_oficial is None and causa is None:
            # Sin causa (IGV, redondeo): se nombra el propio concepto, que sí significa
            # algo para el cliente, en lugar del genérico "otros cargos".
            unicos = sorted(set(datos["conceptos"]))
            concepto = reglas.concepto(unicos[0]) if len(unicos) == 1 else None
            if concepto is not None:
                etiqueta = concepto.nombre_comercial
        causas.append(
            CausaAgregada(
                causa=causa,
                causa_oficial=causa_oficial,
                etiqueta_cliente=etiqueta,
                monto_cent=datos["monto"],
                participacion_bp=participacion,
                conceptos=sorted(dict.fromkeys(datos["conceptos"])),
                movimientos=sorted(dict.fromkeys(datos["movimientos"])),
                confianza=round(min(confianzas), 4) if confianzas else 1.0,
                evidencia=sorted(dict.fromkeys(datos["evidencia"])),
            )
        )
    # Orden narrativo: impacto absoluto descendente y, a igualdad de impacto, primero la
    # que SUBE el recibo. Es lo primero que pregunta el cliente y lo primero que hay que
    # contestarle; el ahorro se reconoce después, nunca fundido con el aumento.
    causas.sort(
        key=lambda causa: (
            -abs(causa.monto_cent),
            0 if causa.monto_cent > 0 else 1,
            causa.etiqueta_cliente,
        )
    )
    return causas


def confianza_global(lineas: Sequence[LineaDelta]) -> float:
    """Confianza del FactSet: media de las confianzas ponderada por impacto absoluto.

    Ponderar por ``|delta|`` es lo correcto: que no sepamos explicar una línea de
    S/ 0.50 no puede tumbar la confianza de una explicación de S/ 45.00, y al revés
    tampoco. Sin líneas con variación, la confianza es 1 (no hay nada dudoso que decir).
    """
    explicables = [linea for linea in lineas if linea.se_explica]
    if not explicables:
        return 1.0
    peso_total = sum(abs(linea.delta_cent) for linea in explicables)
    if peso_total == 0:
        return round(min(linea.confianza for linea in explicables), 4)
    acumulado = sum(linea.confianza * abs(linea.delta_cent) for linea in explicables)
    return round(min(max(acumulado / peso_total, 0.0), 1.0), 4)


# --------------------------------------------------------------------------- #
# Enriquecimiento: reconstrucción de tramos
# --------------------------------------------------------------------------- #
def _tarifa_anterior(movimiento: MovementEvent) -> int | None:
    """Tarifa de lista previa declarada en un ``CAMBIO_PLAN``."""
    valor = movimiento.detalle.get("tarifa_anterior_cent")
    return int(valor) if valor is not None else None


#: Familias a las que se les puede reconstruir la tabla de tramos: la renta recurrente
#: (cuyo importe ES la suma de los tramos) y los ajustes ligados al plan (cuyo importe es
#: una diferencia entre prorrateos, pero que se explican con la misma tabla).
_FAMILIAS_CON_TRAMOS = (FamiliaConcepto.RECURRENTE, FamiliaConcepto.AJUSTE)


def _reconstruir_tramos(
    linea: LineaDelta,
    recibo: Recibo,
    movimientos: Sequence[MovementEvent],
    reglas: ConfiguracionReglas,
) -> LineaDelta:
    """Rellena ``tramos`` cuando el recibo no los trae pero el ciclo sí tiene cortes.

    Tres cautelas, porque una tabla de tramos inventada es una explicación inventada:

    1. Solo se reconstruye si la tarifa de partida es **conocida**: la declara el propio
       ``CAMBIO_PLAN`` en ``tarifa_anterior_cent``. Nunca se supone una tarifa.
    2. Solo para conceptos prorrateables de familia RECURRENTE o AJUSTE que admitan
       ``CAMBIO_PLAN`` como causa; un descuento o un cargo único no se explican con la
       línea de tiempo del plan.
    3. En la renta recurrente, la tabla **se adjunta únicamente si reproduce el importe
       facturado** (±tolerancia). Si no cuadra, la reconstrucción está equivocada —no el
       recibo— y se descarta: es el caso de la renta ADELANTADA, donde la línea es la
       renta anticipada del ciclo siguiente y no la suma de los tramos del ciclo en curso.
       Ahí la tabla la lleva el ajuste retroactivo, que es lo que de verdad explica.

    Si no se puede reconstruir con garantías, la línea vuelve intacta y se explicará sin
    tabla de tramos.
    """
    familia = linea.familia or reglas.familia(linea.concepto_id)
    if (
        linea.tramos
        or familia not in _FAMILIAS_CON_TRAMOS
        or not reglas.es_prorrateable(linea.concepto_id)
        or not reglas.permite_causa(linea.concepto_id, TipoMovimiento.CAMBIO_PLAN)
    ):
        return linea
    cambios = [
        movimiento
        for movimiento in movimientos
        if movimiento.tipo is TipoMovimiento.CAMBIO_PLAN
        and _tarifa_anterior(movimiento) is not None
    ]
    if not cambios:
        return linea
    dias_ciclo = reglas.dias_ciclo_efectivos(recibo.dias_ciclo)
    descuentos: list[DescuentoVigente] = []
    tramos = construir_tramos(
        recibo.ciclo_inicio,
        recibo.ciclo_fin,
        movimientos,
        _tarifa_anterior(cambios[0]) or 0,
        descuentos,
        dias_ciclo=dias_ciclo,
        concepto_id=linea.concepto_id,
        plan_inicial=str(cambios[0].detalle.get("plan_anterior") or "") or None,
        cobrar_en_suspension=reglas.politica.cobro_en_suspension,
        convencion=reglas.politica.convencion_prorrateo,
        dias_base_30_360=reglas.politica.dias_base_30_360,
    )
    if familia is FamiliaConcepto.RECURRENTE:
        reconstruido = sum(tramo.monto_prorrateado_cent for tramo in tramos)
        desvio = abs(reconstruido - linea.monto_actual_cent)
        if desvio > reglas.politica.tolerancia_residual_cent:
            _LOG.debug(
                "no se adjunta la reconstrucción de tramos de %s: difiere en %s céntimos",
                linea.concepto_id,
                desvio,
            )
            return linea
    datos = linea.model_dump()
    datos["tramos"] = [tramo.model_dump() for tramo in tramos]
    return LineaDelta.model_validate(datos)


def _financiamientos_desde_movimientos(
    movimientos: Sequence[MovementEvent],
) -> list[PlanFinanciamiento]:
    """Cronogramas franceses de los equipos financiados presentes en el historial."""
    planes: list[PlanFinanciamiento] = []
    for movimiento in movimientos:
        if movimiento.tipo is not TipoMovimiento.ALTA_EQUIPO_FINANCIADO:
            continue
        try:
            detalle = DetalleAltaEquipoFinanciado.model_validate(movimiento.detalle)
            planes.append(
                cronograma_frances(
                    detalle.equipo,
                    detalle.principal_cent,
                    detalle.tasa_mensual_bp,
                    detalle.cuotas_totales,
                    movimiento_id=movimiento.movimiento_id,
                )
            )
        except (ValueError, TypeError) as error:  # datos incompletos: se explica sin cronograma
            _LOG.warning(
                "no se pudo reconstruir el financiamiento del movimiento %s: %s",
                movimiento.movimiento_id,
                error,
            )
    return planes


# --------------------------------------------------------------------------- #
# Fachada
# --------------------------------------------------------------------------- #
def construir_factset(
    recibo_actual: Recibo,
    recibos_previos: Sequence[Recibo],
    movimientos: Sequence[MovementEvent] = (),
    reglas: ConfiguracionReglas | None = None,
    *,
    ventana_movimientos: tuple[date, date] | None = None,
    financiamientos: Sequence[PlanFinanciamiento] | None = None,
    beneficios_vigentes: Sequence[str] | None = None,
    reconstruir_tramos: bool = True,
) -> FactSet:
    """Construye el ``FactSet`` completo, conciliado y sellado.

    Orquestación (secciones 4.6, 4.7 y 4.1):

    1. Se elige el recibo **inmediatamente anterior** de los cinco previos.
    2. ``diff.comparar_detallado`` hace el FULL OUTER JOIN por concepto.
    3. Se reconstruyen los tramos de las líneas prorrateables cuando el recibo no los
       trae y el ciclo tiene cortes conocidos.
    4. ``atribucion.atribuir`` asigna causa y confianza con la ventana del ciclo.
    5. ``invariante.verificar_conciliacion`` comprueba que
       ``Σ deltas == total_actual − total_previo`` (±1 céntimo).
    6. Se agregan las causas en el vocabulario de la ficha y se calcula la confianza
       global ponderada.
    7. Se sella con SHA-256 sobre el JSON canónico.

    Si el invariante falla **no se lanza excepción**: el FactSet vuelve con
    ``invariante.ok = False`` para que la capa superior derive con contexto completo.
    Un error aquí dejaría al asesor sin los datos que necesita.

    Args:
        recibo_actual: recibo del periodo que se explica.
        recibos_previos: hasta cinco recibos anteriores (BrainyBill).
        movimientos: historial de órdenes de la cuenta (Amdocs).
        reglas: configuración de negocio; por defecto ``cargar_reglas()``.
        ventana_movimientos: ``(inicio, fin)`` alternativo para la atribución. Por
            defecto, el ciclo del recibo actual.
        financiamientos: cronogramas ya calculados. Si se omiten, se reconstruyen de
            los movimientos ``ALTA_EQUIPO_FINANCIADO``.
        beneficios_vigentes: beneficios que el cliente **ya tiene** (efecto
            efervescente). Por defecto se leen de ``recibo_actual.meta``.
        reconstruir_tramos: permite desactivar el paso 3.

    Returns:
        El ``FactSet`` sellado (``sha256`` calculado y verificable).

    Raises:
        SinReciboPrevio: si no hay ningún recibo anterior con el que comparar.
        ValueError: si el recibo previo pertenece a otra cuenta.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    previo = seleccionar_recibo_previo(
        recibos_previos, recibo_actual.periodo, recibo_actual.cuenta_id
    )
    if previo is None:
        raise SinReciboPrevio(
            f"no hay recibo anterior a {recibo_actual.periodo} para la cuenta "
            f"{recibo_actual.cuenta_id}: no se puede explicar una variación"
        )
    if previo.cuenta_id != recibo_actual.cuenta_id:
        raise ValueError("el recibo previo pertenece a otra cuenta")

    inicio, fin = ventana_movimientos or (recibo_actual.ciclo_inicio, recibo_actual.ciclo_fin)
    ventana = movimientos_del_ciclo(movimientos, recibo_actual.cuenta_id, inicio, fin)

    resumen = comparar_detallado(recibo_actual.lineas, previo.lineas, reglas=configuracion)
    lineas = resumen.lineas
    if reconstruir_tramos:
        lineas = [
            _reconstruir_tramos(linea, recibo_actual, ventana, configuracion) for linea in lineas
        ]
    lineas = atribuir(
        lineas,
        ventana,
        configuracion,
        dias_ciclo=configuracion.dias_ciclo_efectivos(recibo_actual.dias_ciclo),
    )

    invariante = verificar_conciliacion(
        recibo_actual.total_cent,
        previo.total_cent,
        lineas,
        tolerancia_cent=configuracion.politica.tolerancia_residual_cent,
    )
    if not invariante.ok:
        _LOG.warning(
            "invariante roto para %s %s: residual de %s céntimos",
            recibo_actual.cuenta_id,
            recibo_actual.periodo,
            invariante.residual_cent,
        )

    causas = agregar_causas(lineas, configuracion)
    planes = (
        list(financiamientos)
        if financiamientos is not None
        else _financiamientos_desde_movimientos(movimientos)
    )
    beneficios = (
        list(beneficios_vigentes)
        if beneficios_vigentes is not None
        else [str(item) for item in recibo_actual.meta.get("beneficios_vigentes", [])]
    )

    factset = FactSet(
        factset_id=FactSet.id_determinista(
            recibo_actual.cuenta_id, recibo_actual.periodo, configuracion.rules_version
        ),
        cuenta_id=recibo_actual.cuenta_id,
        modalidad_renta=recibo_actual.modalidad_renta,
        periodo_actual=recibo_actual.periodo,
        periodo_previo=previo.periodo,
        dias_ciclo=recibo_actual.dias_ciclo,
        total_actual_cent=recibo_actual.total_cent,
        total_previo_cent=previo.total_cent,
        delta_total_cent=recibo_actual.total_cent - previo.total_cent,
        lineas=lineas,
        causas_agregadas=causas,
        invariante=invariante,
        deuda_anterior_cent=recibo_actual.deuda_anterior_cent,
        confianza_global=confianza_global(lineas),
        rules_version=configuracion.rules_version,
        ciclo_inicio=recibo_actual.ciclo_inicio,
        ciclo_fin=recibo_actual.ciclo_fin,
        fecha_vencimiento=recibo_actual.fecha_vencimiento,
        estado_servicio=recibo_actual.estado_servicio,
        plan_vigente=recibo_actual.plan_vigente,
        financiamientos=planes,
        beneficios_vigentes=beneficios,
        movimientos_ciclo=[movimiento.movimiento_id for movimiento in ventana],
    )
    return factset.sellar()


def resumen_de_conciliacion(factset: FactSet) -> dict[str, object]:
    """Proyección para el evento ``FACTS_BUILT`` de la auditoría.

    Incluye ``residual_cent`` (obligatorio según la sección 7) y las cifras mínimas para
    reconstruir la decisión sin volver a calcular nada.
    """
    return {
        "factset_id": str(factset.factset_id),
        "sha256": factset.sha256,
        "rules_version": factset.rules_version,
        "modalidad_renta": str(factset.modalidad_renta),
        "delta_total_cent": factset.delta_total_cent,
        "residual_cent": factset.invariante.residual_cent,
        "invariante_ok": factset.invariante.ok,
        "lineas_con_variacion": len(factset.lineas),
        "lineas_atribuidas": sum(1 for linea in factset.lineas if esta_atribuida(linea)),
        "causas": [causa.etiqueta_cliente for causa in factset.causas_agregadas],
        "confianza_global": factset.confianza_global,
        "firma_causal": factset.firma_causal(),
    }
