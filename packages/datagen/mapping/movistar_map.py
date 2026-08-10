"""ACL (*anti-corruption layer*) entre el dataset externo y el modelo canónico.

**ESTE ES EL ÚNICO ARCHIVO QUE HAY QUE TOCAR CUANDO LLEGUE EL DATASET REAL DE
MOVISTAR.** Todo lo demás del proyecto —motor de cálculo, FactSet, verificador,
API, evaluación— trabaja exclusivamente contra el modelo canónico de
``packages.core_domain`` y no sabe qué columnas trae el fichero de origen.

Qué hay aquí, y solo aquí:

* ``COLUMN_MAP`` — nombres de columna del export tabular de recibos → campos nuestros.
* ``COLUMN_MAP_ORDENES`` — nombres de columna del export de órdenes de Amdocs → campos
  nuestros.
* ``CONCEPTO_MAP`` — códigos de concepto del facturador → ``concepto_id`` del catálogo.
* ``TIPO_ORDEN_MAP`` — tipos de orden del CRM → ``TipoMovimiento``.
* ``validar(df)`` — control de calidad de ingesta: **rechaza toda fila cuya suma de
  líneas no cuadre con el total del recibo**. Un recibo que no cuadra no se explica: se
  deriva. Por eso el descuadre se detecta aquí, en el borde, y no en el motor.

Convenciones de la fuente externa (documentadas, no adivinadas):

* Los importes vienen **en soles** como texto decimal (``"124.90"``, ``"1,234.50"``).
  Se convierten a céntimos enteros con ``dinero.a_centimos``. A partir de la frontera
  de este módulo, ningún importe vuelve a ser texto ni decimal.
* Las fechas vienen en ISO (``YYYY-MM-DD``); se aceptan además ``DD/MM/YYYY`` y
  ``YYYY-MM-DD HH:MM:SS``, que son las tres formas que aparecen en exports de CRM.
* Los rangos de fecha del origen suelen ser **inclusivos** en el extremo derecho; el
  modelo canónico los usa **exclusivos**. La conversión está en ``FIN_INCLUSIVO_EN_ORIGEN``
  y es un interruptor, no un cambio de código repartido por el proyecto.

``validar`` acepta indistintamente un ``DataFrame`` de pandas (por su método
``to_dict("records")``), una lista de diccionarios o cualquier iterable de mapas. Se
hace así a propósito: pandas no es dependencia del proyecto y la ingesta no debería
obligar a instalarlo.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from packages.core_domain.dinero import Centimos, a_centimos
from packages.core_domain.enums import TipoMovimiento
from packages.core_domain.esquemas.movimiento import MovementEvent

__all__ = [
    "COLUMNAS_ORDENES",
    "COLUMNAS_RECIBO_OBLIGATORIAS",
    "COLUMN_MAP",
    "COLUMN_MAP_ORDENES",
    "CONCEPTO_MAP",
    "FIN_INCLUSIVO_EN_ORIGEN",
    "TIPO_ORDEN_MAP",
    "TOLERANCIA_CUADRE_CENT",
    "a_movimiento",
    "concepto_canonico",
    "fila_orden_desde_movimiento",
    "normalizar_filas",
    "normalizar_orden",
    "validar",
    "validar_ordenes",
]

#: **[POR VALIDAR con Movistar]** Si el export marca el fin de vigencia como último día
#: incluido, hay que sumarle un día para pasar a la convención ``[inicio, fin)``.
FIN_INCLUSIVO_EN_ORIGEN = False

#: Un céntimo de tolerancia, la misma del invariante de conciliación (sección 4.6).
TOLERANCIA_CUADRE_CENT = 1


# --------------------------------------------------------------------------- #
# Mapa de columnas — export tabular de recibos (CSV/Excel de historiales masivos)
# --------------------------------------------------------------------------- #
COLUMN_MAP: dict[str, str] = {
    # Cabecera del recibo (se repite en cada fila de línea, como en todo export plano)
    "ACCOUNT_ID": "cuenta_id",
    "BILL_ID": "recibo_id",
    "BILL_PERIOD": "periodo",
    "BILLING_MODE": "modalidad_renta",
    "CYCLE_START": "ciclo_inicio",
    "CYCLE_END": "ciclo_fin",
    "ISSUE_DATE": "fecha_emision",
    "DUE_DATE": "fecha_vencimiento",
    "BILL_TOTAL_AMT": "total_cent",
    "PREV_BALANCE_AMT": "deuda_anterior_cent",
    "SERVICE_STATUS": "estado_servicio",
    "CURRENT_PLAN": "plan_vigente",
    # Línea de detalle
    "LINE_ID": "linea_id",
    "CHARGE_CODE": "concepto_externo",
    "CHARGE_DESC": "descripcion",
    "CHARGE_AMT": "monto_cent",
    "CHARGE_PERIOD": "periodo_imputado",
    "SERVICE_ID": "servicio_id",
    "QTY": "cantidad",
    "VAT_FLAG": "afecto_igv",
    "PRORATE_DAYS": "dias_prorrateo",
    "PRORATE_START": "fecha_inicio",
    "PRORATE_END": "fecha_fin",
    "INSTALLMENT_NBR": "cuota_numero",
    "INSTALLMENT_TOT": "cuotas_totales",
    "ORDER_ID": "movimiento_id",
}

#: Columnas sin las cuales la ingesta no puede ni empezar.
COLUMNAS_RECIBO_OBLIGATORIAS: tuple[str, ...] = (
    "ACCOUNT_ID",
    "BILL_ID",
    "BILL_PERIOD",
    "BILL_TOTAL_AMT",
    "CHARGE_CODE",
    "CHARGE_AMT",
)


# --------------------------------------------------------------------------- #
# Mapa de columnas — export de órdenes de Amdocs
# --------------------------------------------------------------------------- #
COLUMN_MAP_ORDENES: dict[str, str] = {
    "ORDER_ID": "movimiento_id",
    "ACCOUNT_ID": "cuenta_id",
    "ORDER_TYPE": "tipo",
    "ORDER_DATE": "ocurrido_en",
    "SERVICE_ID": "servicio_id",
    "CHANNEL": "canal",
    "DETAIL_JSON": "detalle",
}

#: Orden de columnas del CSV de órdenes (el generador escribe exactamente estas).
COLUMNAS_ORDENES: tuple[str, ...] = tuple(COLUMN_MAP_ORDENES)


# --------------------------------------------------------------------------- #
# Mapa de conceptos — códigos del facturador → catálogo canónico
# --------------------------------------------------------------------------- #
CONCEPTO_MAP: dict[str, str] = {
    # Rentas recurrentes
    "CF_MOVIL": "RENTA_PLAN_MOVIL",
    "RENTA_MOVIL": "RENTA_PLAN_MOVIL",
    "CF_BAF": "RENTA_HOGAR_INTERNET",
    "RENTA_INTERNET": "RENTA_HOGAR_INTERNET",
    "CF_TV": "RENTA_TV",
    "RENTA_TV": "RENTA_TV",
    "CF_FIJA": "RENTA_LINEA_FIJA",
    "RENTA_FIJA": "RENTA_LINEA_FIJA",
    "CF_MT": "RENTA_MOVISTAR_TOTAL",
    "RENTA_CONVERGENTE": "RENTA_MOVISTAR_TOTAL",
    # Ajustes y prorrateos
    "PRORR_RENTA": "PRORRATEO_PLAN",
    "PRORRATEO": "PRORRATEO_PLAN",
    "AJU_RETRO": "AJUSTE_RETROACTIVO_RENTA",
    "AJUSTE_RETROACTIVO": "AJUSTE_RETROACTIVO_RENTA",
    "AJU_SUSP": "AJUSTE_DIAS_SUSPENSION",
    "AJUSTE_SUSPENSION": "AJUSTE_DIAS_SUSPENSION",
    "CARGO_RECON": "CARGO_RECONEXION",
    "RECONEXION": "CARGO_RECONEXION",
    # Financiamiento de equipos
    "CUOTA_EQ": "CUOTA_EQUIPO_FINANCIADO",
    "FIN_EQUIPO": "CUOTA_EQUIPO_FINANCIADO",
    "INT_EQ": "INTERES_FINANCIAMIENTO",
    # Paquetes y servicios adicionales
    "PAQ_DATOS": "PAQUETE_DATOS_ADICIONAL",
    "BOLSA_DATOS": "PAQUETE_DATOS_ADICIONAL",
    "PAQ_ROAMING": "PAQUETE_ROAMING",
    "ROAMING": "PAQUETE_ROAMING",
    "PAQ_TV_PREM": "PAQUETE_TV_PREMIUM",
    "TV_PREMIUM": "PAQUETE_TV_PREMIUM",
    "SERV_SEGURO": "SERVICIO_ADICIONAL_SEGURO",
    "ALQ_EQUIPO": "ALQUILER_EQUIPO_HOGAR",
    "CARGO_INSTAL": "INSTALACION_HOGAR",
    "CARGO_TRASL": "CARGO_TRASLADO",
    # Consumo fuera de plan
    "CONS_VOZ": "LLAMADAS_FUERA_DE_PLAN",
    "VOZ_FUERA_PLAN": "LLAMADAS_FUERA_DE_PLAN",
    "CONS_DATOS": "CONSUMO_DATOS_ADICIONAL",
    "SMS_PREM": "SMS_PREMIUM",
    "LDI": "LARGA_DISTANCIA",
    "LARGA_DIST": "LARGA_DISTANCIA",
    # Créditos
    "DCTO_PROMO": "DESCUENTO_PROMOCIONAL",
    "DESCUENTO": "DESCUENTO_PROMOCIONAL",
    "DCTO_MT": "DESCUENTO_MOVISTAR_TOTAL",
    "DCTO_EQUIPO": "DESCUENTO_EQUIPO",
    "NC": "NOTA_CREDITO",
    "NOTA_CRED": "NOTA_CREDITO",
    "ND": "NOTA_DEBITO",
    "NOTA_DEB": "NOTA_DEBITO",
    # Otros
    "INT_MORA": "INTERES_MORATORIO",
    "SALDO_ANT": "DEUDA_ANTERIOR",
    "IGV": "IGV",
    "REDONDEO": "REDONDEO",
}


# --------------------------------------------------------------------------- #
# Mapa de tipos de orden — CRM → TipoMovimiento
# --------------------------------------------------------------------------- #
TIPO_ORDEN_MAP: dict[str, TipoMovimiento] = {
    "PLAN_CHANGE": TipoMovimiento.CAMBIO_PLAN,
    "CAMBIO_PLAN": TipoMovimiento.CAMBIO_PLAN,
    "SUSPEND": TipoMovimiento.SUSPENSION,
    "SUSPENSION": TipoMovimiento.SUSPENSION,
    "RESUME": TipoMovimiento.RECONEXION,
    "RECONEXION": TipoMovimiento.RECONEXION,
    "SERVICE_ADD": TipoMovimiento.ALTA_SERVICIO,
    "ALTA_SERVICIO": TipoMovimiento.ALTA_SERVICIO,
    "SERVICE_REMOVE": TipoMovimiento.BAJA_SERVICIO,
    "BAJA_SERVICIO": TipoMovimiento.BAJA_SERVICIO,
    "PROMO_END": TipoMovimiento.FIN_DESCUENTO,
    "FIN_DESCUENTO": TipoMovimiento.FIN_DESCUENTO,
    "ADDON_ADD": TipoMovimiento.ALTA_PAQUETE,
    "ALTA_PAQUETE": TipoMovimiento.ALTA_PAQUETE,
    "DEVICE_FINANCE": TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
    "ALTA_EQUIPO_FINANCIADO": TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
    "CREDIT_NOTE": TipoMovimiento.NOTA_CREDITO,
    "NOTA_CREDITO": TipoMovimiento.NOTA_CREDITO,
    "DEBIT_NOTE": TipoMovimiento.NOTA_DEBITO,
    "NOTA_DEBITO": TipoMovimiento.NOTA_DEBITO,
    "SUSPENSION_ADJ": TipoMovimiento.AJUSTE_SUSPENSION,
    "AJUSTE_SUSPENSION": TipoMovimiento.AJUSTE_SUSPENSION,
}


# --------------------------------------------------------------------------- #
# Conversión de valores
# --------------------------------------------------------------------------- #
def concepto_canonico(codigo: str | None) -> str | None:
    """Traduce un código de concepto del facturador al ``concepto_id`` del catálogo.

    Devuelve ``None`` si el código no está mapeado. Un concepto no mapeado **no se
    inventa**: se reporta como error de ingesta y, en tiempo de consulta, dispara la
    regla dura de derivación "concepto fuera de catálogo".
    """
    if codigo is None:
        return None
    clave = str(codigo).strip().upper()
    if clave in CONCEPTO_MAP:
        return CONCEPTO_MAP[clave]
    # Un export puede traer ya el identificador canónico; se acepta tal cual.
    return clave if clave in set(CONCEPTO_MAP.values()) else None


def _a_fecha(valor: Any) -> date | None:
    """Convierte una fecha del origen a ``date``; ``None`` si viene vacía."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValueError(f"fecha no reconocible en el origen: {valor!r}")


