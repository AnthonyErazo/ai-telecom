"""Modelo de tramos (sección 4.1): **un solo algoritmo cubre los cinco escenarios**.

El ciclo ``[t0, t1)`` se parte por todos los eventos del historial de órdenes en tramos
disjuntos que suman exactamente ``D`` días. Cada tramo lleva la tarifa mensual vigente
(con el descuento ya aplicado) y el estado del servicio, y de ahí sale el prorrateo.

**La tabla de tramos ES la explicación**: "del 1 al 12 de julio el Plan Max, del 13 al 30
el Plan Ligero". No hay una fórmula por escenario — cambio de plan, alta, baja,
suspensión, reconexión y fin de descuento son todos el mismo corte en la recta del ciclo.

Convención de intervalos: ``[inicio, fin)`` con ``fin`` **exclusivo**, igual que en
``Tramo`` y ``Recibo.ciclo_fin``. Así los tramos encadenan sin solaparse y
``Σ dias == dias_ciclo`` es comprobable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import Centimos, prorratear
from packages.core_domain.enums import ConvencionProrrateo, EstadoServicio, TipoMovimiento
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.esquemas.recibo import Tramo, etiqueta_rango_fechas
from packages.facts_engine.prorrateo import denominador_ciclo, dias_para_prorrateo

__all__ = [
    "EVENTOS_QUE_CORTAN_TRAMO",
    "DescuentoVigente",
    "construir_tramos",
    "describir_tramos",
    "dias_facturables",
    "dias_suspendidos",
    "validar_particion",
]

#: Movimientos que cambian la tarifa o el estado del servicio y, por tanto, cortan el
#: ciclo. El resto (paquetes, notas, alta de financiamiento) son cargos puntuales: no
#: alteran la renta devengada y no parten la recta del ciclo.
EVENTOS_QUE_CORTAN_TRAMO: frozenset[TipoMovimiento] = frozenset(
    {
        TipoMovimiento.CAMBIO_PLAN,
        TipoMovimiento.SUSPENSION,
        TipoMovimiento.RECONEXION,
        TipoMovimiento.ALTA_SERVICIO,
        TipoMovimiento.BAJA_SERVICIO,
        TipoMovimiento.FIN_DESCUENTO,
    }
)

#: Claves donde los distintos orígenes (Amdocs, generador sintético) traen la tarifa.
_CLAVES_TARIFA = ("tarifa_mensual_cent", "tarifa_nueva_cent", "tarifa_cent", "monto_cent")


class DescuentoVigente(BaseModel):
    """Descuento recurrente con vigencia, que resta de la tarifa mensual de lista.

    ``[desde, hasta)``: ambos extremos opcionales; ``hasta`` es **exclusivo**, de modo
    que un ``FIN_DESCUENTO`` del día 15 significa que el descuento aplicó hasta el 14.
    """

    model_config = ConfigDict(extra="forbid")

    descuento_id: str
    nombre: str = ""
    monto_cent: Centimos = Field(description="Importe mensual que resta, en positivo")
    desde: date | None = None
    hasta: date | None = Field(default=None, description="Exclusivo")
    concepto_id: str | None = None

    def vigente_en(self, dia: date) -> bool:
        """Si el descuento aplica el día indicado."""
        if self.desde is not None and dia < self.desde:
            return False
        return not (self.hasta is not None and dia >= self.hasta)


def _clave_orden(movimiento: MovementEvent) -> tuple[object, int]:
    """Orden estable de los movimientos: por instante y, en empate, por identificador."""
    return (movimiento.ocurrido_en, movimiento.movimiento_id)


def _servicio_del_movimiento(movimiento: MovementEvent) -> str | None:
    """``servicio_id`` del evento, mirando también dentro del detalle."""
    if movimiento.servicio_id:
        return movimiento.servicio_id
    valor = movimiento.detalle.get("servicio_id")
    return str(valor) if valor else None


def _entero(detalle: dict[str, object], claves: Sequence[str]) -> int | None:
    """Primer valor entero presente en el detalle entre las claves dadas."""
    for clave in claves:
        if clave in detalle and detalle[clave] is not None:
            return int(detalle[clave])  # type: ignore[arg-type]
    return None


def _normalizar_descuentos(
    descuentos: Sequence[DescuentoVigente] | Centimos | None,
) -> list[DescuentoVigente]:
    """Admite una lista de descuentos, un importe plano para todo el ciclo, o nada."""
    if descuentos is None:
        return []
    if isinstance(descuentos, int):
        if descuentos == 0:
            return []
        return [DescuentoVigente(descuento_id="DESCUENTO_VIGENTE", monto_cent=descuentos)]
    return [descuento.model_copy(deep=True) for descuento in descuentos]


def _cerrar_descuentos_por_evento(
    descuentos: list[DescuentoVigente], eventos_fin: Iterable[MovementEvent]
) -> list[DescuentoVigente]:
    """Aplica los ``FIN_DESCUENTO`` del ciclo poniendo ``hasta`` en la fecha del evento.

    El emparejamiento va de lo más específico a lo más general: por ``promocion_id``,
    por importe idéntico y, si no hay nada declarado, se sintetiza un descuento que
    estuvo vigente hasta esa fecha (es la lectura correcta de "la promoción terminó":
    antes del evento el cliente sí lo tenía).
    """
    resultado = list(descuentos)
    for evento in eventos_fin:
        detalle = evento.detalle
        promocion_id = detalle.get("promocion_id")
        monto = _entero(detalle, ("descuento_cent", "monto_cent")) or 0
        objetivo: DescuentoVigente | None = None
        abiertos = [d for d in resultado if d.hasta is None]
        if promocion_id is not None:
            objetivo = next((d for d in abiertos if d.descuento_id == str(promocion_id)), None)
        if objetivo is None and monto:
            objetivo = next((d for d in abiertos if d.monto_cent == monto), None)
        if objetivo is None and promocion_id is None and abiertos:
            objetivo = abiertos[0]
        if objetivo is not None:
            objetivo.hasta = evento.fecha
        elif monto > 0:
            resultado.append(
                DescuentoVigente(
                    descuento_id=str(promocion_id or f"FIN_DESCUENTO_{evento.movimiento_id}"),
                    nombre=str(detalle.get("nombre", "")),
                    monto_cent=monto,
                    hasta=evento.fecha,
                )
            )
    return resultado


def _descuento_en(descuentos: Sequence[DescuentoVigente], dia: date) -> Centimos:
    """Suma de los descuentos vigentes un día concreto."""
    return sum(d.monto_cent for d in descuentos if d.vigente_en(dia))


def _mismo_perfil(izquierda: Tramo, derecha: Tramo) -> bool:
    """Dos tramos contiguos son fusionables si nada de lo que se explica cambió."""
    return (
        izquierda.tarifa_mensual_cent == derecha.tarifa_mensual_cent
        and izquierda.estado == derecha.estado
        and izquierda.facturable == derecha.facturable
        and izquierda.plan == derecha.plan
        and izquierda.descuento_cent == derecha.descuento_cent
        and izquierda.concepto_id == derecha.concepto_id
    )


def construir_tramos(
    ciclo_inicio: date,
    ciclo_fin: date,
    movimientos: Sequence[MovementEvent],
    tarifa_base_cent: Centimos,
    descuentos: Sequence[DescuentoVigente] | Centimos | None = None,
    *,
    dias_ciclo: int | None = None,
    estado_inicial: EstadoServicio = EstadoServicio.ACTIVO,
    plan_inicial: str | None = None,
    concepto_id: str | None = None,
    servicio_id: str | None = None,
    cobrar_en_suspension: bool = False,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
    fusionar: bool = True,
) -> list[Tramo]:
    """Parte el ciclo ``[ciclo_inicio, ciclo_fin)`` en tramos disjuntos (fórmula 4.1).

    Recorre la recta del ciclo aplicando los eventos en orden cronológico y emite un
    tramo por cada intervalo homogéneo::

        Ciclo [t0, t1), D = (t1 − t0).days
        tramos j = [a_j, b_j),  len_j = b_j − a_j,  Σ len_j = D
          P_j = tarifa mensual vigente (lista − descuentos vigentes, nunca negativa)
          e_j ∈ {ACTIVO, SUSPENDIDO}
          facturable(e_j) = False si SUSPENDIDO y cobro_en_suspension == False
          monto_j = P_j · len_j / D  si facturable, 0 si no

    Efecto de cada evento sobre el estado del recorrido:

    * ``CAMBIO_PLAN`` -> nueva tarifa de lista y nuevo nombre de plan.
    * ``ALTA_SERVICIO`` -> tarifa del detalle (o la base) y servicio activo.
    * ``BAJA_SERVICIO`` -> tarifa 0 a partir de la fecha.
    * ``SUSPENSION`` / ``RECONEXION`` -> cambian el estado del servicio.
    * ``FIN_DESCUENTO`` -> cierra la vigencia del descuento correspondiente.

    Los eventos anteriores o iguales a ``ciclo_inicio`` se aplican al estado inicial (ya
    estaban en vigor); los posteriores a ``ciclo_fin`` se ignoran. Los tramos contiguos
    con el mismo perfil se fusionan (``fusionar=True``) para que la explicación no
    muestre cortes invisibles para el cliente: un paquete comprado a mitad de mes no
    parte la renta en dos.

    Args:
        ciclo_inicio: primer día del ciclo.
        ciclo_fin: día siguiente al último del ciclo (**exclusivo**).
        movimientos: eventos del historial de órdenes; se filtran por tipo y servicio.
        tarifa_base_cent: tarifa mensual de lista vigente al empezar el ciclo.
        descuentos: descuentos con vigencia, o un importe plano para todo el ciclo.
        dias_ciclo: denominador ``D`` del prorrateo. Por defecto, el que impone la
            convención (días reales o 30).
        cobrar_en_suspension: ``politica.cobro_en_suspension`` de ``rules.yaml``.

    Returns:
        Los tramos en orden cronológico. Siempre cumplen ``Σ dias == (fin − inicio).days``
        y ``tramo[i].fin == tramo[i+1].inicio``.

    Raises:
        ValueError: si el ciclo está vacío o invertido, o si la partición no cierra.
    """
    dias_reales = (ciclo_fin - ciclo_inicio).days
    if dias_reales <= 0:
        raise ValueError(f"ciclo vacío o invertido: [{ciclo_inicio}, {ciclo_fin})")
    denominador = (
        dias_ciclo
        if dias_ciclo is not None
        else denominador_ciclo(dias_reales, convencion, dias_base_30_360)
    )
    if denominador <= 0:
        raise ValueError(f"denominador de prorrateo inválido: {denominador}")

    relevantes = [
        movimiento
        for movimiento in movimientos
        if movimiento.tipo in EVENTOS_QUE_CORTAN_TRAMO
        and movimiento.fecha < ciclo_fin
        and (servicio_id is None or _servicio_del_movimiento(movimiento) in (None, servicio_id))
    ]
    relevantes.sort(key=_clave_orden)

    vigencias = _cerrar_descuentos_por_evento(
        _normalizar_descuentos(descuentos),
        [m for m in relevantes if m.tipo is TipoMovimiento.FIN_DESCUENTO],
    )

    # Fronteras: los extremos del ciclo, cada evento interior y cada cambio de vigencia
    # de un descuento. Un evento en el primer día no corta: ya define el estado inicial.
    fronteras: set[date] = {ciclo_inicio, ciclo_fin}
    for movimiento in relevantes:
        if ciclo_inicio < movimiento.fecha < ciclo_fin:
            fronteras.add(movimiento.fecha)
    for vigencia in vigencias:
        for limite in (vigencia.desde, vigencia.hasta):
            if limite is not None and ciclo_inicio < limite < ciclo_fin:
                fronteras.add(limite)
    cortes = sorted(fronteras)

    tarifa_lista = tarifa_base_cent
    estado = estado_inicial
    plan = plan_inicial
    pendientes = list(relevantes)

    tramos: list[Tramo] = []
    for indice, inicio in enumerate(cortes[:-1]):
        fin = cortes[indice + 1]
        # Se aplican todos los eventos con fecha <= inicio (los del primer tramo incluyen
        # los anteriores al ciclo, que ya estaban en vigor).
        while pendientes and pendientes[0].fecha <= inicio:
            evento = pendientes.pop(0)
            tarifa_lista, estado, plan = _aplicar_evento(evento, tarifa_lista, estado, plan)

        descuento = _descuento_en(vigencias, inicio)
        tarifa_efectiva = max(tarifa_lista - descuento, 0)
        suspendido = estado is EstadoServicio.SUSPENDIDO
        facturable = (not suspendido) or cobrar_en_suspension
        dias_numerador = dias_para_prorrateo(inicio, fin, convencion, dias_base_30_360)
        monto = prorratear(tarifa_efectiva, dias_numerador, denominador) if facturable else 0
        tramos.append(
            Tramo(
                inicio=inicio,
                fin=fin,
                dias=(fin - inicio).days,
                tarifa_mensual_cent=tarifa_efectiva,
                estado=estado,
                facturable=facturable,
                monto_prorrateado_cent=monto,
                etiqueta=etiqueta_rango_fechas(inicio, fin),
                concepto_id=concepto_id,
                plan=plan,
                descuento_cent=descuento,
            )
        )

    if fusionar:
        tramos = _fusionar_tramos(
            tramos,
            denominador,
            convencion=convencion,
            dias_base_30_360=dias_base_30_360,
        )
    validar_particion(tramos, ciclo_inicio, ciclo_fin)
    return tramos


def _aplicar_evento(
    evento: MovementEvent,
    tarifa_lista: Centimos,
    estado: EstadoServicio,
    plan: str | None,
) -> tuple[Centimos, EstadoServicio, str | None]:
    """Nuevo estado del recorrido tras aplicar un evento. Función pura."""
    detalle = evento.detalle
    if evento.tipo is TipoMovimiento.CAMBIO_PLAN:
        nueva = _entero(detalle, ("tarifa_nueva_cent", "tarifa_mensual_cent"))
        if nueva is not None:
            tarifa_lista = nueva
        nombre = detalle.get("plan_nuevo")
        if nombre:
            plan = str(nombre)
    elif evento.tipo is TipoMovimiento.ALTA_SERVICIO:
        nueva = _entero(detalle, _CLAVES_TARIFA)
        if nueva is not None:
            tarifa_lista = nueva
        nombre = detalle.get("plan") or detalle.get("plan_nuevo")
        if nombre:
            plan = str(nombre)
    elif evento.tipo is TipoMovimiento.BAJA_SERVICIO:
        tarifa_lista = 0
    elif evento.tipo is TipoMovimiento.SUSPENSION:
        estado = EstadoServicio.SUSPENDIDO
    elif evento.tipo is TipoMovimiento.RECONEXION:
        estado = EstadoServicio.ACTIVO
    return tarifa_lista, estado, plan


def _fusionar_tramos(
    tramos: Sequence[Tramo],
    dias_ciclo: int,
    *,
    convencion: ConvencionProrrateo = ConvencionProrrateo.ACTUAL,
    dias_base_30_360: int = 30,
) -> list[Tramo]:
    """Une tramos contiguos con el mismo perfil y recalcula su importe y su etiqueta.

    El importe se recalcula sobre los días fusionados (un solo redondeo) en vez de
    sumar los parciales: así el número que ve el cliente es el que corresponde a la
    fila que lee.
    """
    fusionados: list[Tramo] = []
    for tramo in tramos:
        anterior = fusionados[-1] if fusionados else None
        if anterior is not None and anterior.fin == tramo.inicio and _mismo_perfil(anterior, tramo):
            previo = anterior
            inicio, fin = previo.inicio, tramo.fin
            dias_numerador = dias_para_prorrateo(inicio, fin, convencion, dias_base_30_360)
            monto = (
                prorratear(previo.tarifa_mensual_cent, dias_numerador, dias_ciclo)
                if previo.facturable
                else 0
            )
            fusionados[-1] = previo.model_copy(
                update={
                    "fin": fin,
                    "dias": (fin - inicio).days,
                    "monto_prorrateado_cent": monto,
                    "etiqueta": etiqueta_rango_fechas(inicio, fin),
                }
            )
        else:
            fusionados.append(tramo)
    return fusionados


def validar_particion(tramos: Sequence[Tramo], ciclo_inicio: date, ciclo_fin: date) -> None:
    """Comprueba que los tramos cubren el ciclo exactamente, sin huecos ni solapes.

    Raises:
        ValueError: si los tramos no encadenan o no suman los días del ciclo.
    """
    esperados = (ciclo_fin - ciclo_inicio).days
    if not tramos:
        raise ValueError("la partición del ciclo no puede quedar vacía")
    if tramos[0].inicio != ciclo_inicio or tramos[-1].fin != ciclo_fin:
        raise ValueError(
            f"los tramos no cubren el ciclo [{ciclo_inicio}, {ciclo_fin}): "
            f"van de {tramos[0].inicio} a {tramos[-1].fin}"
        )
    for anterior, siguiente in zip(tramos, tramos[1:], strict=False):
        if anterior.fin != siguiente.inicio:
            raise ValueError(
                f"hueco o solape entre tramos: {anterior.fin} != {siguiente.inicio}"
            )
    suma = sum(tramo.dias for tramo in tramos)
    if suma != esperados:
        raise ValueError(f"los tramos suman {suma} días y el ciclo tiene {esperados}")


def dias_facturables(tramos: Sequence[Tramo]) -> int:
    """Días del ciclo que se cobran (los de tramos facturables)."""
    return sum(tramo.dias for tramo in tramos if tramo.facturable)


def dias_suspendidos(tramos: Sequence[Tramo]) -> int:
    """Días del ciclo con el servicio suspendido, se cobren o no."""
    return sum(
        tramo.dias for tramo in tramos if tramo.estado is EstadoServicio.SUSPENDIDO
    )


def describir_tramos(tramos: Sequence[Tramo]) -> str:
    """Frase en español de Perú que describe la partición del ciclo.

    ``"del 1 al 12 de julio el Plan Max; del 13 al 30 de julio el Plan Ligero"``.
    Es el texto que sustenta la explicación del prorrateo, ya en lenguaje de cliente.
    """
    partes: list[str] = []
    for tramo in tramos:
        detalle = tramo.plan or tramo.concepto_id or "su plan"
        if tramo.estado is EstadoServicio.SUSPENDIDO:
            detalle = f"{detalle} (servicio suspendido)"
        partes.append(f"{tramo.etiqueta} {detalle}")
    return "; ".join(partes)
