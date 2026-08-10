"""Aritmética monetaria del proyecto: **todo monto es un ``int`` en céntimos**.

Regla innegociable nº 1 de la especificación: en la lógica de negocio no existe
``float`` ni ``Decimal`` para representar dinero. ``Decimal`` y ``Fraction`` solo
aparecen *dentro* de este módulo como aritmética intermedia exacta, y siempre se
sale de aquí con un ``int``.

Convenciones:

* ``Centimos`` es un alias de ``int``. 1 sol = 100 céntimos. Nunca hay fracciones
  de céntimo en un campo persistido.
* Un monto negativo es un abono a favor del cliente (nota de crédito, descuento,
  ajuste por días de suspensión).
* El redondeo por defecto es **bancario** (mitad al par, ``ROUND_HALF_EVEN``):
  es el que no introduce sesgo sistemático al prorratear miles de recibos.
* Cuando hay que repartir un total entre varias líneas se usa **mayor resto**,
  que garantiza ``suma(partes) == total`` de forma exacta.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from typing import TypeAlias

__all__ = [
    "CENTIMOS_POR_SOL",
    "SIMBOLO_MONEDA",
    "Centimos",
    "a_centimos",
    "aplicar_porcentaje",
    "formatear_numero",
    "formatear_soles",
    "prorratear",
    "redondear_banca",
    "repartir_mayor_resto",
    "variantes_monto",
]

#: Alias de dominio. Es un ``int`` a todos los efectos (Pydantic lo trata como ``int``).
Centimos: TypeAlias = int

CENTIMOS_POR_SOL = 100
SIMBOLO_MONEDA = "S/"

#: Texto que se descarta al parsear un importe escrito ("S/", "S/.", "PEN", espacios raros).
_RUIDO = re.compile(r"(?:S/\.?|PEN|SOLES?|\s)", re.IGNORECASE)
_SOLO_DECIMAL = re.compile(r"^\d+(?:\.\d+)?$")
_TipoNumerico: TypeAlias = "str | int | float | Decimal"


# --------------------------------------------------------------------------- #
# Parseo y formato
# --------------------------------------------------------------------------- #
def a_centimos(texto_o_decimal: _TipoNumerico) -> Centimos:
    """Convierte un importe expresado **en soles** a céntimos enteros.

    El argumento se interpreta siempre como soles, venga como texto o como número:
    ``a_centimos("S/ 1,234.50") == 123450`` y ``a_centimos(120) == 12000``.

    Acepta las cuatro escrituras que se ven en recibos y en texto de clientes:

    * ``"1,234.50"`` (coma de millar, punto decimal — formato peruano de imprenta)
    * ``"1.234,50"`` (punto de millar, coma decimal)
    * ``"1234.50"`` y ``"1234,50"``
    * con o sin prefijo ``S/``, ``S/.`` o ``PEN``; negativos con ``-`` o entre paréntesis.

    Los ``float`` se convierten vía ``Decimal(str(x))`` para no arrastrar el error
    binario; se admiten solo como comodidad en el borde de ingesta.

    Raises:
        ValueError: si el texto no es un importe reconocible.
        TypeError: si el tipo no es texto ni número.
    """
    if isinstance(texto_o_decimal, bool):  # bool es subclase de int: se rechaza explícito
        raise TypeError("un booleano no es un importe")
    if isinstance(texto_o_decimal, Decimal):
        decimal = texto_o_decimal
    elif isinstance(texto_o_decimal, int):
        decimal = Decimal(texto_o_decimal)
    elif isinstance(texto_o_decimal, float):
        if not math.isfinite(texto_o_decimal):
            raise ValueError(f"importe no finito: {texto_o_decimal!r}")
        decimal = Decimal(str(texto_o_decimal))
    elif isinstance(texto_o_decimal, str):
        decimal = _parsear_texto(texto_o_decimal)
    else:
        raise TypeError(f"tipo no admitido para un importe: {type(texto_o_decimal).__name__}")

    if not decimal.is_finite():
        raise ValueError(f"importe no finito: {texto_o_decimal!r}")
    return int((decimal * CENTIMOS_POR_SOL).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _parsear_texto(texto: str) -> Decimal:
    """Normaliza un importe escrito y lo devuelve como ``Decimal`` de soles."""
    bruto = texto.strip()
    if not bruto:
        raise ValueError("importe vacío")

    negativo = False
    if bruto.startswith("(") and bruto.endswith(")"):
        negativo = True
        bruto = bruto[1:-1]

    limpio = _RUIDO.sub("", bruto).replace("−", "-").replace("–", "-")
    if limpio.startswith("-"):
        negativo = not negativo
        limpio = limpio[1:]
    elif limpio.startswith("+"):
        limpio = limpio[1:]
    if not limpio:
        raise ValueError(f"importe no reconocible: {texto!r}")

    hay_coma, hay_punto = "," in limpio, "." in limpio
    if hay_coma and hay_punto:
        # El separador decimal es el que aparece más a la derecha.
        decimal_sep = "," if limpio.rindex(",") > limpio.rindex(".") else "."
        millar_sep = "." if decimal_sep == "," else ","
        limpio = limpio.replace(millar_sep, "").replace(decimal_sep, ".")
    elif hay_coma or hay_punto:
        sep = "," if hay_coma else "."
        partes = limpio.split(sep)
        # Varios separadores => todos son de millar. Uno solo: es decimal salvo que
        # deje exactamente 3 dígitos a la derecha ("1,234" es mil doscientos treinta y cuatro).
        if len(partes) > 2 or len(partes[-1]) == 3:
            limpio = limpio.replace(sep, "")
        else:
            limpio = limpio.replace(sep, ".")

    if not _SOLO_DECIMAL.fullmatch(limpio):
        raise ValueError(f"importe no reconocible: {texto!r}")
    try:
        valor = Decimal(limpio)
    except InvalidOperation as exc:  # pragma: no cover - _SOLO_DECIMAL ya lo garantiza
        raise ValueError(f"importe no reconocible: {texto!r}") from exc
    return -valor if negativo else valor


def formatear_numero(centimos: Centimos) -> str:
    """Devuelve el importe sin símbolo, en formato peruano: ``123450 -> "1,234.50"``.

    El signo se antepone al número (``-1,234.50``).
    """
    signo = "-" if centimos < 0 else ""
    entero, resto = divmod(abs(int(centimos)), CENTIMOS_POR_SOL)
    return f"{signo}{entero:,}.{resto:02d}"


def formatear_soles(centimos: Centimos) -> str:
    """Formato canónico de importe para el cliente: ``123450 -> "S/ 1,234.50"``.

    Es la ÚNICA representación que se emite hacia el usuario. El signo se coloca
    delante del símbolo (``-S/ 1,234.50``), como en los recibos peruanos.
    """
    signo = "-" if centimos < 0 else ""
    return f"{signo}{SIMBOLO_MONEDA} {formatear_numero(abs(int(centimos)))}"


def variantes_monto(centimos: Centimos) -> set[str]:
    """Todas las escrituras plausibles del **valor absoluto** de un importe.

    Sirve al verificador numérico para el emparejamiento literal sobre el texto
    generado (``"S/ 124.90"``, ``"124,90"``, ``"124.90"``…). El emparejamiento
    canónico se hace por token normalizado (ver ``esquemas.factset.token_monto``);
    esta función cubre el caso de comparación por cadena.
    """
    valor = abs(int(centimos))
    entero, resto = divmod(valor, CENTIMOS_POR_SOL)
    con_millar_punto = f"{entero:,}".replace(",", ".")
    cuerpos = {
        f"{entero:,}.{resto:02d}",  # 1,234.50
        f"{con_millar_punto},{resto:02d}",  # 1.234,50
        f"{entero}.{resto:02d}",  # 1234.50
        f"{entero},{resto:02d}",  # 1234,50
    }
    variantes: set[str] = set()
    for cuerpo in cuerpos:
        variantes.add(cuerpo)
        variantes.add(f"{SIMBOLO_MONEDA} {cuerpo}")
        variantes.add(f"{SIMBOLO_MONEDA}{cuerpo}")
        variantes.add(f"{SIMBOLO_MONEDA}. {cuerpo}")
    return variantes


# --------------------------------------------------------------------------- #
# Aritmética exacta
# --------------------------------------------------------------------------- #
def redondear_banca(numerador: int, denominador: int) -> int:
    """Divide dos enteros y redondea al par más cercano (``ROUND_HALF_EVEN``).

    Primitiva de redondeo del proyecto: se implementa solo con enteros, así que
    no hay error de coma flotante posible. ``redondear_banca(5, 2) == 2`` y
    ``redondear_banca(7, 2) == 4`` (ambos empates van al entero par).

    Raises:
        ZeroDivisionError: si ``denominador`` es 0.
    """
    if denominador == 0:
        raise ZeroDivisionError("denominador cero en redondear_banca")
    if denominador < 0:
        numerador, denominador = -numerador, -denominador
    cociente, resto = divmod(numerador, denominador)  # resto siempre en [0, denominador)
    doble = 2 * resto
    if doble > denominador or (doble == denominador and cociente % 2 != 0):
        cociente += 1
    return cociente


def prorratear(monto_cent: Centimos, dias: int, dias_ciclo: int) -> Centimos:
    """Parte proporcional de un monto mensual por ``dias`` de un ciclo de ``dias_ciclo``.

    ``monto_cent * dias / dias_ciclo`` con redondeo bancario y aritmética entera.
    Es la fórmula ``P_j · len_j / D`` del modelo de tramos. Funciona igual con
    montos negativos (ajustes y descuentos).

    Raises:
        ValueError: si ``dias`` es negativo o ``dias_ciclo`` no es positivo.
    """
    if dias < 0:
        raise ValueError(f"días negativos en prorrateo: {dias}")
    if dias_ciclo <= 0:
        raise ValueError(f"días de ciclo inválidos: {dias_ciclo}")
    return redondear_banca(int(monto_cent) * int(dias), int(dias_ciclo))


def aplicar_porcentaje(monto_cent: Centimos, porcentaje_bp: int) -> Centimos:
    """Aplica un porcentaje expresado en **puntos básicos** (1 % = 100 bp).

    El IGV peruano del 18 % es ``porcentaje_bp=1800``. Redondeo bancario, todo entero:
    ``aplicar_porcentaje(10000, 1800) == 1800``.
    """
    return redondear_banca(int(monto_cent) * int(porcentaje_bp), 10_000)


def repartir_mayor_resto(
    total_cent: Centimos,
    pesos: Sequence[float | int | Fraction | Decimal],
) -> list[Centimos]:
    """Reparte ``total_cent`` proporcionalmente a ``pesos`` sin perder ni un céntimo.

    Algoritmo de mayor resto: ``c_i = floor(x_i)``; el residuo ``r = T - Σ c_i`` se
    entrega de a un céntimo a los ``r`` elementos con mayor parte fraccionaria
    (empates resueltos por índice ascendente, para que el resultado sea determinista).

    **Invariante garantizado:** ``sum(repartir_mayor_resto(T, w)) == T`` para cualquier
    ``T`` (positivo, cero o negativo) y cualquier vector de pesos no negativos con suma
    positiva. La aritmética interna usa ``Fraction``, de modo que no hay error de
    redondeo acumulado.

    Raises:
        ValueError: si hay pesos negativos, si la suma de pesos es 0, o si la lista de
            pesos está vacía con un total distinto de cero.
    """
    if len(pesos) == 0:
        if total_cent != 0:
            raise ValueError("no se puede repartir un total distinto de cero sin pesos")
        return []

    fracciones: list[Fraction] = []
    for peso in pesos:
        fraccion = Fraction(str(peso)) if isinstance(peso, Decimal) else Fraction(peso)
        if fraccion < 0:
            raise ValueError(f"peso negativo en el reparto: {peso!r}")
        fracciones.append(fraccion)

    suma = sum(fracciones, Fraction(0))
    if suma == 0:
        raise ValueError("la suma de pesos debe ser mayor que cero")

    total = Fraction(int(total_cent))
    exactos = [total * fraccion / suma for fraccion in fracciones]
    partes = [math.floor(valor) for valor in exactos]
    residuo = int(total_cent) - sum(partes)  # siempre en [0, len(pesos))

    orden = sorted(range(len(fracciones)), key=lambda i: (-(exactos[i] - partes[i]), i))
    for indice in orden[:residuo]:
        partes[indice] += 1
    return partes
