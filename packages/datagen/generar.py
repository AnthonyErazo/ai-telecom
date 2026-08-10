"""Generador del dataset sintético (sección 8 de la especificación).

Uso::

    python -m packages.datagen.generar --seed 20260804 --clientes 300 --salida data/sintetico/

Qué produce, por cliente: un recibo actual y **cinco previos** (lo que expone
BrainyBill), el historial de órdenes que los explica (lo que expone Amdocs) y el
**ground truth** de por qué varió cada concepto entre el recibo actual y el anterior.

Tres decisiones que sostienen todo lo demás:

1. **Reproducibilidad aislada.** La semilla de cada cliente sale de
   ``sha256(f"{seed}|{cuenta_id}")``. Regenerar con otro número de clientes, o generar
   un solo cliente, produce exactamente los mismos datos para ese cliente. Sin esto no
   se puede depurar un caso concreto ni fijar un golden.

2. **El recibo base no tiene ruido.** Los cinco periodos previos son idénticos en
   importe. Cualquier diferencia entre el recibo actual y el anterior procede de un
   escenario inyectado, cuya aritmética conocemos. El ground truth es exacto, no
   estimado.

3. **El generador aborta si el ground truth no cuadra.** No se comprueba solo la suma:
   se comprueba **concepto por concepto** que la fila declarada por el escenario
   coincide con el delta real del recibo, y luego que la suma de todas las filas es
   igual a ``total_actual − total_previo``. Un dataset cuyo ground truth miente es peor
   que no tener dataset: haría pasar la evaluación a un sistema roto.

El IGV se recalcula al final sobre la base afecta y su residuo de redondeo se reparte
por mayor resto entre las líneas afectas, de modo que la suma de líneas es siempre
exactamente el total del recibo.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from random import Random
from typing import Any

from packages.core_domain.dinero import (
    Centimos,
    aplicar_porcentaje,
    formatear_soles,
    redondear_banca,
    repartir_mayor_resto,
)
from packages.core_domain.enums import ModalidadRenta, TipoMovimiento
from packages.core_domain.esquemas.evaluacion import GroundTruthCausaDelta
from packages.core_domain.esquemas.movimiento import DetalleAltaEquipoFinanciado, MovementEvent
from packages.core_domain.esquemas.recibo import LineaRecibo, Recibo
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas, raiz_proyecto
from packages.datagen import catalogo_seed, faq_seed
from packages.datagen.escenarios import (
    CANALES_CLIENTE,
    CONCEPTO_DEUDA_ANTERIOR,
    CONSUMOS_POSIBLES,
    DEPARTAMENTOS,
    EDADES_RANGO,
    EQUIPOS,
    NOMBRES_ESCENARIOS,
    PLANES_HOGAR,
    PLANES_MOVIL,
    PLANES_TOTAL,
    PLANES_TV,
    PROMOCIONES,
    TIPOS_CLIENTE,
    CicloFacturacion,
    DescuentoBase,
    Escenario,
    FinanciamientoBase,
    PerfilCliente,
    ResultadoEscenario,
    ServicioBase,
    construir_ciclo,
    construir_linea,
    desplazar_periodo,
    inicio_de_ciclo,
    lineas_base,
    obtener_escenario,
    orden_de_concepto,
    son_compatibles,
)
from packages.datagen.mapping.movistar_map import COLUMNAS_ORDENES, fila_orden_desde_movimiento

__all__ = [
    "CUENTAS_DEMO",
    "PERIODOS_HISTORIAL",
    "PERIODO_ACTUAL_POR_DEFECTO",
    "PROPORCION_COMPUESTOS",
    "SEED_POR_DEFECTO",
    "ErrorConciliacion",
    "HistorialCliente",
    "ResumenGeneracion",
    "conciliar_ground_truth",
    "construir_argumentos",
    "generar_cliente",
    "generar_dataset",
    "main",
    "recibo_a_documento",
    "semilla_cliente",
]

#: Semilla por defecto de la demo (``DEMO_SEED`` del ``.env.example``).
SEED_POR_DEFECTO = 20260804

#: Último periodo facturado a la fecha del proyecto.
PERIODO_ACTUAL_POR_DEFECTO = "2026-07"

#: Actual + cinco previos, exactamente lo que expone BrainyBill.
PERIODOS_HISTORIAL = 6

#: Tres de cada diez clientes reciben dos escenarios simultáneos (sección 8).
PROPORCION_COMPUESTOS = 3

#: Los tres clientes de guion de la demo en vivo.
CUENTAS_DEMO: tuple[str, ...] = ("C-DEMO-01", "C-DEMO-02", "C-DEMO-03")

_SEGMENTOS: tuple[str, ...] = ("MASIVO", "PREMIUM", "HOGAR", "CONVERGENTE")

#: Beneficios que el cliente **ya tiene** (efecto efervescente). Sin cifras, a propósito.
_BENEFICIOS: tuple[str, ...] = (
    "llamadas ilimitadas a todo destino nacional",
    "redes sociales que no consumen sus datos",
    "acceso a Movistar TV App incluido",
    "roaming incluido en la Comunidad Andina",
    "atención preferente en tiendas",
    "instalación sin costo de sus equipos",
)


class ErrorConciliacion(RuntimeError):
    """El ground truth no reproduce la variación real del recibo.

    Es un error fatal del generador: no se escribe ningún fichero. Antes que publicar un
    dataset cuyo ground truth miente, el generador se detiene.
    """


# --------------------------------------------------------------------------- #
# Semillas
# --------------------------------------------------------------------------- #
def semilla_cliente(seed: int, cuenta_id: str) -> int:
    """Semilla determinista y **aislada** de un cliente.

    ``int(sha256(f"{seed}|{cuenta_id}").hexdigest()[:8], 16)``: dos clientes distintos
    no comparten flujo aleatorio, así que generar solo uno da el mismo resultado que
    generarlo dentro de una tanda de trescientos.
    """
    digest = hashlib.sha256(f"{seed}|{cuenta_id}".encode()).hexdigest()
    return int(digest[:8], 16)


def _base_movimiento_id(cuenta_id: str) -> int:
    """Rango de identificadores de orden reservado a una cuenta.

    Se deriva del ``cuenta_id`` para no depender del número de clientes generados. La
    unicidad global se verifica al final de la generación y aborta si falla.
    """
    digest = hashlib.sha256(f"orden|{cuenta_id}".encode()).hexdigest()
    return (1_000_000 + int(digest[:6], 16) % 8_000_000) * 10


# --------------------------------------------------------------------------- #
# Perfiles
# --------------------------------------------------------------------------- #
def _servicios_de_segmento(cuenta_id: str, segmento: str, rng: Random) -> list[ServicioBase]:
    """Servicios contratados según el segmento comercial del cliente."""
    if segmento == "CONVERGENTE":
        plan, tarifa = rng.choice(PLANES_TOTAL)
        return [
            ServicioBase(
                concepto_id="RENTA_MOVISTAR_TOTAL",
                nombre_comercial="Movistar Total",
                plan=plan,
                tarifa_cent=tarifa,
                servicio_id=f"{cuenta_id}-MT",
            )
        ]
    if segmento == "HOGAR":
        plan_hogar, tarifa_hogar = rng.choice(PLANES_HOGAR)
        servicios = [
            ServicioBase(
                concepto_id="RENTA_HOGAR_INTERNET",
                nombre_comercial="Internet hogar",
                plan=plan_hogar,
                tarifa_cent=tarifa_hogar,
                servicio_id=f"{cuenta_id}-BAF",
            )
        ]
        if rng.random() < 0.6:
            plan_tv, tarifa_tv = rng.choice(PLANES_TV)
            servicios.append(
                ServicioBase(
                    concepto_id="RENTA_TV",
                    nombre_comercial="Televisión",
                    plan=plan_tv,
                    tarifa_cent=tarifa_tv,
                    servicio_id=f"{cuenta_id}-TV",
                )
            )
        return servicios
    # PREMIUM se queda con los dos escalones altos del catálogo (Max y Ilimitado); el
    # resto puede tener cualquiera de los cuatro planes móviles.
    planes = PLANES_MOVIL[2:] if segmento == "PREMIUM" else PLANES_MOVIL
    plan, tarifa = rng.choice(planes)
    return [
        ServicioBase(
            concepto_id="RENTA_PLAN_MOVIL",
            nombre_comercial="Plan móvil",
            plan=plan,
            tarifa_cent=tarifa,
            servicio_id=f"{cuenta_id}-MOV",
        )
    ]


#: Departamentos con Lima sobrerrepresentada, que es como está la base real: el
#: vocabulario canónico es ``DEPARTAMENTOS``; esto solo es la urna de la que se sortea.
_DEPARTAMENTOS_PONDERADOS: tuple[str, ...] = (
    *(("Lima",) * 4),
    *DEPARTAMENTOS[1:],
)


def _perfil_comercial(segmento: str, rng: Random) -> dict[str, str]:
    """Perfil comercial del cliente con el vocabulario del dataset real de Movistar.

    Los cuatro valores salen de las columnas ``tipo_cliente``, ``edad_rango``,
    ``ubicacion_departamento`` y ``canal_mas_usado`` de ``dataset_clientes.csv``. No
    intervienen en ningún importe: son para personalizar el trato.

    ``tipo_cliente`` no se sortea a ciegas. Una línea móvil que se factura mes a mes es,
    por definición, **postpago**: un prepago no genera recibo mensual, se recarga. El
    único caso realista de cliente *prepago* con recibo es el de quien tiene el servicio
    de casa contratado y lleva el celular con recargas, muy común en Perú. Por eso solo
    el segmento HOGAR puede salir prepago; el resto es siempre postpago. Inventar
    prepagos con renta móvil daría un dataset con el vocabulario correcto y los hechos
    equivocados, que es peor que no tener el campo.
    """
    postpago, prepago_txt = TIPOS_CLIENTE
    prepago = segmento == "HOGAR" and rng.random() < 0.35
    return {
        "tipo_cliente": prepago_txt if prepago else postpago,
        "edad_rango": rng.choice(EDADES_RANGO),
        "ubicacion_departamento": rng.choice(_DEPARTAMENTOS_PONDERADOS),
        "canal_mas_usado": rng.choice(CANALES_CLIENTE),
    }


def construir_perfil(cuenta_id: str, rng: Random, indice: int) -> PerfilCliente:
    """Perfil sintético reproducible de un cliente.

    El segmento y la modalidad de renta se reparten por índice, no por azar, para que
    la muestra cubra siempre las dos modalidades y los cuatro segmentos aunque se
    generen pocos clientes.

    Se reparten por **bloque** y no por el propio índice: como el escenario se asigna
    con ``indice % 8``, usar ``indice % 2`` para la modalidad las dejaría correlacionadas
    y habría escenarios que jamás aparecerían en renta adelantada, justo los que hay que
    demostrar en ambas modalidades. Con el bloque, el ciclo completo
    (ocho escenarios × dos modalidades × cuatro segmentos) se cierra cada sesenta y
    cuatro clientes.
    """
    bloque = indice // len(NOMBRES_ESCENARIOS)
    segmento = _SEGMENTOS[(bloque // 2) % len(_SEGMENTOS)]
    modalidad = ModalidadRenta.ADELANTADA if bloque % 2 == 0 else ModalidadRenta.VENCIDA
    servicios = _servicios_de_segmento(cuenta_id, segmento, rng)

    consumos: dict[str, Centimos] = {}
    for concepto_id, minimo, maximo, paso in CONSUMOS_POSIBLES:
        if rng.random() < 0.30:
            consumos[concepto_id] = rng.randrange(minimo, maximo + 1, paso)

    descuento: DescuentoBase | None = None
    if rng.random() < 0.35:
        promocion_id, nombre = rng.choice(PROMOCIONES)
        base = servicios[0].tarifa_cent
        descuento = DescuentoBase(
            promocion_id=promocion_id,
            nombre=nombre,
            monto_cent=max(500, redondear_banca(base * rng.choice((1500, 2000, 2500)), 10_000)),
            meses_vigencia=rng.choice((6, 12)),
        )

    perfil = PerfilCliente(
        cuenta_id=cuenta_id,
        seed=semilla_cliente(0, cuenta_id),
        segmento=segmento,
        modalidad_renta=modalidad,
        dia_ciclo=rng.choice((1, 5, 8, 12, 15, 18, 22, 25)),
        servicios=servicios,
        consumos=consumos,
        descuento=descuento,
        beneficios=rng.sample(_BENEFICIOS, k=2),
        **_perfil_comercial(segmento, rng),
        base_movimiento_id=_base_movimiento_id(cuenta_id),
    )

    if rng.random() < 0.30:
        perfil.financiamiento = _financiamiento_base(perfil, rng)
    return perfil


def _financiamiento_base(perfil: PerfilCliente, rng: Random) -> FinanciamientoBase:
    """Equipo financiado que ya venía pagándose: cuota constante en los seis periodos.

    Es el distractor de la atribución: hay una línea grande y un movimiento en el
    historial, pero su delta es cero y no explica nada. El capital se elige múltiplo del
    número de cuotas para que la cuota sea idéntica todos los meses.
    """
    equipo, _precio = rng.choice(EQUIPOS)
    cuotas_totales = rng.choice((12, 18, 24))
    cuota = rng.choice((4500, 5900, 6900, 8500, 12900))
    cuota_en_actual = rng.randint(PERIODOS_HISTORIAL, cuotas_totales)
    return FinanciamientoBase(
        equipo=equipo,
        cuota_cent=cuota,
        cuotas_totales=cuotas_totales,
        cuota_en_actual=cuota_en_actual,
        movimiento_id=perfil.siguiente_movimiento_id(),
        fecha_alta=date(2024, 1, 1),  # se ajusta al construir el historial
    )


# --------------------------------------------------------------------------- #
# Guiones de la demo
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class GuionDemo:
    """Cliente de guion con perfil y opciones fijadas a mano para la demo en vivo."""

    cuenta_id: str
    titular: str
    escenarios: tuple[str, ...]
    perfil: PerfilCliente
    opciones: dict[str, dict[str, Any]] = field(default_factory=dict)


def _guion_demo_01() -> GuionDemo:
    """C-DEMO-01 — cambio de plan, renta ADELANTADA, cuota de equipo como distractor.

    El caso insignia: el cliente se pasa a un plan **más barato** de precio de lista y
    su recibo **sube**, porque el descuento estaba atado al plan anterior y porque la
    renta adelantada hace convivir el mes siguiente con el ajuste del mes en curso.
    Encima tiene una cuota de equipo grande que no cambió: la atribución ingenua la
    culparía por ser la línea más visible.
    """
    cuenta_id = "C-DEMO-01"
    perfil = PerfilCliente(
        cuenta_id=cuenta_id,
        seed=semilla_cliente(0, cuenta_id),
        segmento="PREMIUM",
        modalidad_renta=ModalidadRenta.ADELANTADA,
        dia_ciclo=1,
        servicios=[
            ServicioBase(
                concepto_id="RENTA_PLAN_MOVIL",
                nombre_comercial="Plan móvil",
                plan="Plan Movil Ilimitado",
                tarifa_cent=9990,
                servicio_id=f"{cuenta_id}-MOV",
            )
        ],
        consumos={"LLAMADAS_FUERA_DE_PLAN": 640},
        descuento=DescuentoBase(
            promocion_id="PROMO_FIDELIDAD",
            nombre="Descuento por permanencia",
            monto_cent=4990,
            meses_vigencia=12,
        ),
        beneficios=[
            "llamadas ilimitadas a todo destino nacional",
            "roaming incluido en la Comunidad Andina",
        ],
        tipo_cliente="postpago",
        edad_rango="36-45",
        ubicacion_departamento="Lima",
        canal_mas_usado="Digital",
        base_movimiento_id=_base_movimiento_id(cuenta_id),
    )
    perfil.financiamiento = FinanciamientoBase(
        equipo="Samsung Galaxy serie S",
        cuota_cent=12900,
        cuotas_totales=18,
        cuota_en_actual=11,
        movimiento_id=perfil.siguiente_movimiento_id(),
        fecha_alta=date(2024, 1, 1),
    )
    return GuionDemo(
        cuenta_id=cuenta_id,
        titular="Cliente de demostración uno",
        escenarios=("CAMBIO_PLAN_MEDIO_CICLO",),
        perfil=perfil,
        opciones={
            "CAMBIO_PLAN_MEDIO_CICLO": {
                "variante": "BAJADA_PIERDE_DESCUENTO",
                "plan_nuevo": "Plan Movil Max 50GB",
                "tarifa_nueva_cent": 7990,
                "dia_cambio": 12,
            }
        },
    )


def _guion_demo_02() -> GuionDemo:
    """C-DEMO-02 — corte por deuda y reconexión, renta VENCIDA.

    Le devuelven los días sin servicio y le cobran la reconexión en el mismo recibo. El
    neto sube, que es justo lo que el cliente no entiende cuando llama.
    """
    cuenta_id = "C-DEMO-02"
    perfil = PerfilCliente(
        cuenta_id=cuenta_id,
        seed=semilla_cliente(0, cuenta_id),
        segmento="MASIVO",
        modalidad_renta=ModalidadRenta.VENCIDA,
        dia_ciclo=5,
        servicios=[
            ServicioBase(
                concepto_id="RENTA_PLAN_MOVIL",
                nombre_comercial="Plan móvil",
                plan="Plan Movil Plus 25GB",
                tarifa_cent=5990,
                servicio_id=f"{cuenta_id}-MOV",
            )
        ],
        consumos={"CONSUMO_DATOS_ADICIONAL": 890},
        descuento=None,
        beneficios=[
            "redes sociales que no consumen sus datos",
            "acceso a Movistar TV App incluido",
        ],
        tipo_cliente="postpago",
        edad_rango="26-35",
        ubicacion_departamento="Piura",
        canal_mas_usado="Call In",
        base_movimiento_id=_base_movimiento_id(cuenta_id),
    )
    return GuionDemo(
        cuenta_id=cuenta_id,
        titular="Cliente de demostración dos",
        escenarios=("CORTE_RECONEXION",),
        perfil=perfil,
        opciones={"CORTE_RECONEXION": {"dia_suspension": 6, "dias_suspendido": 9}},
    )


def _guion_demo_03() -> GuionDemo:
    """C-DEMO-03 — fin de descuento **y** deuda anterior, renta VENCIDA.

    Dos causas de naturaleza distinta a la vez: una explica por qué subió el recibo del
    mes, la otra por qué subió el importe a pagar. Es el caso que obliga a separar
    ambas cifras y el que rompe cualquier explicación de causa única.
    """
    cuenta_id = "C-DEMO-03"
    perfil = PerfilCliente(
        cuenta_id=cuenta_id,
        seed=semilla_cliente(0, cuenta_id),
        segmento="HOGAR",
        modalidad_renta=ModalidadRenta.VENCIDA,
        dia_ciclo=10,
        servicios=[
            ServicioBase(
                concepto_id="RENTA_HOGAR_INTERNET",
                nombre_comercial="Internet hogar",
                plan="Internet Hogar 100Mb",
                tarifa_cent=8990,
                servicio_id=f"{cuenta_id}-BAF",
            ),
            ServicioBase(
                concepto_id="RENTA_TV",
                nombre_comercial="Televisión",
                plan="TV Hogar Sola",
                tarifa_cent=6990,
                servicio_id=f"{cuenta_id}-TV",
            ),
        ],
        consumos={},
        descuento=DescuentoBase(
            promocion_id="PROMO_BIENVENIDA",
            nombre="Descuento de bienvenida",
            monto_cent=3000,
            meses_vigencia=6,
        ),
        beneficios=[
            "acceso a Movistar TV App incluido",
            "instalación sin costo de sus equipos",
        ],
        tipo_cliente="prepago",
        edad_rango="56-65",
        ubicacion_departamento="Arequipa",
        canal_mas_usado="Tienda",
        base_movimiento_id=_base_movimiento_id(cuenta_id),
    )
    return GuionDemo(
        cuenta_id=cuenta_id,
        titular="Cliente de demostración tres",
        escenarios=("FIN_DESCUENTO", "DEUDA_ANTERIOR"),
        perfil=perfil,
        opciones={
            "FIN_DESCUENTO": {"dia_fin": 14},
            "DEUDA_ANTERIOR": {"porcentaje_bp": 10_000, "tasa_mora_bp": 150},
        },
    )


def _guiones_demo() -> list[GuionDemo]:
    """Los tres clientes de guion, en el orden en que se presentan en la demo."""
    return [_guion_demo_01(), _guion_demo_02(), _guion_demo_03()]


# --------------------------------------------------------------------------- #
# Selección de escenarios
# --------------------------------------------------------------------------- #
def elegir_escenarios(indice: int, rng: Random) -> list[str]:
    """Asigna escenarios a un cliente sintético.

    El primero se reparte por *round robin* sobre los ocho escenarios, de modo que la
    cobertura está garantizada aunque se generen pocos clientes. **Tres de cada diez**
    clientes reciben un segundo escenario compatible: son los casos en los que la
    atribución ingenua falla, porque hay dos causas en el mismo ciclo.

    ``ESTABLE`` es el control y nunca se combina (no tendría sentido: no cambia nada).
    Cuando a un cliente del tramo compuesto le tocaría ``ESTABLE``, se avanza en el
    reparto hasta el siguiente escenario combinable, de modo que la proporción del
    treinta por ciento se cumple exactamente.
    """
    posicion = indice % len(NOMBRES_ESCENARIOS)
    principal = NOMBRES_ESCENARIOS[posicion]
    if indice % 10 >= PROPORCION_COMPUESTOS:
        return [principal]

    for salto in range(len(NOMBRES_ESCENARIOS)):
        candidato = NOMBRES_ESCENARIOS[(posicion + salto) % len(NOMBRES_ESCENARIOS)]
        compatibles = sorted(
            nombre for nombre in NOMBRES_ESCENARIOS if son_compatibles(candidato, nombre)
        )
        if compatibles:
            return [candidato, rng.choice(compatibles)]
    return [principal]


# --------------------------------------------------------------------------- #
# Ensamblado del recibo
# --------------------------------------------------------------------------- #
def _linea_igv(
    lineas: list[LineaRecibo], ciclo: CicloFacturacion, reglas: ConfiguracionReglas
) -> LineaRecibo:
    """Calcula el IGV del periodo y reparte su residuo por mayor resto.

    El impuesto es una línea más del recibo (familia ``IMPUESTO``). Se calcula sobre la
    base afecta completa y luego se reparte entre las líneas afectas por mayor resto,
    lo que deja constancia auditable de cuánto impuesto aporta cada concepto sin que la
    suma pierda ni gane un céntimo.
    """
    afectas = [linea for linea in lineas if linea.afecto_igv]
    base_afecta = sum(linea.monto_cent for linea in afectas)
    igv = aplicar_porcentaje(base_afecta, reglas.politica.igv_bp)

    pesos = [abs(linea.monto_cent) for linea in afectas]
    partes = repartir_mayor_resto(igv, pesos) if sum(pesos) > 0 else [0] * len(afectas)
    reparto = {
        linea.concepto_id: reparto_linea
        for linea, reparto_linea in zip(afectas, partes, strict=True)
    }

    return construir_linea(
        concepto_id="IGV",
        monto_cent=igv,
        periodo_imputado=ciclo.periodo,
        reglas=reglas,
        descripcion="Impuesto general a las ventas sobre los servicios afectos",
        meta={
            "base_afecta_cent": base_afecta,
            "igv_bp": reglas.politica.igv_bp,
            "reparto_cent": reparto,
        },
    )


def ensamblar_lineas(
    perfil: PerfilCliente,
    ciclo: CicloFacturacion,
    resultados: Sequence[ResultadoEscenario],
    reglas: ConfiguracionReglas,
) -> list[LineaRecibo]:
    """Compone el recibo: base + escenarios + IGV, en orden canónico y renumerado.

    Una línea devuelta por un escenario **reemplaza** a la línea base del mismo
    concepto. Como dos escenarios solo se combinan si tocan conceptos disjuntos, nunca
    hay dos sustituciones sobre la misma línea.
    """
    mapa: dict[str, LineaRecibo] = {
        linea.concepto_id: linea for linea in lineas_base(perfil, ciclo, reglas)
    }
    for resultado in resultados:
        for linea in resultado.lineas:
            mapa[linea.concepto_id] = linea

    ordenadas = sorted(mapa.values(), key=lambda linea: orden_de_concepto(linea.concepto_id))
    ordenadas.append(_linea_igv(ordenadas, ciclo, reglas))
    return [
        linea.model_copy(update={"linea_id": numero})
        for numero, linea in enumerate(ordenadas, start=1)
    ]


def construir_recibo(
    perfil: PerfilCliente, ciclo: CicloFacturacion, lineas: list[LineaRecibo]
) -> Recibo:
    """Construye el ``Recibo``; su validador comprueba que las líneas suman el total."""
    return Recibo(
        recibo_id=f"R-{perfil.cuenta_id}-{ciclo.periodo}",
        cuenta_id=perfil.cuenta_id,
        periodo=ciclo.periodo,
        modalidad_renta=perfil.modalidad_renta,
        ciclo_inicio=ciclo.inicio,
        ciclo_fin=ciclo.fin,
        dias_ciclo=ciclo.dias,
        fecha_emision=ciclo.fecha_emision,
        fecha_vencimiento=ciclo.fecha_vencimiento,
        lineas=lineas,
        total_cent=sum(linea.monto_cent for linea in lineas),
        deuda_anterior_cent=ciclo.deuda_anterior_cent,
        estado_servicio=ciclo.estado_servicio,
        plan_vigente=ciclo.plan_vigente,
        meta={
            "segmento": perfil.segmento,
            "perfil_cliente": perfil.perfil_comercial(),
            "beneficios": list(perfil.beneficios),
            "notas_escenario": dict(ciclo.notas),
        },
    )


# --------------------------------------------------------------------------- #
# Ground truth del IGV y conciliación
# --------------------------------------------------------------------------- #
def _ground_truth_igv(
    filas: list[GroundTruthCausaDelta],
    delta_igv: Centimos,
    cuenta_id: str,
    periodo: str,
    reglas: ConfiguracionReglas,
) -> list[GroundTruthCausaDelta]:
    """Reparte el delta del IGV entre las causas que movieron la base afecta.

    No es una deducción a posteriori sobre el recibo terminado: se aplica el porcentaje
    de ley al delta que **cada causa ya había declarado**, y el residuo de redondeo
    (unos pocos céntimos, por la diferencia entre redondear la suma y sumar los
    redondeos) se asigna a la causa de mayor impacto absoluto. Así cada céntimo de
    impuesto queda atribuido y la conciliación cierra exacta.
    """
    afectas = [
        fila
        for fila in filas
        if fila.concepto_id != CONCEPTO_DEUDA_ANTERIOR
        and reglas.es_afecto_igv(fila.concepto_id)
        and fila.delta_cent != 0
    ]
    if not afectas:
        if delta_igv == 0:
            return []
        return [
            GroundTruthCausaDelta(
                cuenta_id=cuenta_id,
                periodo=periodo,
                concepto_id="IGV",
                causa=None,
                delta_cent=delta_igv,
                movimiento_id=None,
                escenario="IGV",
            )
        ]

    parciales = [aplicar_porcentaje(fila.delta_cent, reglas.politica.igv_bp) for fila in afectas]
    residual = delta_igv - sum(parciales)
    if residual != 0:
        dominante = max(
            range(len(afectas)),
            key=lambda indice: (abs(afectas[indice].delta_cent), afectas[indice].concepto_id),
        )
        parciales[dominante] += residual

    # Varias líneas pueden compartir causa (un mismo cambio de plan mueve la renta, el
    # ajuste y el descuento): se agrupan para que el ground truth tenga una fila de IGV
    # por causa, no una por línea.
    agrupado: dict[tuple[str | None, int | None, str | None], int] = defaultdict(int)
    for fila, parcial in zip(afectas, parciales, strict=True):
        agrupado[(fila.causa, fila.movimiento_id, fila.escenario)] += parcial

    return [
        GroundTruthCausaDelta(
            cuenta_id=cuenta_id,
            periodo=periodo,
            concepto_id="IGV",
            causa=causa,
            delta_cent=parcial,
            movimiento_id=movimiento_id,
            escenario=escenario,
        )
        for (causa, movimiento_id, escenario), parcial in sorted(
            agrupado.items(), key=lambda par: (str(par[0][0]), par[0][1] or 0)
        )
        if parcial != 0
    ]


def conciliar_ground_truth(
    actual: Recibo,
    previo: Recibo,
    filas: list[GroundTruthCausaDelta],
) -> None:
    """Comprueba que el ground truth reproduce exactamente la variación del recibo.

    Dos controles, en este orden:

    1. **Concepto por concepto**: el delta declarado por los escenarios debe coincidir
       con el delta real entre ambos recibos. Detecta el error de haber movido un
       importe sin declararlo (o de declararlo mal), que es el fallo silencioso más
       peligroso de un generador sintético.
    2. **Suma total**: ``Σ delta_cent == total_actual − total_previo``, excluyendo la
       deuda anterior, que por definición no forma parte del total del periodo.

    Raises:
        ErrorConciliacion: con el detalle exacto de cada discrepancia.
    """
    reales: dict[str, int] = defaultdict(int)
    for concepto_id, monto in actual.agrupar_por_concepto().items():
        reales[concepto_id] += monto
    for concepto_id, monto in previo.agrupar_por_concepto().items():
        reales[concepto_id] -= monto

    declarados: dict[str, int] = defaultdict(int)
    for fila in filas:
        if fila.concepto_id == CONCEPTO_DEUDA_ANTERIOR:
            continue
        declarados[fila.concepto_id] += fila.delta_cent

    problemas: list[str] = []
    for concepto_id in sorted(set(reales) | set(declarados)):
        real = reales.get(concepto_id, 0)
        declarado = declarados.get(concepto_id, 0)
        if real != declarado:
            problemas.append(
                f"  {concepto_id}: el recibo varió {real} céntimos y el ground truth "
                f"declara {declarado} (diferencia de {real - declarado})"
            )

    delta_total = actual.total_cent - previo.total_cent
    suma = sum(declarados.values())
    if suma != delta_total:
        problemas.append(
            f"  TOTAL: la suma del ground truth es {suma} céntimos y el delta real del "
            f"recibo es {delta_total} (diferencia de {suma - delta_total})"
        )

    deuda_declarada = sum(
        fila.delta_cent for fila in filas if fila.concepto_id == CONCEPTO_DEUDA_ANTERIOR
    )
    deuda_real = actual.deuda_anterior_cent - previo.deuda_anterior_cent
    if deuda_declarada != deuda_real:
        problemas.append(
            f"  DEUDA_ANTERIOR: la deuda arrastrada varió {deuda_real} céntimos y el "
            f"ground truth declara {deuda_declarada}"
        )

    if problemas:
        raise ErrorConciliacion(
            f"el ground truth de {actual.cuenta_id} en {actual.periodo} no cuadra con el "
            "recibo generado. No se escribe ningún fichero.\n" + "\n".join(problemas)
        )


# --------------------------------------------------------------------------- #
# Generación por cliente
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class HistorialCliente:
    """Todo lo generado para un cliente: recibos, órdenes y ground truth."""

    perfil: PerfilCliente
    escenarios: list[str]
    recibos: list[Recibo]
    movimientos: list[MovementEvent]
    ground_truth: list[GroundTruthCausaDelta]

    @property
    def recibo_actual(self) -> Recibo:
        """El recibo del periodo M0, el que se explica."""
        return self.recibos[-1]

    @property
    def recibo_previo(self) -> Recibo:
        """El recibo de M-1, contra el que se compara."""
        return self.recibos[-2]

    @property
    def delta_total_cent(self) -> Centimos:
        """Variación del total del periodo entre el recibo actual y el previo."""
        return self.recibo_actual.total_cent - self.recibo_previo.total_cent


def _movimiento_financiamiento_base(
    perfil: PerfilCliente, primer_ciclo_inicio: date
) -> MovementEvent | None:
    """Orden de alta del equipo financiado que ya venía del pasado.

    Se fecha **antes** del historial, a propósito: existe en Amdocs pero queda fuera de
    la ventana de atribución del ciclo actual. Un motor que la use para explicar la
    variación del mes está atribuyendo mal.
    """
    financiamiento = perfil.financiamiento
    if financiamiento is None:
        return None
    meses_atras = financiamiento.cuota_en_actual - 1 + (PERIODOS_HISTORIAL - 1)
    fecha = primer_ciclo_inicio - timedelta(days=30 * meses_atras + 2)
    financiamiento.fecha_alta = fecha
    return MovementEvent(
        movimiento_id=financiamiento.movimiento_id,
        cuenta_id=perfil.cuenta_id,
        tipo=TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
        ocurrido_en=datetime.combine(fecha, time(10, 30)),
        detalle=DetalleAltaEquipoFinanciado(
            equipo=financiamiento.equipo,
            principal_cent=financiamiento.principal_cent,
            cuotas_totales=financiamiento.cuotas_totales,
            tasa_mensual_bp=0,
            cuota_cent=financiamiento.cuota_cent,
        ).model_dump(mode="json"),
        canal="TIENDA",
        servicio_id=perfil.servicio_principal.servicio_id,
    )


def generar_cliente(
    perfil: PerfilCliente,
    escenarios: Sequence[Escenario],
    rng: Random,
    periodo_actual: str,
    reglas: ConfiguracionReglas,
    opciones_forzadas: dict[str, dict[str, Any]] | None = None,
    atributos_forzados: dict[str, Any] | None = None,
) -> HistorialCliente:
    """Genera el historial completo de un cliente y valida su ground truth.

    Los cinco periodos previos se generan sin escenarios (recibo base estable). En el
    periodo actual se aplican los escenarios asignados, que producen líneas, órdenes y
    ground truth en el mismo acto.

    ``opciones_forzadas`` y ``atributos_forzados`` se aplican **después** de
    ``preparar``: los clientes de guion fijan a mano lo que el azar habría elegido, sin
    que ``preparar`` pueda sobrescribirles el descuento ni el financiamiento.

    Raises:
        ErrorConciliacion: si el ground truth no reproduce la variación real.
    """
    for escenario in escenarios:
        escenario.preparar(perfil, rng)
    for nombre, valores in (opciones_forzadas or {}).items():
        perfil.opcion(nombre).update(valores)
    for atributo, valor in (atributos_forzados or {}).items():
        setattr(perfil, atributo, valor)

    periodos = [
        desplazar_periodo(periodo_actual, desplazamiento)
        for desplazamiento in range(-(PERIODOS_HISTORIAL - 1), 1)
    ]

    recibos: list[Recibo] = []
    movimientos: list[MovementEvent] = []
    ground_truth: list[GroundTruthCausaDelta] = []

    movimiento_base = _movimiento_financiamiento_base(
        perfil, inicio_de_ciclo(periodos[0], perfil.dia_ciclo)
    )
    if movimiento_base is not None:
        movimientos.append(movimiento_base)

    deuda_previa = 0
    for desplazamiento, periodo in enumerate(periodos, start=-(PERIODOS_HISTORIAL - 1)):
        ciclo = construir_ciclo(periodo, desplazamiento, perfil, reglas)
        ciclo.total_previo_cent = recibos[-1].total_cent if recibos else 0
        ciclo.deuda_previa_cent = deuda_previa

        resultados: list[ResultadoEscenario] = []
        if ciclo.es_actual:
            for escenario in escenarios:
                resultado = escenario.aplicar(perfil, ciclo, rng)
                resultados.append(resultado)
                movimientos.extend(resultado.movimientos)
                ground_truth.extend(resultado.ground_truth)

        lineas = ensamblar_lineas(perfil, ciclo, resultados, reglas)
        recibo = construir_recibo(perfil, ciclo, lineas)
        recibos.append(recibo)
        deuda_previa = recibo.deuda_anterior_cent

    actual, previo = recibos[-1], recibos[-2]
    delta_igv = actual.agrupar_por_concepto().get("IGV", 0) - previo.agrupar_por_concepto().get(
        "IGV", 0
    )
    ground_truth.extend(
        _ground_truth_igv(ground_truth, delta_igv, perfil.cuenta_id, actual.periodo, reglas)
    )
    conciliar_ground_truth(actual, previo, ground_truth)

    movimientos.sort(key=lambda evento: (evento.ocurrido_en, evento.movimiento_id))
    return HistorialCliente(
        perfil=perfil,
        escenarios=[escenario.nombre for escenario in escenarios],
        recibos=recibos,
        movimientos=movimientos,
        ground_truth=ground_truth,
    )


# --------------------------------------------------------------------------- #
# Serialización — forma de respuesta de BrainyBill
# --------------------------------------------------------------------------- #
def recibo_a_documento(recibo: Recibo) -> dict[str, Any]:
    """Proyecta un ``Recibo`` a la forma que devuelve la API de BrainyBill.

    ``header`` con la cabecera del documento y ``lines`` con el detalle. Los importes
    viajan en **céntimos enteros** (sufijo ``_cent``): el ACL de un sistema real
    convertiría aquí desde el decimal en soles del origen.
    """
    return {
        "header": {
            "recibo_id": recibo.recibo_id,
            "cuenta_id": recibo.cuenta_id,
            "periodo": recibo.periodo,
            "modalidad_renta": str(recibo.modalidad_renta),
            "emision": recibo.fecha_emision.isoformat(),
            "vencimiento": recibo.fecha_vencimiento.isoformat(),
            "ciclo_inicio": recibo.ciclo_inicio.isoformat(),
            "ciclo_fin": recibo.ciclo_fin.isoformat(),
            "dias_ciclo": recibo.dias_ciclo,
            "moneda": recibo.moneda,
            "total_cent": recibo.total_cent,
            "deuda_anterior_cent": recibo.deuda_anterior_cent,
            "total_a_pagar_cent": recibo.total_a_pagar_cent,
            "estado_servicio": str(recibo.estado_servicio),
            "plan_vigente": recibo.plan_vigente,
            "meta": recibo.meta,
        },
        "lines": [
            {
                "linea_id": linea.linea_id,
                "concepto_id": linea.concepto_id,
                "nombre_comercial": linea.nombre_comercial,
                "familia": str(linea.familia),
                "descripcion": linea.descripcion,
                "monto_cent": linea.monto_cent,
                "periodo": linea.periodo,
                "servicio_id": linea.servicio_id,
                "cantidad": linea.cantidad,
                "afecto_igv": linea.afecto_igv,
                "dias_prorrateo": linea.dias_prorrateo,
                "fecha_inicio": linea.fecha_inicio.isoformat() if linea.fecha_inicio else None,
                "fecha_fin": linea.fecha_fin.isoformat() if linea.fecha_fin else None,
                "cuota_numero": linea.cuota_numero,
                "cuotas_totales": linea.cuotas_totales,
                "movimiento_id": linea.movimiento_id,
                "tramos": [tramo.model_dump(mode="json") for tramo in linea.tramos],
                "meta": linea.meta,
            }
            for linea in linea_ordenadas(recibo)
        ],
    }


def linea_ordenadas(recibo: Recibo) -> list[LineaRecibo]:
    """Líneas del recibo en su orden de numeración."""
    return sorted(recibo.lineas, key=lambda linea: linea.linea_id)


def historial_a_documento(historial: HistorialCliente, seed: int) -> dict[str, Any]:
    """Documento completo de una cuenta: el recibo actual y los cinco previos.

    Los recibos van del más reciente al más antiguo, que es como los expone BrainyBill
    ("la factura actual y los cinco recibos previos").

    ``perfil_cliente`` viaja con los nombres de campo del dataset real de Movistar
    (``tipo_cliente``, ``edad_rango``, ``ubicacion_departamento``, ``canal_mas_usado``)
    para que el consumidor no tenga que traducir nada cuando la fuente deje de ser
    sintética. Va también dentro de ``header.meta`` de cada recibo, que es lo que
    sobrevive al ACL de entrada.
    """
    perfil = historial.perfil
    return {
        "cuenta_id": perfil.cuenta_id,
        "modalidad_renta": str(perfil.modalidad_renta),
        "segmento": perfil.segmento,
        "perfil_cliente": perfil.perfil_comercial(),
        "dia_ciclo": perfil.dia_ciclo,
        "moneda": "PEN",
        "generado_con_seed": seed,
        "escenarios_inyectados": historial.escenarios,
        "beneficios_vigentes": list(perfil.beneficios),
        "recibos": [
            recibo_a_documento(recibo)
            for recibo in sorted(historial.recibos, key=lambda recibo: recibo.periodo, reverse=True)
        ],
    }


# --------------------------------------------------------------------------- #
# Escritura de salidas
# --------------------------------------------------------------------------- #
def _escribir_json(ruta: Path, datos: Any) -> Path:
    """Vuelca datos como JSON legible en UTF-8."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ruta