def _a_fecha_hora(valor: Any) -> datetime:
    """Convierte una marca temporal del origen a ``datetime``."""
    if isinstance(valor, datetime):
        return valor
    texto = str(valor).strip()
    for formato in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato)
        except ValueError:
            continue
    raise ValueError(f"marca temporal no reconocible en el origen: {valor!r}")


def _a_entero(valor: Any, por_defecto: int | None = None) -> int | None:
    """Convierte un entero del origen; ``None`` si viene vacío."""
    if valor is None or valor == "":
        return por_defecto
    return int(str(valor).strip())


def _a_booleano(valor: Any, por_defecto: bool = True) -> bool:
    """Interpreta los indicadores booleanos que usan los exports (``S/N``, ``1/0``)."""
    if valor is None or valor == "":
        return por_defecto
    return str(valor).strip().upper() in {"1", "S", "SI", "SÍ", "Y", "YES", "TRUE", "T"}


def _importe(valor: Any) -> Centimos:
    """Convierte un importe **en soles** del origen a céntimos enteros."""
    if valor is None or valor == "":
        return 0
    return a_centimos(valor if isinstance(valor, str) else str(valor))


def _registros(df: Any) -> list[Mapping[str, Any]]:
    """Normaliza la entrada a una lista de mapas.

    Acepta un ``DataFrame`` de pandas (vía ``to_dict("records")``), una lista de
    diccionarios o cualquier iterable de mapas. Evita imponer pandas como dependencia.
    """
    if hasattr(df, "to_dict"):
        return list(df.to_dict("records"))
    if isinstance(df, Mapping):
        return [df]
    if isinstance(df, Iterable):
        return [fila for fila in df if isinstance(fila, Mapping)]
    raise TypeError(f"no se sabe iterar un {type(df).__name__} como tabla de filas")


