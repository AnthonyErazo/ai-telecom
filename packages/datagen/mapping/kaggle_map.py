"""ACL de **ensayo** contra un dataset público tabular de telecomunicaciones.

Este archivo es el **hermano gemelo** de :mod:`packages.datagen.mapping.movistar_map`.
Tiene la misma estructura (``COLUMN_MAP`` → campos canónicos, mapas de valores,
``validar(df) -> list[str]``, normalización de filas) y la misma regla de oro: *si un
valor del origen no está mapeado, no se inventa*. La diferencia está en el origen y,
sobre todo, en lo que se puede y no se puede hacer con él.

Por qué existe, y por qué NO es la fuente de datos del proyecto
---------------------------------------------------------------

Los datasets públicos de telecomunicaciones disponibles son, casi sin excepción,
**datasets de fuga de clientes** (*churn*): tienen **una fila por cliente** con
variables **agregadas** — antigüedad en meses, cargo mensual, cargo total acumulado,
tipo de contrato, servicios contratados, método de pago —. No tienen:

* líneas de recibo por concepto (ni renta, ni prorrateo, ni IGV desglosado),
* seis recibos por cliente,
* historial de órdenes (cambios de plan, suspensiones, reconexiones, altas),
* fechas de ciclo, modalidad de renta ni convención de prorrateo,
* ninguna variación **entre meses**, que es exactamente el objeto de este proyecto.

Con una sola fila agregada por cliente **no se puede construir ni un solo ``FactSet``**:
no hay nada que diferenciar entre un mes y el anterior. Por eso este adaptador **no**
alimenta la demo ni la evaluación oficial: el dataset del proyecto sigue siendo el
sintético propio de ``packages.datagen.generar``, cuyo *ground truth* es exacto por
construcción.

Qué sí aporta, y es su único propósito
--------------------------------------

Demostrar —ejecutándolo, no contándolo— que el *anti-corruption layer* funciona contra
un **esquema que no diseñamos nosotros**: nombres de columna ajenos, valores en inglés,
columnas que sobran, columnas que faltan y filas que hay que rechazar. Es el ensayo
general del día en que llegue el export real de Movistar, y la prueba de que el motor
no sabe —ni necesita saber— de dónde vienen los datos.

Honestidad metodológica (léase antes de usar nada de aquí)
----------------------------------------------------------

La **síntesis honesta** que hace este módulo separa, campo por campo, tres orígenes:

* ``DATASET_EXTERNO`` — el valor viene tal cual del CSV: identificador de cliente,
  antigüedad en meses, cargo mensual, cargo total, tipo de contrato, método de pago y
  qué servicios tiene contratados.
* ``DERIVADO_DEL_DATASET`` — se calcula a partir de lo anterior con una regla escrita y
  auditable: la tarifa de cada servicio (reparto del cargo mensual real desagregando el
  IGV), la modalidad de renta (del tipo de contrato), el segmento y cuántos periodos se
  pueden sintetizar (acotado por la antigüedad real).
* ``SINTETIZADO_POR_EL_EQUIPO`` — **no está en el dataset y lo inventamos nosotros**:
  el desglose por concepto, las fechas de ciclo y de vencimiento, el IGV línea a línea,
  el escenario de variación del último periodo y todos los movimientos de CRM.

Cada cuenta producida lleva ese desglose en su fichero (``procedencia``), y el resumen
de la ejecución lo repite en la terminal. **Consecuencia directa, y no es una nota al
pie:** los recibos derivados de este adaptador sirven para *ejercitar la ingesta*, y
**no** para validar la exactitud del motor. Medir la tasa de alucinación o la precisión
de atribución contra ellos sería medirnos contra nuestra propia invención.

Convenciones del origen (documentadas, no adivinadas)
-----------------------------------------------------

* **No se asume el nombre exacto de las columnas de ningún dataset concreto.**
  ``COLUMN_MAP`` recoge los nombres más habituales, ya normalizados (minúsculas y sin
  separadores), y :func:`detectar_esquema` informa de qué encontró, qué falta y qué
  ignoró. Añadir un alias es añadir una línea a este archivo.
* Los importes vienen **en unidades monetarias decimales** (``"64.75"``) y se convierten
  a céntimos enteros con ``dinero.a_centimos``. Pasada esta frontera no vuelve a haber
  decimales.
* Los booleanos vienen como ``Yes``/``No`` y, en las columnas de servicio, también como
  ``No internet service`` o el nombre de la tecnología (``DSL``, ``Fiber optic``): todo
  lo que no sea una negación explícita cuenta como servicio contratado.

Igual que su hermano, ``validar`` acepta un ``DataFrame`` de pandas (por su método
``to_dict("records")``), una lista de diccionarios o cualquier iterable de mapas:
pandas no es dependencia del proyecto y la ingesta no debería obligar a instalarlo.

Uso desde la línea de comandos::

    python -m packages.datagen.mapping.kaggle_map --csv data/ejemplos_externos/telco_ficticio.csv \\
        --salida data/externo/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from random import Random
from typing import Any

from packages.core_domain.dinero import (
    Centimos,
    a_centimos,
    formatear_soles,
    redondear_banca,
    repartir_mayor_resto,
)
from packages.core_domain.enums import ModalidadRenta
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas, raiz_proyecto
from packages.datagen.escenarios import (
    PROMOCIONES,
    DescuentoBase,
    PerfilCliente,
    ServicioBase,
    obtener_escenario,
)
from packages.datagen.generar import (
    PERIODO_ACTUAL_POR_DEFECTO,
    PERIODOS_HISTORIAL,
    SEED_POR_DEFECTO,
    ErrorConciliacion,
    HistorialCliente,
    escribir_ground_truth,
    escribir_ordenes,
    generar_cliente,
    historial_a_documento,
    semilla_cliente,
)

__all__ = [
    "ANTIGUEDAD_MINIMA_MESES",
    "CAMPOS_CANONICOS",
    "CAMPOS_OBLIGATORIOS",
    "CAMPOS_SERVICIO",
    "COLUMN_MAP",
    "CONTRATO_MAP",
    "ESCENARIOS_ENSAYO",
    "METODO_PAGO_MAP",
    "NOMBRE_PLAN_SINTETIZADO",
    "ORIGEN_DATASET",
    "ORIGEN_DERIVADO",
    "ORIGEN_SINTETICO",
    "PESOS_SERVICIO",
    "PORCENTAJE_DESCUENTO_BP",
    "PREFIJO_CUENTA_EXTERNA",
    "PROCEDENCIA_CAMPOS",
    "SERVICIO_MAP",
    "TOLERANCIA_COHERENCIA_BP",
    "CuentaSintetizada",
    "InformeEsquema",
    "ResumenIngesta",
    "columna_canonica",
    "construir_argumentos",
    "detectar_esquema",
    "escribir_salida",
    "ingerir",
    "leer_csv",
    "main",
    "normalizar_filas",
    "procedencia_de_cuenta",
    "sintetizar_cuenta",
    "validar",
]


# --------------------------------------------------------------------------- #
# Etiquetas de procedencia — el corazón de la honestidad metodológica
# --------------------------------------------------------------------------- #
#: El valor viene **tal cual** del CSV externo.
ORIGEN_DATASET = "DATASET_EXTERNO"

#: El valor se calcula a partir del dataset con una regla escrita en este archivo.
ORIGEN_DERIVADO = "DERIVADO_DEL_DATASET"

#: El valor **no está en el dataset**: lo inventa el equipo. No vale para medir nada.
ORIGEN_SINTETICO = "SINTETIZADO_POR_EL_EQUIPO"

#: Aviso que viaja dentro de cada cuenta producida y que se imprime en cada ejecución.
AVISO_SINTESIS = (
    "Recibos PARCIALMENTE SINTÉTICOS. El cargo mensual, la antigüedad y los servicios "
    "contratados proceden del dataset externo; el desglose por concepto, las fechas de "
    "ciclo, el IGV y los movimientos de CRM los sintetiza el equipo. Sirven para "
    "ejercitar la ingesta, NO para validar la exactitud del motor."
)

#: Antigüedad mínima para poder sintetizar el historial completo (actual + cinco previos).
ANTIGUEDAD_MINIMA_MESES = PERIODOS_HISTORIAL

#: Desviación máxima admitida entre ``cargo_total`` y ``cargo_mensual × antigüedad``,
#: en puntos básicos (2500 bp = 25 %). Es el equivalente al cuadre de recibo del
#: hermano: un agregado que no cuadra no se sintetiza, se descarta.
TOLERANCIA_COHERENCIA_BP = 2_500

#: Descuento sintetizado, en puntos básicos sobre la base afecta, cuando el escenario
#: asignado a la cuenta es ``FIN_DESCUENTO`` (que exige un descuento vigente previo).
PORCENTAJE_DESCUENTO_BP = 2_000

#: Prefijo de los identificadores de cuenta producidos. Deja a la vista, en el propio
#: identificador, que la cuenta **no** pertenece al dataset del proyecto.
PREFIJO_CUENTA_EXTERNA = "EXT"


# --------------------------------------------------------------------------- #
# Mapa de columnas — nombres habituales del export tabular → campos canónicos
# --------------------------------------------------------------------------- #
# Las claves están **ya normalizadas**: minúsculas y sin nada que no sea letra o
# dígito ("Monthly Charges", "monthly_charges" y "MonthlyCharges" son la misma clave).
# Añadir un dataset nuevo es añadir alias aquí, y solo aquí.
COLUMN_MAP: dict[str, str] = {
    # Identificador del cliente
    "customerid": "cliente_ref",
    "clientid": "cliente_ref",
    "subscriberid": "cliente_ref",
    "accountid": "cliente_ref",
    "idcliente": "cliente_ref",
    "clienteid": "cliente_ref",
    "id": "cliente_ref",
    # Antigüedad de la relación, en meses
    "tenure": "antiguedad_meses",
    "tenuremonths": "antiguedad_meses",
    "tenureinmonths": "antiguedad_meses",
    "monthsastomer": "antiguedad_meses",
    "antiguedad": "antiguedad_meses",
    "antiguedadmeses": "antiguedad_meses",
    "mesesantiguedad": "antiguedad_meses",
    # Cargo mensual (el importe recurrente que paga hoy)
    "monthlycharges": "cargo_mensual",
    "monthlycharge": "cargo_mensual",
    "monthlyfee": "cargo_mensual",
    "chargemonthly": "cargo_mensual",
    "cargomensual": "cargo_mensual",
    "mensualidad": "cargo_mensual",
    "arpu": "cargo_mensual",
    # Cargo acumulado a lo largo de toda la relación
    "totalcharges": "cargo_total",
    "totalcharge": "cargo_total",
    "totalspend": "cargo_total",
    "cargototal": "cargo_total",
    "totalfacturado": "cargo_total",
    # Tipo de contrato
    "contract": "tipo_contrato",
    "contracttype": "tipo_contrato",
    "contrato": "tipo_contrato",
    "tipocontrato": "tipo_contrato",
    # Método de pago
    "paymentmethod": "metodo_pago",
    "paymenttype": "metodo_pago",
    "metodopago": "metodo_pago",
    "formapago": "metodo_pago",
    # Servicios contratados
    "phoneservice": "servicio_telefono",
    "phone": "servicio_telefono",
    "telephoneservice": "servicio_telefono",
    "serviciotelefono": "servicio_telefono",
    "telefonia": "servicio_telefono",
    "internetservice": "servicio_internet",
    "internet": "servicio_internet",
    "broadband": "servicio_internet",
    "servicointernet": "servicio_internet",
    "streamingtv": "servicio_tv",
    "tvservice": "servicio_tv",
    "television": "servicio_tv",
    "serviciotv": "servicio_tv",
    # Señales adicionales que se leen pero **no** se usan para sintetizar
    "paperlessbilling": "facturacion_electronica",
    "ebilling": "facturacion_electronica",
    "facturaelectronica": "facturacion_electronica",
    "churn": "fuga",
    "churned": "fuga",
    "exited": "fuga",
    "fuga": "fuga",
    "baja": "fuga",
}

#: Campos canónicos en orden de aparición, sin repetir.
CAMPOS_CANONICOS: tuple[str, ...] = tuple(dict.fromkeys(COLUMN_MAP.values()))

#: Sin estos tres campos la ingesta no puede ni empezar: no hay cliente, no hay
#: importe recurrente o no se sabe si el cliente lleva suficientes meses.
CAMPOS_OBLIGATORIOS: tuple[str, ...] = (
    "cliente_ref",
    "antiguedad_meses",
    "cargo_mensual",
)

#: Campos de servicio, en el orden en que se reparte el cargo mensual entre ellos.
CAMPOS_SERVICIO: tuple[str, ...] = (
    "servicio_internet",
    "servicio_telefono",
    "servicio_tv",
)


# --------------------------------------------------------------------------- #
# Mapas de valores — texto ajeno → vocabulario canónico
# --------------------------------------------------------------------------- #
#: Tipo de contrato → (etiqueta canónica, modalidad de renta).
#:
#: **[SUPUESTO DEL EQUIPO, marcado como tal]** El dataset externo no dice si la renta se
#: cobra por adelantado o vencida: esa distinción no existe en un dataset de fuga. Se
#: adopta la convención de que un contrato con permanencia se factura por adelantado y
#: uno mes a mes, vencido. **Es una suposición nuestra**, y por eso ``modalidad_renta``
#: viaja etiquetada como ``DERIVADO_DEL_DATASET`` y nunca como dato del origen.
CONTRATO_MAP: dict[str, tuple[str, ModalidadRenta]] = {
    "monthtomonth": ("MES_A_MES", ModalidadRenta.VENCIDA),
    "monthly": ("MES_A_MES", ModalidadRenta.VENCIDA),
    "mtm": ("MES_A_MES", ModalidadRenta.VENCIDA),
    "mesames": ("MES_A_MES", ModalidadRenta.VENCIDA),
    "mensual": ("MES_A_MES", ModalidadRenta.VENCIDA),
    "sinpermanencia": ("MES_A_MES", ModalidadRenta.VENCIDA),
    "oneyear": ("ANUAL", ModalidadRenta.ADELANTADA),
    "annual": ("ANUAL", ModalidadRenta.ADELANTADA),
    "anual": ("ANUAL", ModalidadRenta.ADELANTADA),
    "unano": ("ANUAL", ModalidadRenta.ADELANTADA),
    "twoyear": ("BIANUAL", ModalidadRenta.ADELANTADA),
    "biannual": ("BIANUAL", ModalidadRenta.ADELANTADA),
    "bianual": ("BIANUAL", ModalidadRenta.ADELANTADA),
    "dosanos": ("BIANUAL", ModalidadRenta.ADELANTADA),
}

#: Método de pago → etiqueta canónica. Se lee y se conserva en la procedencia; **no
#: interviene en ninguna cifra**, porque el método de pago no cambia lo que se cobra.
METODO_PAGO_MAP: dict[str, str] = {
    "electroniccheck": "CHEQUE_ELECTRONICO",
    "mailedcheck": "CHEQUE_POSTAL",
    "banktransferautomatic": "DEBITO_BANCARIO",
    "banktransfer": "DEBITO_BANCARIO",
    "debitoautomatico": "DEBITO_BANCARIO",
    "creditcardautomatic": "TARJETA_CREDITO",
    "creditcard": "TARJETA_CREDITO",
    "tarjetacredito": "TARJETA_CREDITO",
    "efectivo": "EFECTIVO",
    "cash": "EFECTIVO",
}

#: Campo de servicio → ``concepto_id`` del catálogo canónico de ``rules.yaml``.
SERVICIO_MAP: dict[str, str] = {
    "servicio_internet": "RENTA_HOGAR_INTERNET",
    "servicio_telefono": "RENTA_LINEA_FIJA",
    "servicio_tv": "RENTA_TV",
}

#: Reparto del cargo mensual real entre los servicios contratados, en pesos relativos.
#: **[SUPUESTO DEL EQUIPO]** El dataset da un único importe agregado y no dice cuánto
#: corresponde a cada servicio. Estos pesos son nuestros; el importe total que producen
#: sí es el real. Por eso las tarifas por servicio viajan como ``DERIVADO_DEL_DATASET``.
PESOS_SERVICIO: dict[str, int] = {
    "RENTA_HOGAR_INTERNET": 60,
    "RENTA_LINEA_FIJA": 25,
    "RENTA_TV": 15,
}

#: Nombre del plan sintetizado por concepto. Lleva la palabra "Externo" **a propósito**:
#: el nombre del plan viaja dentro del ``FactSet`` y llega a la explicación, así que la
#: marca de procedencia tiene que ser visible ahí también. Sin dígitos, como todo nombre
#: propio del generador (un dígito en un nombre sería una cifra imposible de anclar).
NOMBRE_PLAN_SINTETIZADO: dict[str, str] = {
    "RENTA_HOGAR_INTERNET": "Plan Externo Hogar",
    "RENTA_LINEA_FIJA": "Plan Externo Línea Fija",
    "RENTA_TV": "Plan Externo Televisión",
    "RENTA_PLAN_MOVIL": "Plan Externo Móvil",
}

#: Servicio de reserva cuando el CSV no permite saber qué tiene contratado el cliente.
#: Es **sintetizado**, no derivado: el dataset no lo dice.
CONCEPTO_SERVICIO_POR_DEFECTO = "RENTA_PLAN_MOVIL"

#: Conceptos de renta con catálogo de planes alternativos en ``escenarios.py``. Sin él,
#: el escenario de cambio de plan elegiría un plan de otra familia.
CONCEPTOS_CON_CATALOGO_DE_PLANES: frozenset[str] = frozenset(
    {"RENTA_PLAN_MOVIL", "RENTA_HOGAR_INTERNET", "RENTA_TV", "RENTA_MOVISTAR_TOTAL"}
)

#: Escenarios de variación que se inyectan en el último periodo, en orden estable: el
#: reparto por cliente es un *round robin* determinista sobre esta tupla.
ESCENARIOS_ENSAYO: tuple[str, ...] = (
    "CAMBIO_PLAN_MEDIO_CICLO",
    "CORTE_RECONEXION",
    "FIN_DESCUENTO",
    "ALTA_PAQUETE",
    "CUOTA_EQUIPO_FINANCIADO",
    "NOTA_CREDITO",
    "DEUDA_ANTERIOR",
    "ESTABLE",
)

#: Procedencia declarada campo por campo. Es la tabla que se copia en cada cuenta
#: producida y la que hay que leer antes de sacar cualquier conclusión de estos datos.
PROCEDENCIA_CAMPOS: dict[str, str] = {
    # --- del dataset externo, tal cual ---
    "cliente_ref": ORIGEN_DATASET,
    "antiguedad_meses": ORIGEN_DATASET,
    "cargo_mensual": ORIGEN_DATASET,
    "cargo_total": ORIGEN_DATASET,
    "tipo_contrato": ORIGEN_DATASET,
    "metodo_pago": ORIGEN_DATASET,
    "servicios_contratados": ORIGEN_DATASET,
    # --- derivado con una regla escrita en este archivo ---
    "cuenta_id": ORIGEN_DERIVADO,
    "modalidad_renta": ORIGEN_DERIVADO,
    "segmento": ORIGEN_DERIVADO,
    "periodos_sintetizados": ORIGEN_DERIVADO,
    "servicio.concepto_id": ORIGEN_DERIVADO,
    "servicio.tarifa_cent": ORIGEN_DERIVADO,
    "recibo.total_cent": ORIGEN_DERIVADO,
    # --- inventado por el equipo: no está en el dataset ---
    "servicio.plan": ORIGEN_SINTETICO,
    "dia_ciclo": ORIGEN_SINTETICO,
    "recibo.ciclo_inicio": ORIGEN_SINTETICO,
    "recibo.ciclo_fin": ORIGEN_SINTETICO,
    "recibo.fecha_emision": ORIGEN_SINTETICO,
    "recibo.fecha_vencimiento": ORIGEN_SINTETICO,
    "recibo.lineas": ORIGEN_SINTETICO,
    "recibo.lineas.tramos": ORIGEN_SINTETICO,
    "recibo.deuda_anterior_cent": ORIGEN_SINTETICO,
    "linea.IGV": ORIGEN_SINTETICO,
    "descuento": ORIGEN_SINTETICO,
    "escenario": ORIGEN_SINTETICO,
    "movimientos": ORIGEN_SINTETICO,
    "ground_truth": ORIGEN_SINTETICO,
    "beneficios_vigentes": ORIGEN_SINTETICO,
}


# --------------------------------------------------------------------------- #
# Conversión de valores (gemelas de las de movistar_map, adaptadas a este origen)
# --------------------------------------------------------------------------- #
_NO_ALFANUMERICO = re.compile(r"[^a-z0-9]")

#: Valores que significan "no tiene el servicio". Todo lo demás cuenta como que sí lo
#: tiene, incluida la tecnología de acceso (``DSL``, ``Fiber optic``).
_NEGACIONES: frozenset[str] = frozenset(
    {
        "",
        "0",
        "n",
        "no",
        "none",
        "null",
        "nan",
        "f",
        "false",
        "nophoneservice",
        "nointernetservice",
        "notvservice",
        "sinservicio",
        "ninguno",
        "sin",
    }
)


def _clave(valor: Any) -> str:
    """Normaliza un nombre de columna o un valor de texto a su clave de búsqueda.

    ``"Monthly Charges"``, ``"monthly_charges"`` y ``"MonthlyCharges"`` dan la misma
    clave, igual que ``"Month-to-month"`` y ``"month to month"``. Es lo que permite no
    depender del nombre exacto de las columnas de ningún dataset concreto.
    """
    return _NO_ALFANUMERICO.sub("", str(valor).strip().lower())


def columna_canonica(nombre: Any) -> str | None:
    """Traduce un nombre de columna del origen al campo canónico, o ``None``.

    Una columna no reconocida **no se adivina**: se reporta como ignorada en
    :func:`detectar_esquema` y no interviene en ninguna cifra.
    """
    return COLUMN_MAP.get(_clave(nombre))


def _importe(valor: Any) -> Centimos:
    """Convierte un importe decimal del origen a céntimos enteros.

    Raises:
        ValueError: si el texto no es un importe reconocible (el caso típico es la
            celda vacía de un cliente recién dado de alta).
    """
    if valor is None or str(valor).strip() == "":
        raise ValueError("importe vacío")
    return a_centimos(valor if isinstance(valor, str) else str(valor))


def _a_entero(valor: Any, por_defecto: int | None = None) -> int | None:
    """Convierte un entero del origen; ``None`` si viene vacío.

    Acepta ``"12"`` y ``"12.0"``: un export escrito desde una columna de coma flotante
    trae la segunda forma y rechazarla sería rechazar el fichero entero por un detalle
    del exportador.

    Raises:
        ValueError: si el valor no es un número o no es entero.
    """
    if valor is None or str(valor).strip() == "":
        return por_defecto
    texto = str(valor).strip()
    try:
        return int(texto)
    except ValueError:
        pass
    try:
        decimal = Decimal(texto)
    except InvalidOperation as exc:
        raise ValueError(f"no es un número: {valor!r}") from exc
    if decimal != decimal.to_integral_value():
        raise ValueError(f"no es un número entero de meses: {valor!r}")
    return int(decimal)


def _es_afirmativo(valor: Any) -> bool:
    """Interpreta una columna de servicio contratado.

    Cuenta como contratado todo lo que no sea una negación explícita: los datasets de
    fuga escriben ``"Yes"``, pero también ``"DSL"`` o ``"Fiber optic"`` en la columna de
    internet, y ``"No internet service"`` cuando el añadido no aplica.
    """
    return _clave(valor) not in _NEGACIONES


def _registros(df: Any) -> list[Mapping[str, Any]]:
    """Normaliza la entrada a una lista de mapas.

    Acepta un ``DataFrame`` de pandas (vía ``to_dict("records")``), una lista de
    diccionarios o cualquier iterable de mapas. Evita imponer pandas como dependencia,
    exactamente igual que en ``movistar_map``.
    """
    if hasattr(df, "to_dict"):
        return list(df.to_dict("records"))
    if isinstance(df, Mapping):
        return [df]
    if isinstance(df, Iterable):
        return [fila for fila in df if isinstance(fila, Mapping)]
    raise TypeError(f"no se sabe iterar un {type(df).__name__} como tabla de filas")


# --------------------------------------------------------------------------- #
# Detección de esquema
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class InformeEsquema:
    """Qué columnas se reconocieron, cuáles faltan y cuáles se ignoran.

    Es la primera respuesta que hay que dar ante un fichero ajeno: *qué entiendo de lo
    que me has dado*. Se imprime siempre, aunque la ingesta acabe bien.
    """

    columnas: list[str]
    encontradas: dict[str, str]
    faltantes: list[str]
    opcionales_ausentes: list[str]
    ignoradas: list[str]

    @property
    def apto(self) -> bool:
        """Verdadero si están todos los campos obligatorios."""
        return not self.faltantes

    def a_dict(self) -> dict[str, Any]:
        """Proyección serializable del informe."""
        return {
            "columnas_del_fichero": list(self.columnas),
            "reconocidas": dict(self.encontradas),
            "obligatorias_faltantes": list(self.faltantes),
            "opcionales_ausentes": list(self.opcionales_ausentes),
            "ignoradas": list(self.ignoradas),
            "apto": self.apto,
        }

    def a_texto(self) -> str:
        """Informe para la terminal, en español y sin adornos."""
        lineas = [
            f"  columnas del fichero: {len(self.columnas)} · reconocidas: "
            f"{len(self.encontradas)} · ignoradas: {len(self.ignoradas)}"
        ]
        for campo, columna in sorted(self.encontradas.items()):
            lineas.append(f"    {campo:<24} <- {columna}")
        if self.faltantes:
            lineas.append("    FALTAN (obligatorias): " + ", ".join(self.faltantes))
        if self.opcionales_ausentes:
            lineas.append("    ausentes (opcionales): " + ", ".join(self.opcionales_ausentes))
        if self.ignoradas:
            lineas.append("    ignoradas: " + ", ".join(self.ignoradas))
        return "\n".join(lineas)


def detectar_esquema(df: Any) -> InformeEsquema:
    """Reconoce el esquema de un export tabular ajeno.

    No falla nunca: informa. Un dataset externo se examina antes de decidir si se puede
    ingerir, y ese examen es parte de la respuesta al jurado, no un paso interno.

    Args:
        df: ``DataFrame``, lista de diccionarios o iterable de mapas.

    Returns:
        El :class:`InformeEsquema` con lo reconocido, lo que falta y lo que se ignora.
    """
    filas = _registros(df)
    columnas: list[str] = []
    for fila in filas[:1] or []:
        columnas = [str(columna) for columna in fila]

    encontradas: dict[str, str] = {}
    ignoradas: list[str] = []
    for columna in columnas:
        campo = columna_canonica(columna)
        if campo is None:
            ignoradas.append(columna)
        elif campo not in encontradas:
            encontradas[campo] = columna

    faltantes = [campo for campo in CAMPOS_OBLIGATORIOS if campo not in encontradas]
    opcionales = [
        campo
        for campo in CAMPOS_CANONICOS
        if campo not in CAMPOS_OBLIGATORIOS and campo not in encontradas
    ]
    return InformeEsquema(
        columnas=columnas,
        encontradas=encontradas,
        faltantes=faltantes,
        opcionales_ausentes=opcionales,
        ignoradas=ignoradas,
    )


# --------------------------------------------------------------------------- #
# Normalización de filas
# --------------------------------------------------------------------------- #
def normalizar_filas(df: Any) -> list[dict[str, Any]]:
    """Traduce las filas del export externo al vocabulario canónico.

    No convierte tipos ni valida: solo renombra, para que el resto del módulo hable un
    único idioma. La conversión y el rechazo ocurren en :func:`validar`, que es quien
    decide qué fila entra y cuál no.
    """
    informe = detectar_esquema(df)
    normalizadas: list[dict[str, Any]] = []
    for fila in _registros(df):
        destino: dict[str, Any] = {}
        for campo, columna in informe.encontradas.items():
            if columna in fila:
                destino[campo] = fila[columna]
        destino["_columnas"] = dict(informe.encontradas)
        normalizadas.append(destino)
    return normalizadas


# --------------------------------------------------------------------------- #
# Validación
# --------------------------------------------------------------------------- #
def _errores_de_fila(fila: Mapping[str, Any], indice: int) -> list[str]:
    """Errores de una sola fila normalizada, en orden determinista.

    Lista vacía significa que la fila se puede sintetizar. Los mensajes explican **por
    qué** se rechaza y qué habría que hacer, igual que en el hermano: un error de
    ingesta que no dice qué arreglar obliga a leer el código.
    """
    errores: list[str] = []
    referencia = str(fila.get("cliente_ref", "")).strip()
    etiqueta = f"fila {indice}" + (f" (cliente {referencia})" if referencia else "")

    if not referencia:
        return [f"fila {indice}: identificador de cliente vacío"]

    # --- antigüedad: es lo que decide cuántos recibos se pueden sintetizar ---
    antiguedad: int | None = None
    try:
        antiguedad = _a_entero(fila.get("antiguedad_meses"))
    except ValueError as exc:
        errores.append(f"{etiqueta}: antigüedad inválida: {exc}")
    if antiguedad is None and not errores:
        errores.append(f"{etiqueta}: antigüedad ausente")
    elif antiguedad is not None and antiguedad < ANTIGUEDAD_MINIMA_MESES:
        errores.append(
            f"RECHAZADO {etiqueta}: antigüedad de {antiguedad} meses. "
            f"Hacen falta al menos {ANTIGUEDAD_MINIMA_MESES} para sintetizar el "
            "recibo actual y los cinco previos. Un cliente sin historia suficiente no se "
            "sintetiza: se descarta."
        )

    # --- cargo mensual: es el único importe real que entra en el recibo ---
    cargo_mensual: int | None = None
    try:
        cargo_mensual = _importe(fila.get("cargo_mensual"))
    except (ValueError, TypeError) as exc:
        errores.append(f"{etiqueta}: cargo mensual inválido: {exc}")
    if cargo_mensual is not None and cargo_mensual <= 0:
        errores.append(
            f"RECHAZADO {etiqueta}: cargo mensual de {cargo_mensual} céntimos. "
            "Un recibo sin importe no se explica."
        )

    # --- cargo total: no se usa para sintetizar, solo para comprobar coherencia ---
    cargo_total: int | None = None
    if "cargo_total" in fila:
        try:
            cargo_total = _importe(fila.get("cargo_total"))
        except (ValueError, TypeError) as exc:
            errores.append(f"{etiqueta}: cargo total inválido: {exc}")

    if (
        cargo_total is not None
        and cargo_mensual is not None
        and cargo_mensual > 0
        and antiguedad is not None
        and antiguedad > 0
    ):
        esperado = cargo_mensual * antiguedad
        desvio_bp = abs(cargo_total - esperado) * 10_000 // esperado
        if desvio_bp > TOLERANCIA_COHERENCIA_BP:
            errores.append(
                f"RECHAZADO {etiqueta}: el cargo total "
                f"{cargo_total} céntimos no cuadra con {antiguedad} meses a "
                f"{cargo_mensual} céntimos (esperado {esperado}); desviación de "
                f"{desvio_bp} puntos básicos sobre una tolerancia de "
                f"{TOLERANCIA_COHERENCIA_BP}. Un agregado que no cuadra no se sintetiza: "
                "se descarta."
            )

    # --- valores de texto: mapeados o rechazados, nunca adivinados ---
    if "tipo_contrato" in fila:
        bruto = fila.get("tipo_contrato")
        if _clave(bruto) not in CONTRATO_MAP:
            errores.append(
                f"{etiqueta}: tipo de contrato no mapeado en CONTRATO_MAP: {bruto!r}. "
                "Añádalo a este archivo, no al motor."
            )
    if "metodo_pago" in fila:
        bruto = fila.get("metodo_pago")
        if _clave(bruto) not in METODO_PAGO_MAP:
            errores.append(
                f"{etiqueta}: método de pago no mapeado en METODO_PAGO_MAP: {bruto!r}. "
                "Añádalo a este archivo, no al motor."
            )
    return errores


def validar(df: Any) -> list[str]:
    """Valida un export tabular externo y devuelve la lista de errores.

    Lista vacía significa que el fichero es apto para ingesta. Se comprueba, con los
    mismos criterios que el hermano ``movistar_map.validar``:

    1. Que estén todas las columnas obligatorias (si falta alguna, se devuelve **solo**
       ese error: sin cliente o sin importe no hay nada más que comprobar).
    2. Que el identificador de cliente exista y **no se repita** (dos filas para el
       mismo cliente producirían dos historiales distintos para la misma cuenta).
    3. Que la antigüedad alcance para los seis periodos del historial.
    4. Que los importes sean convertibles y el cargo mensual sea positivo.
    5. **Que el cargo total cuadre con el cargo mensual por la antigüedad**, con la
       tolerancia de ``TOLERANCIA_COHERENCIA_BP``. Es el equivalente exacto del cuadre
       de recibo del hermano: un agregado que se contradice a sí mismo no se sintetiza.
    6. Que el tipo de contrato y el método de pago estén mapeados.

    Args:
        df: ``DataFrame`` de pandas, lista de diccionarios o iterable de mapas, con una
            fila **por cliente** (no por línea de recibo: en este origen no las hay).

    Returns:
        Lista de mensajes de error, uno por problema, en orden determinista.
    """
    filas = _registros(df)
    if not filas:
        return ["el export está vacío: no hay ninguna fila que ingerir"]

    informe = detectar_esquema(filas)
    if not informe.apto:
        return [
            "faltan columnas obligatorias en el export externo: "
            + ", ".join(informe.faltantes)
            + ". Añada el alias de su fichero a COLUMN_MAP, en este mismo archivo."
        ]

    errores: list[str] = []
    vistos: set[str] = set()
    for indice, fila in enumerate(normalizar_filas(filas), start=1):
        referencia = str(fila.get("cliente_ref", "")).strip()
        if referencia:
            if referencia in vistos:
                errores.append(
                    f"fila {indice} (cliente {referencia}): identificador duplicado. "
                    "Dos filas para el mismo cliente producirían dos historiales para la "
                    "misma cuenta."
                )
                continue
            vistos.add(referencia)
        errores.extend(_errores_de_fila(fila, indice))
    return errores


# --------------------------------------------------------------------------- #
# Síntesis honesta
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CuentaSintetizada:
    """Una cuenta canónica producida a partir de una fila del dataset externo."""

    cuenta_id: str
    cliente_ref: str
    escenario: str
    historial: HistorialCliente
    procedencia: dict[str, Any]
    desvio_cargo_mensual_cent: Centimos

    @property
    def recibos(self) -> int:
        """Cuántos recibos se sintetizaron para esta cuenta."""
        return len(self.historial.recibos)


def _cuenta_id(cliente_ref: str) -> str:
    """Identificador de cuenta canónico, seudonimizado y con marca de origen.

    Se conserva la referencia del dataset solo en su parte alfanumérica y con el prefijo
    ``EXT-``, de modo que ninguna cuenta externa pueda confundirse con una del dataset
    del proyecto ni colisionar con ella.
    """
    limpio = re.sub(r"[^A-Za-z0-9]", "", str(cliente_ref)).upper()[:16]
    if not limpio:
        limpio = hashlib.sha256(str(cliente_ref).encode()).hexdigest()[:12].upper()
    return f"{PREFIJO_CUENTA_EXTERNA}-{limpio}"


def _base_movimiento_id(cuenta_id: str) -> int:
    """Rango de identificadores de orden reservado a una cuenta externa.

    Se usa un rango disjunto del que emplea ``generar.py`` para el dataset propio
    (``1M–9M``), de modo que mezclar ambos conjuntos nunca produce una colisión de
    ``movimiento_id``, que rompería la trazabilidad línea → orden.
    """
    digest = hashlib.sha256(f"orden-externa|{cuenta_id}".encode()).hexdigest()
    return (20_000_000 + int(digest[:6], 16) % 8_000_000) * 10


def _servicios_de(fila: Mapping[str, Any]) -> tuple[list[str], bool]:
    """Conceptos de renta contratados según el CSV.

    Returns:
        La lista de ``concepto_id`` en orden de reparto y un indicador de si hubo que
        recurrir al servicio por defecto (es decir, si el dato **no** venía del dataset).
    """
    conceptos = [
        SERVICIO_MAP[campo]
        for campo in CAMPOS_SERVICIO
        if campo in fila and _es_afirmativo(fila.get(campo))
    ]
    if conceptos:
        return conceptos, False
    return [CONCEPTO_SERVICIO_POR_DEFECTO], True


def _segmento_de(conceptos: Sequence[str]) -> str:
    """Segmento comercial derivado de los servicios contratados.

    Hay internet fijo o televisión de por medio ⇒ ``HOGAR``; en cualquier otro caso
    ``MASIVO``. Es una regla nuestra, no un dato del origen, y por eso el segmento viaja
    etiquetado como ``DERIVADO_DEL_DATASET``.
    """
    if "RENTA_HOGAR_INTERNET" in conceptos or "RENTA_TV" in conceptos:
        return "HOGAR"
    return "MASIVO"


def _escenario_de(cuenta_id: str, concepto_principal: str) -> str:
    """Escenario de variación asignado a la cuenta, determinista y reproducible.

    El escenario **no está en el dataset**: es la parte inventada, y por eso se asigna
    por reparto determinista sobre ``ESCENARIOS_ENSAYO`` y no por azar. Si el servicio
    principal no tiene catálogo de planes alternativos, se excluye el cambio de plan:
    inventarle al cliente un plan de otra familia sería ruido, no ensayo.
    """
    candidatos = [
        nombre
        for nombre in ESCENARIOS_ENSAYO
        if nombre != "CAMBIO_PLAN_MEDIO_CICLO"
        or concepto_principal in CONCEPTOS_CON_CATALOGO_DE_PLANES
    ]
    digest = hashlib.sha256(f"escenario-externo|{cuenta_id}".encode()).hexdigest()
    return candidatos[int(digest[:8], 16) % len(candidatos)]


def procedencia_de_cuenta(
    *,
    cuenta_id: str,
    fila: Mapping[str, Any],
    conceptos: Sequence[str],
    tarifas_cent: Sequence[Centimos],
    modalidad: ModalidadRenta,
    escenario: str,
    servicio_por_defecto: bool,
    desvio_cent: Centimos,
    periodos: int,
) -> dict[str, Any]:
    """Construye el bloque ``procedencia`` que viaja dentro de cada cuenta producida.

    Es el entregable central de este adaptador: **campo por campo**, de dónde sale cada
    valor. Sin este bloque, los datos que produce el módulo serían indistinguibles de
    datos reales, que es exactamente lo que no queremos.
    """
    columnas = dict(fila.get("_columnas") or {})
    del_dataset: dict[str, Any] = {}
    for campo in (
        "cliente_ref",
        "antiguedad_meses",
        "cargo_mensual",
        "cargo_total",
        "tipo_contrato",
        "metodo_pago",
        *CAMPOS_SERVICIO,
    ):
        if campo in fila:
            del_dataset[campo] = {
                "valor": fila[campo],
                "columna_origen": columnas.get(campo),
                "origen": ORIGEN_DATASET,
            }

    derivado = {
        "cuenta_id": {"valor": cuenta_id, "regla": "EXT- + referencia alfanumérica"},
        "modalidad_renta": {
            "valor": str(modalidad),
            "regla": "tipo de contrato → CONTRATO_MAP (SUPUESTO del equipo)",
        },
        "tarifas_cent": {
            "valor": dict(zip(conceptos, tarifas_cent, strict=True)),
            "regla": (
                "cargo mensual real, desagregado el IGV, repartido por PESOS_SERVICIO (mayor resto)"
            ),
        },
        "periodos_sintetizados": {
            "valor": periodos,
            "regla": "acotado por la antigüedad real del cliente",
        },
        "desvio_frente_al_cargo_mensual_cent": {
            "valor": desvio_cent,
            "regla": "total del recibo base menos el cargo mensual del dataset",
        },
    }

    sintetico = {
        "escenario": escenario,
        "servicio_por_defecto": servicio_por_defecto,
        "campos": sorted(
            campo for campo, origen in PROCEDENCIA_CAMPOS.items() if origen == ORIGEN_SINTETICO
        ),
    }

    return {
        "adaptador": "packages.datagen.mapping.kaggle_map",
        "aviso": AVISO_SINTESIS,
        "apto_para_evaluar_exactitud": False,
        "del_dataset": del_dataset,
        "derivado": derivado,
        "sintetizado": sintetico,
        "tabla_por_campo": dict(PROCEDENCIA_CAMPOS),
    }


def sintetizar_cuenta(
    fila: Mapping[str, Any],
    *,
    seed: int = SEED_POR_DEFECTO,
    periodo_actual: str = PERIODO_ACTUAL_POR_DEFECTO,
    reglas: ConfiguracionReglas | None = None,
) -> CuentaSintetizada:
    """Sintetiza el historial canónico de una cuenta a partir de una fila normalizada.

    El procedimiento, y qué parte es real en cada paso:

    1. **Real.** El cargo mensual del dataset es el total del recibo base. Se le
       desagrega el IGV de ley (``base = total · 10000 / (10000 + igv_bp)``) y la base
       resultante se reparte entre los servicios contratados **que declara el dataset**,
       por mayor resto, de modo que no se pierde ni un céntimo.
    2. **Real.** La antigüedad acota el historial: por debajo de seis meses la fila ya
       se rechazó en :func:`validar`.
    3. **Inventado.** El día de ciclo, las fechas, el desglose por concepto, el IGV
       línea a línea, el escenario de variación del último periodo y los movimientos de
       CRM los produce ``packages.datagen``, exactamente el mismo motor que genera el
       dataset propio. Ahí está la reutilización, y ahí está la frontera de la honestidad.

    Args:
        fila: fila **ya normalizada** por :func:`normalizar_filas` y ya validada.
        seed: semilla global; la de cada cuenta se deriva por sha256, como en el
            generador propio, para que regenerar una sola cuenta dé el mismo resultado.
        periodo_actual: periodo M0 en formato ``YYYY-MM``.
        reglas: configuración de reglas; si no se pasa, se carga la vigente.

    Returns:
        La :class:`CuentaSintetizada`, con su historial y su bloque de procedencia.

    Raises:
        ValueError: si la fila no trae cargo mensual convertible o positivo.
        ErrorConciliacion: si el *ground truth* del escenario no cuadra (aborta, igual
            que el generador propio: antes eso que publicar datos que mienten).
    """
    reglas = reglas or cargar_reglas()
    referencia = str(fila.get("cliente_ref", "")).strip()
    cuenta_id = _cuenta_id(referencia)
    rng = Random(semilla_cliente(seed, cuenta_id))

    cargo_mensual_cent = _importe(fila.get("cargo_mensual"))
    if cargo_mensual_cent <= 0:
        raise ValueError(f"cargo mensual no positivo para {referencia!r}: {cargo_mensual_cent}")
    antiguedad = _a_entero(fila.get("antiguedad_meses"), ANTIGUEDAD_MINIMA_MESES) or 0

    conceptos, servicio_por_defecto = _servicios_de(fila)
    modalidad = ModalidadRenta.VENCIDA
    if "tipo_contrato" in fila:
        entrada = CONTRATO_MAP.get(_clave(fila.get("tipo_contrato")))
        if entrada is not None:
            modalidad = entrada[1]

    escenario_nombre = _escenario_de(cuenta_id, conceptos[0])

    # El cargo mensual del dataset es el total CON impuesto: se desagrega el IGV para
    # obtener la base afecta que hay que repartir entre los servicios.
    base_objetivo = redondear_banca(cargo_mensual_cent * 10_000, 10_000 + reglas.politica.igv_bp)

    # El escenario FIN_DESCUENTO exige un descuento vigente en los periodos previos. Se
    # fija aquí (y se fuerza después, para que `preparar` no lo sobrescriba) y se suma a
    # las tarifas, de modo que el total del recibo base sigue siendo el cargo mensual real.
    descuento: DescuentoBase | None = None
    base_tarifas = base_objetivo
    if escenario_nombre == "FIN_DESCUENTO":
        monto_descuento = max(100, redondear_banca(base_objetivo * PORCENTAJE_DESCUENTO_BP, 10_000))
        promocion_id, nombre_promocion = rng.choice(PROMOCIONES)
        descuento = DescuentoBase(
            promocion_id=promocion_id,
            nombre=nombre_promocion,
            monto_cent=monto_descuento,
            meses_vigencia=min(antiguedad, 12) or 12,
        )
        base_tarifas = base_objetivo + monto_descuento

    pesos = [PESOS_SERVICIO.get(concepto, 10) for concepto in conceptos]
    tarifas = repartir_mayor_resto(base_tarifas, pesos)

    servicios = [
        ServicioBase(
            concepto_id=concepto,
            nombre_comercial=_nombre_comercial(concepto, reglas),
            plan=NOMBRE_PLAN_SINTETIZADO.get(concepto, "Plan Externo"),
            tarifa_cent=tarifa,
            servicio_id=f"{cuenta_id}-{_sufijo_servicio(concepto)}",
        )
        for concepto, tarifa in zip(conceptos, tarifas, strict=True)
    ]

    perfil = PerfilCliente(
        cuenta_id=cuenta_id,
        seed=semilla_cliente(0, cuenta_id),
        segmento=_segmento_de(conceptos),
        modalidad_renta=modalidad,
        dia_ciclo=1 + int(hashlib.sha256(cuenta_id.encode()).hexdigest()[:4], 16) % 28,
        servicios=servicios,
        consumos={},
        descuento=descuento,
        # El dataset externo no dice nada de los beneficios del cliente y no se inventan:
        # el "efecto efervescente" se queda mudo antes que hablar sin dato detrás.
        beneficios=[],
        base_movimiento_id=_base_movimiento_id(cuenta_id),
    )

    historial = generar_cliente(
        perfil=perfil,
        escenarios=[obtener_escenario(escenario_nombre)],
        rng=rng,
        periodo_actual=periodo_actual,
        reglas=reglas,
        atributos_forzados={"descuento": descuento, "financiamiento": None},
    )

    # Los cinco periodos previos son el recibo base: su total debe ser el cargo mensual
    # real del dataset, salvo el céntimo que se pierde al desagregar el IGV.
    desvio = historial.recibos[0].total_cent - cargo_mensual_cent

    procedencia = procedencia_de_cuenta(
        cuenta_id=cuenta_id,
        fila=fila,
        conceptos=conceptos,
        tarifas_cent=tarifas,
        modalidad=modalidad,
        escenario=escenario_nombre,
        servicio_por_defecto=servicio_por_defecto,
        desvio_cent=desvio,
        periodos=len(historial.recibos),
    )

    return CuentaSintetizada(
        cuenta_id=cuenta_id,
        cliente_ref=referencia,
        escenario=escenario_nombre,
        historial=historial,
        procedencia=procedencia,
        desvio_cargo_mensual_cent=desvio,
    )


def _nombre_comercial(concepto_id: str, reglas: ConfiguracionReglas) -> str:
    """Nombre comercial del concepto según el catálogo canónico."""
    concepto = reglas.concepto(concepto_id)
    return concepto.nombre_comercial if concepto is not None else concepto_id


def _sufijo_servicio(concepto_id: str) -> str:
    """Sufijo del ``servicio_id`` según el concepto, como en el dataset propio."""
    return {
        "RENTA_HOGAR_INTERNET": "BAF",
        "RENTA_LINEA_FIJA": "FIJA",
        "RENTA_TV": "TV",
        "RENTA_PLAN_MOVIL": "MOV",
    }.get(concepto_id, "SRV")


# --------------------------------------------------------------------------- #
# Ingesta completa
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ResumenIngesta:
    """Cifras de control de una ingesta completa, listas para imprimir o serializar."""

    fuente: str
    seed: int
    periodo_actual: str
    rules_version: str
    informe: InformeEsquema
    filas_leidas: int
    cuentas: list[CuentaSintetizada] = field(default_factory=list)
    rechazos: list[str] = field(default_factory=list)

    @property
    def aceptadas(self) -> int:
        """Cuentas canónicas producidas."""
        return len(self.cuentas)

    @property
    def rechazadas(self) -> int:
        """Filas que no se pudieron o no se debieron sintetizar."""
        return self.filas_leidas - self.aceptadas

    @property
    def recibos(self) -> int:
        """Recibos sintetizados en total."""
        return sum(cuenta.recibos for cuenta in self.cuentas)

    @property
    def ordenes(self) -> int:
        """Órdenes de CRM sintetizadas en total."""
        return sum(len(cuenta.historial.movimientos) for cuenta in self.cuentas)

    @property
    def filas_ground_truth(self) -> int:
        """Filas de ``gt_causa_delta`` producidas por los escenarios inyectados."""
        return sum(len(cuenta.historial.ground_truth) for cuenta in self.cuentas)

    @property
    def desvio_maximo_cent(self) -> Centimos:
        """Mayor desviación, en valor absoluto, frente al cargo mensual real."""
        return max((abs(cuenta.desvio_cargo_mensual_cent) for cuenta in self.cuentas), default=0)

    @property
    def por_escenario(self) -> dict[str, int]:
        """Cuántas cuentas recibió cada escenario sintetizado."""
        conteo: dict[str, int] = {}
        for cuenta in self.cuentas:
            conteo[cuenta.escenario] = conteo.get(cuenta.escenario, 0) + 1
        return dict(sorted(conteo.items()))

    def a_dict(self) -> dict[str, Any]:
        """Proyección serializable del resumen."""
        return {
            "adaptador": "packages.datagen.mapping.kaggle_map",
            "aviso": AVISO_SINTESIS,
            "apto_para_evaluar_exactitud": False,
            "fuente": self.fuente,
            "seed": self.seed,
            "periodo_actual": self.periodo_actual,
            "rules_version": self.rules_version,
            "esquema": self.informe.a_dict(),
            "filas_leidas": self.filas_leidas,
            "cuentas_producidas": self.aceptadas,
            "filas_rechazadas": self.rechazadas,
            "rechazos": list(self.rechazos),
            "recibos": self.recibos,
            "ordenes": self.ordenes,
            "filas_ground_truth": self.filas_ground_truth,
            "desvio_maximo_cent": self.desvio_maximo_cent,
            "por_escenario": self.por_escenario,
            "cuentas": [
                {
                    "cuenta_id": cuenta.cuenta_id,
                    "cliente_ref": cuenta.cliente_ref,
                    "escenario": cuenta.escenario,
                    "recibos": cuenta.recibos,
                    "desvio_cent": cuenta.desvio_cargo_mensual_cent,
                }
                for cuenta in self.cuentas
            ],
            "procedencia_por_campo": dict(PROCEDENCIA_CAMPOS),
        }

    def a_texto(self) -> str:
        """Informe para la terminal, en español y sin adornos."""
        lineas = [
            f"Ingesta de dataset externo tabular — {self.fuente}",
            f"  semilla {self.seed} · periodo actual {self.periodo_actual} · "
            f"reglas {self.rules_version}",
            "  ESQUEMA DETECTADO",
            self.informe.a_texto(),
            f"  filas leídas {self.filas_leidas} · aceptadas {self.aceptadas} · "
            f"rechazadas {self.rechazadas}",
        ]
        if self.rechazos:
            lineas.append("  RECHAZOS (con su motivo):")
            lineas.extend(f"    {motivo}" for motivo in self.rechazos)
        lineas.extend(
            [
                f"  cuentas canónicas producidas: {self.aceptadas} · {self.recibos} recibos · "
                f"{self.ordenes} órdenes · {self.filas_ground_truth} filas de ground truth",
                f"  desviación máxima frente al cargo mensual real: "
                f"{self.desvio_maximo_cent} "
                f"céntimo{'' if abs(self.desvio_maximo_cent) == 1 else 's'} "
                f"({formatear_soles(self.desvio_maximo_cent)})",
                "  escenarios sintetizados:",
            ]
        )
        for nombre, cuantos in self.por_escenario.items():
            lineas.append(f"    {nombre:<26} {cuantos}")
        lineas.extend(
            [
                "  PROCEDENCIA",
                "    DATASET_EXTERNO ......... cargo mensual, antigüedad, contrato, "
                "método de pago y servicios contratados",
                "    DERIVADO_DEL_DATASET .... tarifa por servicio, modalidad de renta, "
                "segmento y profundidad del historial",
                "    SINTETIZADO_POR_EL_EQUIPO desglose por concepto, fechas de ciclo, IGV, "
                "escenario de variación y movimientos de CRM",
                f"  AVISO: {AVISO_SINTESIS}",
            ]
        )
        return "\n".join(lineas)


def ingerir(
    df: Any,
    *,
    seed: int = SEED_POR_DEFECTO,
    periodo_actual: str = PERIODO_ACTUAL_POR_DEFECTO,
    fuente: str = "(en memoria)",
    reglas: ConfiguracionReglas | None = None,
    limite: int | None = None,
) -> ResumenIngesta:
    """Ingiere un export tabular externo y sintetiza las cuentas que superen la validación.

    Fila a fila: se valida, y si no pasa se rechaza **con el motivo escrito**; si pasa,
    se sintetiza. Una fila que reviente durante la síntesis (ground truth que no cuadra,
    concepto fuera de catálogo) se rechaza igual, con su excepción como motivo: nunca se
    escribe una cuenta a medias.

    Args:
        df: ``DataFrame``, lista de diccionarios o iterable de mapas.
        seed: semilla global de la síntesis.
        periodo_actual: periodo M0 en formato ``YYYY-MM``.
        fuente: etiqueta de origen para el informe (normalmente la ruta del CSV).
        reglas: configuración de reglas; si no se pasa, se carga la vigente.
        limite: número máximo de filas a ingerir (útil para una prueba rápida).

    Returns:
        El :class:`ResumenIngesta` con las cuentas producidas y los rechazos.
    """
    reglas = reglas or cargar_reglas()
    filas_brutas = _registros(df)
    if limite is not None:
        filas_brutas = filas_brutas[: max(0, limite)]
    informe = detectar_esquema(filas_brutas)

    resumen = ResumenIngesta(
        fuente=fuente,
        seed=seed,
        periodo_actual=periodo_actual,
        rules_version=reglas.rules_version,
        informe=informe,
        filas_leidas=len(filas_brutas),
    )
    if not filas_brutas:
        resumen.rechazos.append("el export está vacío: no hay ninguna fila que ingerir")
        return resumen
    if not informe.apto:
        resumen.rechazos.append(
            "faltan columnas obligatorias en el export externo: "
            + ", ".join(informe.faltantes)
            + ". Añada el alias de su fichero a COLUMN_MAP, en este mismo archivo."
        )
        return resumen

    vistos: set[str] = set()
    for indice, fila in enumerate(normalizar_filas(filas_brutas), start=1):
        referencia = str(fila.get("cliente_ref", "")).strip()
        if referencia and referencia in vistos:
            resumen.rechazos.append(
                f"fila {indice} (cliente {referencia}): identificador duplicado. Dos filas "
                "para el mismo cliente producirían dos historiales para la misma cuenta."
            )
            continue
        errores = _errores_de_fila(fila, indice)
        if errores:
            resumen.rechazos.extend(errores)
            continue
        if referencia:
            vistos.add(referencia)
        try:
            resumen.cuentas.append(
                sintetizar_cuenta(fila, seed=seed, periodo_actual=periodo_actual, reglas=reglas)
            )
        except (ErrorConciliacion, ValueError, KeyError) as error:
            resumen.rechazos.append(
                f"fila {indice} (cliente {referencia}): la síntesis falló y la cuenta no "
                f"se escribe: {error}"
            )
    return resumen


# --------------------------------------------------------------------------- #
# Lectura y escritura
# --------------------------------------------------------------------------- #
def leer_csv(ruta: str | Path) -> list[dict[str, Any]]:
    """Lee un CSV externo sin imponer pandas.

    Devuelve la tabla tal cual, con sus nombres de columna originales: la traducción es
    responsabilidad de este módulo y ocurre después, no al leer.

    Raises:
        FileNotFoundError: si el fichero no existe.
    """
    camino = Path(ruta)
    if not camino.is_file():
        raise FileNotFoundError(f"no existe el CSV externo: {camino}")
    with camino.open(encoding="utf-8-sig", newline="") as fichero:
        return [dict(fila) for fila in csv.DictReader(fichero)]


def _escribir_json(ruta: Path, datos: Any) -> Path:
    """Vuelca datos como JSON legible en UTF-8."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ruta