def escribir_bills(salida: Path, historiales: list[HistorialCliente], seed: int) -> int:
    """Escribe ``bills/{cuenta_id}.json`` por cliente y devuelve cuántos ficheros creó."""
    directorio = salida / "bills"
    directorio.mkdir(parents=True, exist_ok=True)
    for historial in historiales:
        _escribir_json(
            directorio / f"{historial.perfil.cuenta_id}.json",
            historial_a_documento(historial, seed),
        )
    return len(historiales)


def escribir_ordenes(salida: Path, historiales: list[HistorialCliente]) -> int:
    """Escribe ``ordenes.csv`` con la forma del export de Amdocs.

    Las columnas son las del sistema real (``ORDER_ID``, ``ACCOUNT_ID``, …) y la
    conversión la hace el ACL. Ese CSV se vuelve a leer con
    ``movistar_map.a_movimiento``, de modo que el ACL queda ejercitado desde el primer
    día y no solo cuando llegue el dataset de Movistar.
    """
    ruta = salida / "ordenes.csv"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    filas = 0
    with ruta.open("w", encoding="utf-8", newline="") as fichero:
        escritor = csv.DictWriter(fichero, fieldnames=list(COLUMNAS_ORDENES))
        escritor.writeheader()
        for historial in historiales:
            for movimiento in historial.movimientos:
                escritor.writerow(fila_orden_desde_movimiento(movimiento))
                filas += 1
    return filas