# --------------------------------------------------------------------------- #
# Normalización de filas de recibo
# --------------------------------------------------------------------------- #
def normalizar_filas(df: Any) -> list[dict[str, Any]]:
    """Traduce las filas del export de recibos al vocabulario canónico.

    No construye ``Recibo`` ni ``LineaRecibo``: solo renombra y convierte tipos. La
    construcción de los modelos la hace el cargador, que es quien decide qué hacer con
    las filas que ``validar`` haya rechazado.
    """
    normalizadas: list[dict[str, Any]] = []
    for fila in _registros(df):
        destino: dict[str, Any] = {}
        for columna, campo in COLUMN_MAP.items():
            if columna not in fila:
                continue
            valor = fila[columna]
            if campo in {"total_cent", "deuda_anterior_cent", "monto_cent"}:
                destino[campo] = _importe(valor)
            elif campo in {"ciclo_inicio", "ciclo_fin", "fecha_emision", "fecha_vencimiento"}:
                destino[campo] = _a_fecha(valor)
            elif campo in {"fecha_inicio", "fecha_fin"}:
                fecha = _a_fecha(valor)
                if fecha is not None and campo == "fecha_fin" and FIN_INCLUSIVO_EN_ORIGEN:
                    fecha = fecha + timedelta(days=1)
                destino[campo] = fecha
            elif campo in {
                "linea_id",
                "cantidad",
                "dias_prorrateo",
                "cuota_numero",
                "cuotas_totales",
                "movimiento_id",
            }:
                destino[campo] = _a_entero(valor, 1 if campo == "cantidad" else None)
            elif campo == "afecto_igv":
                destino[campo] = _a_booleano(valor)
            elif campo == "concepto_externo":
                destino["concepto_externo"] = str(valor).strip()
                destino["concepto_id"] = concepto_canonico(valor)
            else:
                destino[campo] = valor
        if FIN_INCLUSIVO_EN_ORIGEN and destino.get("ciclo_fin") is not None:
            destino["ciclo_fin"] = destino["ciclo_fin"] + timedelta(days=1)
        normalizadas.append(destino)
    return normalizadas