def escribir_salida(resumen: ResumenIngesta, salida: str | Path) -> dict[str, str]:
    """Escribe las cuentas producidas con la **misma forma** que el dataset propio.

    ``bills/{cuenta_id}.json`` con la forma de BrainyBill y ``ordenes.csv`` con las
    columnas nativas de Amdocs (las escribe ``movistar_map``, no este módulo: el ACL de
    salida sigue siendo el del sistema real). El resultado se puede servir apuntando
    ``DATOS_SINTETICOS`` a este directorio, sin tocar una línea del motor — que es
    justamente lo que este ensayo quiere demostrar.

    Además escribe ``procedencia.json`` y ``resumen.json``: los datos no salen de aquí
    sin su etiqueta de origen.

    Returns:
        Diccionario ``nombre lógico -> ruta escrita``.
    """
    destino = Path(salida)
    destino.mkdir(parents=True, exist_ok=True)
    historiales = [cuenta.historial for cuenta in resumen.cuentas]

    directorio_bills = destino / "bills"
    directorio_bills.mkdir(parents=True, exist_ok=True)
    for cuenta in resumen.cuentas:
        documento = historial_a_documento(cuenta.historial, resumen.seed)
        documento["origen"] = "dataset externo tabular (adaptador de ensayo)"
        documento["procedencia"] = cuenta.procedencia
        _escribir_json(directorio_bills / f"{cuenta.cuenta_id}.json", documento)

    escribir_ordenes(destino, historiales)
    escribir_ground_truth(destino, historiales)
    _escribir_json(
        destino / "procedencia.json",
        {
            "adaptador": "packages.datagen.mapping.kaggle_map",
            "aviso": AVISO_SINTESIS,
            "apto_para_evaluar_exactitud": False,
            "fuente": resumen.fuente,
            "por_campo": dict(PROCEDENCIA_CAMPOS),
            "cuentas": {cuenta.cuenta_id: cuenta.procedencia for cuenta in resumen.cuentas},
        },
    )
    _escribir_json(destino / "resumen.json", resumen.a_dict())
    return {
        "bills": str(directorio_bills),
        "ordenes": str(destino / "ordenes.csv"),
        "ground_truth": str(destino / "ground_truth.csv"),
        "procedencia": str(destino / "procedencia.json"),
        "resumen": str(destino / "resumen.json"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def construir_argumentos() -> argparse.ArgumentParser:
    """Analizador de argumentos de la línea de comandos."""
    analizador = argparse.ArgumentParser(
        prog="recibo-kaggle-map",
        description=(
            "Adaptador de ENSAYO: ingiere un CSV tabular de cliente de telecomunicaciones "
            "(esquema típico de los datasets públicos de fuga) y sintetiza cuentas "
            "canónicas para ejercitar el anti-corruption layer. Los recibos resultantes "
            "son PARCIALMENTE SINTÉTICOS y no sirven para medir la exactitud del motor."
        ),
    )
    analizador.add_argument(
        "--csv",
        type=str,
        required=True,
        help="ruta del CSV externo a ingerir",
    )
    analizador.add_argument(
        "--salida",
        type=str,
        default=None,
        help="directorio de destino (por defecto data/externo dentro del proyecto)",
    )
    analizador.add_argument(
        "--seed",
        type=int,
        default=SEED_POR_DEFECTO,
        help=f"semilla de la síntesis (por defecto {SEED_POR_DEFECTO})",
    )
    analizador.add_argument(
        "--periodo-actual",
        type=str,
        default=PERIODO_ACTUAL_POR_DEFECTO,
        dest="periodo_actual",
        help=f"periodo M0 en formato YYYY-MM (por defecto {PERIODO_ACTUAL_POR_DEFECTO})",
    )
    analizador.add_argument(
        "--limite",
        type=int,
        default=None,
        help="ingiere como mucho este número de filas",
    )
    analizador.add_argument(
        "--solo-validar",
        action="store_true",
        dest="solo_validar",
        help="valida el fichero y muestra el esquema detectado, sin sintetizar nada",
    )
    analizador.add_argument(
        "--no-escribir",
        action="store_true",
        dest="no_escribir",
        help="sintetiza e informa, pero no toca el disco",
    )
    return analizador


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada de ``python -m packages.datagen.mapping.kaggle_map``.

    Códigos de salida: 0 si se produjo al menos una cuenta (o si ``--solo-validar`` no
    encontró errores), 2 si no se pudo ingerir ninguna fila, 3 ante un error de
    configuración (reglas o catálogo) y 4 si el CSV no existe.
    """
    argumentos = construir_argumentos().parse_args(argv)
    try:
        tabla = leer_csv(argumentos.csv)
    except FileNotFoundError as error:
        print(f"ABORTADO: {error}", file=sys.stderr)
        return 4

    if argumentos.solo_validar:
        informe = detectar_esquema(tabla)
        errores = validar(tabla)
        print(f"Validación de {argumentos.csv}")
        print("  ESQUEMA DETECTADO")
        print(informe.a_texto())
        print(f"  filas leídas: {len(tabla)} · problemas encontrados: {len(errores)}")
        for error in errores:
            print(f"    {error}")
        print(f"  AVISO: {AVISO_SINTESIS}")
        return 0 if not errores else 2

    try:
        resumen = ingerir(
            tabla,
            seed=argumentos.seed,
            periodo_actual=argumentos.periodo_actual,
            fuente=str(argumentos.csv),
            limite=argumentos.limite,
        )
    except (FileNotFoundError, KeyError) as error:
        print(f"ABORTADO por error de configuración: {error}", file=sys.stderr)
        return 3

    print(resumen.a_texto())
    if not resumen.cuentas:
        print("No se produjo ninguna cuenta canónica: no se escribe nada.", file=sys.stderr)
        return 2

    if not argumentos.no_escribir:
        salida = (
            Path(argumentos.salida) if argumentos.salida else raiz_proyecto() / "data" / "externo"
        )
        rutas = escribir_salida(resumen, salida)
        print("  escrito en:")
        for nombre, ruta in rutas.items():
            print(f"    {nombre:<14} {ruta}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