def escribir_ground_truth(salida: Path, historiales: list[HistorialCliente]) -> int:
    """Escribe ``ground_truth.csv`` (tabla ``gt_causa_delta``) y devuelve las filas."""
    ruta = salida / "ground_truth.csv"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    campos = (
        "cuenta_id",
        "periodo",
        "concepto_id",
        "causa",
        "delta_cent",
        "movimiento_id",
        "escenario",
    )
    filas = 0
    with ruta.open("w", encoding="utf-8", newline="") as fichero:
        escritor = csv.DictWriter(fichero, fieldnames=list(campos))
        escritor.writeheader()
        for historial in historiales:
            for fila in historial.ground_truth:
                escritor.writerow(
                    {
                        "cuenta_id": fila.cuenta_id,
                        "periodo": fila.periodo,
                        "concepto_id": fila.concepto_id,
                        "causa": str(fila.causa) if fila.causa else "",
                        "delta_cent": fila.delta_cent,
                        "movimiento_id": fila.movimiento_id if fila.movimiento_id else "",
                        "escenario": fila.escenario or "",
                    }
                )
                filas += 1
    return filas


# --------------------------------------------------------------------------- #
# Resumen
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ResumenGeneracion:
    """Cifras de control de una generación completa."""

    seed: int
    periodo_actual: str
    rules_version: str
    salida: str
    clientes: int
    recibos: int
    ordenes: int
    filas_ground_truth: int
    por_escenario: dict[str, int]
    por_modalidad: dict[str, int]
    compuestos: int
    delta_medio_cent: int
    conceptos_catalogo: int
    faqs: int
    casuisticas: int

    def a_dict(self) -> dict[str, Any]:
        """Proyección serializable del resumen."""
        return {
            "seed": self.seed,
            "periodo_actual": self.periodo_actual,
            "rules_version": self.rules_version,
            "salida": self.salida,
            "clientes": self.clientes,
            "recibos": self.recibos,
            "ordenes": self.ordenes,
            "filas_ground_truth": self.filas_ground_truth,
            "por_escenario": dict(sorted(self.por_escenario.items())),
            "por_modalidad": dict(sorted(self.por_modalidad.items())),
            "clientes_con_dos_escenarios": self.compuestos,
            "delta_medio_cent": self.delta_medio_cent,
            "conceptos_catalogo": self.conceptos_catalogo,
            "faqs": self.faqs,
            "casuisticas": self.casuisticas,
        }

    def a_texto(self) -> str:
        """Resumen para la terminal, en español y sin adornos."""
        lineas = [
            f"Dataset sintético generado en {self.salida}",
            f"  semilla {self.seed} · periodo actual {self.periodo_actual} · "
            f"reglas {self.rules_version}",
            f"  {self.clientes} clientes · {self.recibos} recibos · {self.ordenes} órdenes",
            f"  {self.filas_ground_truth} filas de ground truth · "
            f"{self.compuestos} clientes con dos escenarios",
            f"  variación media del recibo: {formatear_soles(self.delta_medio_cent)}",
            f"  corpus: {self.conceptos_catalogo} conceptos · {self.faqs} FAQ · "
            f"{self.casuisticas} casuísticas",
            "  escenarios:",
        ]
        for nombre, cuantos in sorted(self.por_escenario.items()):
            lineas.append(f"    {nombre:<26} {cuantos}")
        lineas.append("  modalidad de renta:")
        for nombre, cuantos in sorted(self.por_modalidad.items()):
            lineas.append(f"    {nombre:<26} {cuantos}")
        return "\n".join(lineas)