def validar(df: Any) -> list[str]:
    """Valida un export tabular de recibos y devuelve la lista de errores.

    Lista vacía significa que el fichero es apto para ingesta. Se comprueba:

    1. Que estén todas las columnas obligatorias.
    2. Que cada código de concepto esté mapeado en ``CONCEPTO_MAP`` (un concepto fuera
       de catálogo no puede explicarse y debe detectarse en la ingesta, no en la
       conversación con el cliente).
    3. Que los importes y las fechas sean convertibles.
    4. **Que la suma de las líneas de cada recibo coincida con su total**, con un
       céntimo de tolerancia. Es el mismo invariante de conciliación de la sección 4.6:
       un recibo que no cuadra no se explica, se deriva. Aquí se rechaza en el borde.

    Args:
        df: ``DataFrame`` de pandas, lista de diccionarios o iterable de mapas, con una
            fila por **línea de recibo** y la cabecera repetida en cada fila.

    Returns:
        Lista de mensajes de error, uno por problema detectado, en orden determinista.
    """
    filas = _registros(df)
    errores: list[str] = []
    if not filas:
        return ["el export está vacío: no hay ninguna fila que ingerir"]

    columnas = set(filas[0].keys())
    faltantes = [columna for columna in COLUMNAS_RECIBO_OBLIGATORIAS if columna not in columnas]
    if faltantes:
        errores.append(
            "faltan columnas obligatorias en el export de recibos: " + ", ".join(faltantes)
        )
        return errores

    sumas: dict[tuple[str, str], int] = {}
    totales: dict[tuple[str, str], int] = {}
    periodos: dict[tuple[str, str], str] = {}

    for indice, fila in enumerate(filas, start=1):
        cuenta = str(fila.get("ACCOUNT_ID", "")).strip()
        recibo = str(fila.get("BILL_ID", "")).strip()
        if not cuenta or not recibo:
            errores.append(f"fila {indice}: ACCOUNT_ID o BILL_ID vacíos")
            continue
        clave = (cuenta, recibo)

        codigo = fila.get("CHARGE_CODE")
        if concepto_canonico(codigo) is None:
            errores.append(
                f"fila {indice} (recibo {recibo}): concepto no mapeado en CONCEPTO_MAP: "
                f"{codigo!r}. Añádalo a este archivo, no al motor."
            )

        try:
            monto = _importe(fila.get("CHARGE_AMT"))
        except (ValueError, TypeError) as exc:
            errores.append(f"fila {indice} (recibo {recibo}): importe de línea inválido: {exc}")
            continue
        try:
            total = _importe(fila.get("BILL_TOTAL_AMT"))
        except (ValueError, TypeError) as exc:
            errores.append(f"fila {indice} (recibo {recibo}): total del recibo inválido: {exc}")
            continue

        for columna in ("CYCLE_START", "CYCLE_END", "ISSUE_DATE", "DUE_DATE"):
            if columna in fila:
                try:
                    _a_fecha(fila[columna])
                except ValueError as exc:
                    errores.append(f"fila {indice} (recibo {recibo}): {columna} inválida: {exc}")

        sumas[clave] = sumas.get(clave, 0) + monto
        anterior = totales.get(clave)
        if anterior is not None and anterior != total:
            errores.append(
                f"recibo {recibo} de la cuenta {cuenta}: la cabecera trae dos totales "
                f"distintos ({anterior} y {total} céntimos)"
            )
        totales[clave] = total
        periodos[clave] = str(fila.get("BILL_PERIOD", "")).strip()

    for clave in sorted(sumas):
        cuenta, recibo = clave
        residual = sumas[clave] - totales[clave]
        if abs(residual) > TOLERANCIA_CUADRE_CENT:
            errores.append(
                f"RECHAZADO recibo {recibo} de la cuenta {cuenta} (periodo "
                f"{periodos.get(clave, '?')}): la suma de líneas es {sumas[clave]} céntimos "
                f"y el total declarado es {totales[clave]}; descuadre de {residual} céntimos. "
                "Un recibo que no cuadra no se explica: se deriva."
            )
    return errores