# --------------------------------------------------------------------------- #
# Generación completa
# --------------------------------------------------------------------------- #
def generar_dataset(
    seed: int = SEED_POR_DEFECTO,
    clientes: int = 300,
    salida: str | Path = "data/sintetico",
    periodo_actual: str = PERIODO_ACTUAL_POR_DEFECTO,
    solo_demo: bool = False,
    escribir: bool = True,
) -> ResumenGeneracion:
    """Genera el dataset completo y lo escribe en disco.

    Args:
        seed: semilla global. La de cada cliente se deriva de ella por sha256.
        clientes: número total de cuentas, incluidos los tres clientes de guion.
        salida: directorio de destino (``data/sintetico`` por defecto).
        periodo_actual: periodo M0 en formato ``YYYY-MM``.
        solo_demo: genera únicamente los tres clientes de guion.
        escribir: si es ``False`` no toca el disco (útil para pruebas).

    Raises:
        ErrorConciliacion: si el ground truth de cualquier cliente no cuadra.
        ValueError: si hay identificadores de orden repetidos entre clientes.
    """
    reglas = cargar_reglas()
    destino = Path(salida)
    historiales: list[HistorialCliente] = []

    for guion in _guiones_demo():
        rng = Random(semilla_cliente(seed, guion.cuenta_id))
        historiales.append(
            generar_cliente(
                perfil=guion.perfil,
                escenarios=[obtener_escenario(nombre) for nombre in guion.escenarios],
                rng=rng,
                periodo_actual=periodo_actual,
                reglas=reglas,
                opciones_forzadas=guion.opciones,
                atributos_forzados={
                    "descuento": guion.perfil.descuento,
                    "financiamiento": guion.perfil.financiamiento,
                },
            )
        )

    if not solo_demo:
        sinteticos = max(0, clientes - len(CUENTAS_DEMO))
        for indice in range(sinteticos):
            cuenta_id = f"C-{indice + 1:05d}"
            rng = Random(semilla_cliente(seed, cuenta_id))
            perfil = construir_perfil(cuenta_id, rng, indice)
            nombres = elegir_escenarios(indice, rng)
            historiales.append(
                generar_cliente(
                    perfil=perfil,
                    escenarios=[obtener_escenario(nombre) for nombre in nombres],
                    rng=rng,
                    periodo_actual=periodo_actual,
                    reglas=reglas,
                )
            )

    _verificar_ordenes_unicas(historiales)

    por_escenario: dict[str, int] = defaultdict(int)
    por_modalidad: dict[str, int] = defaultdict(int)
    compuestos = 0
    suma_delta = 0
    for historial in historiales:
        for nombre in historial.escenarios:
            por_escenario[nombre] += 1
        por_modalidad[str(historial.perfil.modalidad_renta)] += 1
        if len(historial.escenarios) > 1:
            compuestos += 1
        suma_delta += historial.delta_total_cent

    conceptos = catalogo_seed.construir_catalogo_seed(reglas)
    faqs = faq_seed.construir_faqs()
    casuisticas = faq_seed.construir_casuisticas()

    resumen = ResumenGeneracion(
        seed=seed,
        periodo_actual=periodo_actual,
        rules_version=reglas.rules_version,
        salida=str(destino),
        clientes=len(historiales),
        recibos=sum(len(historial.recibos) for historial in historiales),
        ordenes=sum(len(historial.movimientos) for historial in historiales),
        filas_ground_truth=sum(len(historial.ground_truth) for historial in historiales),
        por_escenario=dict(por_escenario),
        por_modalidad=dict(por_modalidad),
        compuestos=compuestos,
        delta_medio_cent=redondear_banca(suma_delta, max(1, len(historiales))),
        conceptos_catalogo=len(conceptos),
        faqs=len(faqs),
        casuisticas=len(casuisticas),
    )

    if escribir:
        destino.mkdir(parents=True, exist_ok=True)
        escribir_bills(destino, historiales, seed)
        escribir_ordenes(destino, historiales)
        escribir_ground_truth(destino, historiales)
        catalogo_seed.escribir_catalogo(destino / "catalogo.json", reglas)
        faq_seed.escribir_faqs(destino / "faqs.json")
        faq_seed.escribir_casuisticas(destino / "casuisticas.json")
        _escribir_json(destino / "resumen.json", resumen.a_dict())

    return resumen