# --------------------------------------------------------------------------- #
# Normalización de órdenes
# --------------------------------------------------------------------------- #
def normalizar_orden(fila: Mapping[str, Any]) -> dict[str, Any]:
    """Traduce una fila del export de órdenes al vocabulario canónico.

    Raises:
        ValueError: si el tipo de orden no está en ``TIPO_ORDEN_MAP`` o la fecha no es
            reconocible.
    """
    tipo_externo = str(fila.get("ORDER_TYPE", "")).strip().upper()
    tipo = TIPO_ORDEN_MAP.get(tipo_externo)
    if tipo is None:
        raise ValueError(
            f"tipo de orden no mapeado en TIPO_ORDEN_MAP: {tipo_externo!r}. "
            "Añádalo a este archivo, no al motor de atribución."
        )
    detalle_bruto = fila.get("DETAIL_JSON") or "{}"
    if isinstance(detalle_bruto, Mapping):
        detalle = dict(detalle_bruto)
    else:
        detalle = json.loads(str(detalle_bruto))
    servicio = fila.get("SERVICE_ID")
    canal = fila.get("CHANNEL")
    return {
        "movimiento_id": int(str(fila["ORDER_ID"]).strip()),
        "cuenta_id": str(fila["ACCOUNT_ID"]).strip(),
        "tipo": tipo,
        "ocurrido_en": _a_fecha_hora(fila["ORDER_DATE"]),
        "detalle": detalle,
        "canal": str(canal).strip() if canal else None,
        "servicio_id": str(servicio).strip() if servicio else None,
    }


def a_movimiento(fila: Mapping[str, Any]) -> MovementEvent:
    """Construye un ``MovementEvent`` canónico desde una fila del export de órdenes."""
    return MovementEvent.model_validate(normalizar_orden(fila))


def fila_orden_desde_movimiento(movimiento: MovementEvent) -> dict[str, str]:
    """Proyecta un ``MovementEvent`` a una fila del export de órdenes.

    Es la dirección inversa del ACL y existe para que el generador sintético escriba
    ``ordenes.csv`` **con los nombres de columna del sistema real**. Así el ACL se
    ejercita desde hoy: el pipeline lee el CSV con ``a_movimiento`` y, cuando llegue el
    export de Amdocs de verdad, el único cambio es este archivo.
    """
    return {
        "ORDER_ID": str(movimiento.movimiento_id),
        "ACCOUNT_ID": movimiento.cuenta_id,
        "ORDER_TYPE": str(movimiento.tipo),
        "ORDER_DATE": movimiento.ocurrido_en.isoformat(sep=" ", timespec="seconds"),
        "SERVICE_ID": movimiento.servicio_id or "",
        "CHANNEL": movimiento.canal or "",
        "DETAIL_JSON": json.dumps(movimiento.detalle, ensure_ascii=False, sort_keys=True),
    }


def validar_ordenes(df: Any) -> list[str]:
    """Valida un export de órdenes y devuelve la lista de errores.

    Comprueba columnas obligatorias, unicidad de ``ORDER_ID``, tipos mapeados y fechas
    convertibles. Lista vacía significa apto para ingesta.
    """
    filas = _registros(df)
    errores: list[str] = []
    if not filas:
        return ["el export de órdenes está vacío"]

    columnas = set(filas[0].keys())
    faltantes = [
        columna
        for columna in ("ORDER_ID", "ACCOUNT_ID", "ORDER_TYPE", "ORDER_DATE")
        if columna not in columnas
    ]
    if faltantes:
        return ["faltan columnas obligatorias en el export de órdenes: " + ", ".join(faltantes)]

    vistos: set[str] = set()
    for indice, fila in enumerate(filas, start=1):
        identificador = str(fila.get("ORDER_ID", "")).strip()
        if not identificador:
            errores.append(f"fila {indice}: ORDER_ID vacío")
            continue
        if identificador in vistos:
            errores.append(f"fila {indice}: ORDER_ID duplicado: {identificador}")
        vistos.add(identificador)
        try:
            normalizar_orden(fila)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            errores.append(f"fila {indice} (orden {identificador}): {exc}")
    return errores