def _verificar_ordenes_unicas(historiales: list[HistorialCliente]) -> None:
    """Comprueba que ningún ``movimiento_id`` se repite entre cuentas.

    Los rangos se derivan del ``cuenta_id`` para conservar el aislamiento por cliente;
    una colisión es improbable pero posible, y silenciarla rompería la trazabilidad
    entre una línea del recibo y la orden que la originó.

    Raises:
        ValueError: si hay identificadores repetidos, indicando cuáles.
    """
    vistos: dict[int, str] = {}
    colisiones: list[str] = []
    for historial in historiales:
        for movimiento in historial.movimientos:
            duenio = vistos.get(movimiento.movimiento_id)
            if duenio is not None and duenio != movimiento.cuenta_id:
                colisiones.append(
                    f"  orden {movimiento.movimiento_id}: {duenio} y {movimiento.cuenta_id}"
                )
            vistos[movimiento.movimiento_id] = movimiento.cuenta_id
    if colisiones:
        raise ValueError(
            "hay identificadores de orden repetidos entre cuentas:\n" + "\n".join(colisiones)
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def construir_argumentos() -> argparse.ArgumentParser:
    """Analizador de argumentos de la línea de comandos."""
    analizador = argparse.ArgumentParser(
        prog="recibo-datagen",
        description=(
            "Genera el dataset sintético de recibos (actual + cinco previos), el "
            "historial de órdenes y el ground truth de las variaciones."
        ),
    )
    analizador.add_argument(
        "--seed",
        type=int,
        default=SEED_POR_DEFECTO,
        help=f"semilla global de la generación (por defecto {SEED_POR_DEFECTO})",
    )
    analizador.add_argument(
        "--clientes",
        type=int,
        default=300,
        help="número total de cuentas, incluidos los tres clientes de guion (por defecto 300)",
    )
    analizador.add_argument(
        "--salida",
        type=str,
        default=None,
        help="directorio de destino (por defecto data/sintetico dentro del proyecto)",
    )
    analizador.add_argument(
        "--periodo-actual",
        type=str,
        default=PERIODO_ACTUAL_POR_DEFECTO,
        dest="periodo_actual",
        help=f"periodo M0 en formato YYYY-MM (por defecto {PERIODO_ACTUAL_POR_DEFECTO})",
    )
    analizador.add_argument(
        "--solo-demo",
        action="store_true",
        dest="solo_demo",
        help="genera únicamente los tres clientes de guion de la demo",
    )
    return analizador


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada de ``recibo-datagen`` y de ``python -m packages.datagen.generar``.

    Devuelve 0 si todo cuadra, 2 si el ground truth no concilia y 3 ante un error de
    configuración (reglas, catálogo o corpus). Nunca escribe un dataset a medias: la
    conciliación ocurre antes de tocar el disco.
    """
    argumentos = construir_argumentos().parse_args(argv)
    salida = (
        Path(argumentos.salida) if argumentos.salida else raiz_proyecto() / "data" / "sintetico"
    )
    try:
        resumen = generar_dataset(
            seed=argumentos.seed,
            clientes=argumentos.clientes,
            salida=salida,
            periodo_actual=argumentos.periodo_actual,
            solo_demo=argumentos.solo_demo,
        )
    except ErrorConciliacion as error:
        print(f"ABORTADO: {error}", file=sys.stderr)
        return 2
    except (ValueError, KeyError, FileNotFoundError) as error:
        print(f"ABORTADO por error de configuración: {error}", file=sys.stderr)
        return 3

    print(resumen.a_texto())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
