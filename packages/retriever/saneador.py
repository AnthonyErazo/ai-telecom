"""Saneador de cifras del corpus recuperado. **Obligatorio** antes del prompt.

Por qué existe
--------------
Regla innegociable nº 4: ``ALLOWED`` del verificador se construye SOLO desde el
``FactSet``. Ninguna cifra de un documento recuperado puede sobrevivir al texto final.

El riesgo es concreto y no hipotético: si una FAQ del corpus dice *"por ejemplo, un
paquete de datos cuesta S/ 49.90"*, ese número **no es de este cliente**. Si llega al
prompt, el modelo puede copiarlo; si lo copia, el verificador lo marcará como no
anclado y la respuesta caerá a plantilla (mejor caso) o, si alguien relajara el
verificador, se convertiría en una invención financiera (peor caso). La defensa
correcta no es confiar en el modelo ni en el verificador: es **que el número nunca
entre**. Este módulo lo garantiza en origen.

Garantía que ofrece :func:`sanear`
----------------------------------
El texto devuelto **no contiene ni un solo dígito**. No es una heurística: la última
regla de sustitución captura cualquier resto numérico. Por construcción, la
intersección entre las cifras del corpus y ``FactSet.tokens_permitidos()`` es vacía,
porque en el corpus saneado ya no hay cifras que extraer.

Lo sustituido se devuelve para auditoría: el evento ``RETRIEVE`` de la cadena registra
qué se retiró de cada documento, de modo que la neutralización es demostrable y no un
acto de fe.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from pydantic import BaseModel, ConfigDict

__all__ = [
    "MARCADORES",
    "MARCADOR_CANTIDAD",
    "MARCADOR_CUOTA",
    "MARCADOR_DIAS",
    "MARCADOR_FECHA",
    "MARCADOR_MONTO",
    "MARCADOR_NUMERO",
    "MARCADOR_PORCENTAJE",
    "MARCADOR_RANGO_FECHAS",
    "ResultadoSaneado",
    "Sustitucion",
    "contiene_cifras",
    "sanear",
    "sanear_detallado",
]

# --------------------------------------------------------------------------- #
# Marcadores genéricos (español de Perú, trato de usted, sin dígitos)
# --------------------------------------------------------------------------- #
MARCADOR_MONTO: Final = "«un monto»"
MARCADOR_FECHA: Final = "«una fecha»"
MARCADOR_RANGO_FECHAS: Final = "«un rango de fechas»"
MARCADOR_PORCENTAJE: Final = "«un porcentaje»"
MARCADOR_DIAS: Final = "«una cantidad de días»"
MARCADOR_CUOTA: Final = "«una cuota»"
MARCADOR_CANTIDAD: Final = "«una cantidad»"
MARCADOR_NUMERO: Final = "«un número»"

#: Todos los marcadores. Ninguno contiene dígitos: por eso las reglas posteriores
#: nunca vuelven a capturar lo ya sustituido.
MARCADORES: Final[frozenset[str]] = frozenset(
    {
        MARCADOR_MONTO,
        MARCADOR_FECHA,
        MARCADOR_RANGO_FECHAS,
        MARCADOR_PORCENTAJE,
        MARCADOR_DIAS,
        MARCADOR_CUOTA,
        MARCADOR_CANTIDAD,
        MARCADOR_NUMERO,
    }
)

# --------------------------------------------------------------------------- #
# Piezas de las expresiones regulares
# --------------------------------------------------------------------------- #
#: Número con separadores de miles y decimales en cualquiera de las dos convenciones
#: que conviven en recibos peruanos: ``1,234.50`` y ``1.234,50``.
_NUM: Final = r"\d+(?:[.,]\d+)*"

#: Meses en español, con las dos grafías vigentes de septiembre/setiembre.
_MESES: Final = (
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"setiembre|septiembre|octubre|noviembre|diciembre"
)

_UNIDADES: Final = r"GB|MB|TB|KB|Mbps|min(?:utos)?|SMS|mensajes|llamadas|canales|l[ií]neas"


class Sustitucion(BaseModel):
    """Una cifra retirada del corpus, con su posición en el texto original."""

    model_config = ConfigDict(extra="forbid")

    original: str
    marcador: str
    clase: str
    inicio: int
    fin: int


class ResultadoSaneado(BaseModel):
    """Texto neutralizado más la traza completa de lo que se retiró."""

    model_config = ConfigDict(extra="forbid")

    texto: str
    sustituciones: list[Sustitucion] = []

    @property
    def originales(self) -> list[str]:
        """Solo los fragmentos retirados, en orden de aparición."""
        return [sustitucion.original for sustitucion in self.sustituciones]

    @property
    def hubo_cambios(self) -> bool:
        """Verdadero si el documento contenía alguna cifra."""
        return bool(self.sustituciones)


# --------------------------------------------------------------------------- #
# Reglas, en orden de aplicación (de la más específica a la más general)
# --------------------------------------------------------------------------- #
# El orden importa: "del 1 al 12 de julio" debe capturarse como rango antes de que
# la regla de fechas se quede solo con "12 de julio" y deje el "1" suelto.
_REGLAS: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    (
        "rango_fechas",
        re.compile(
            rf"\bdel\s+\d{{1,2}}\s*(?:de\s+(?:{_MESES})\s*)?al\s+\d{{1,2}}\s+de\s+(?:{_MESES})"
            rf"(?:\s+de\s+\d{{4}})?",
            re.IGNORECASE,
        ),
        MARCADOR_RANGO_FECHAS,
    ),
    (
        "fecha_iso",
        re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
        MARCADOR_FECHA,
    ),
    (
        "fecha_numerica",
        re.compile(r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"),
        MARCADOR_FECHA,
    ),
    (
        "fecha_literal",
        re.compile(
            rf"\b\d{{1,2}}\s+de\s+(?:{_MESES})(?:\s+de(?:l)?\s+\d{{4}})?", re.IGNORECASE
        ),
        MARCADOR_FECHA,
    ),
    (
        "mes_anio",
        re.compile(rf"\b(?:{_MESES})\s+de(?:l)?\s+\d{{4}}\b", re.IGNORECASE),
        MARCADOR_FECHA,
    ),
    (
        "periodo",
        re.compile(r"\b\d{4}-(?:0[1-9]|1[0-2])\b"),
        MARCADOR_FECHA,
    ),
    (
        "cuota",
        re.compile(r"\bcuota\s+\d+\s+de\s+\d+\b", re.IGNORECASE),
        MARCADOR_CUOTA,
    ),
    (
        "cuotas_totales",
        re.compile(rf"\b\d+\s+cuotas?\b|\ben\s+\d+\s+(?:meses|cuotas)\b|\b{_NUM}\s+de\s+\d+\s+cuotas\b",
                   re.IGNORECASE),
        MARCADOR_CUOTA,
    ),
    (
        "porcentaje",
        re.compile(rf"{_NUM}\s*(?:%|por\s+ciento|puntos\s+b[áa]sicos|bp\b)", re.IGNORECASE),
        MARCADOR_PORCENTAJE,
    ),
    (
        "monto_con_simbolo",
        # "S/ 49.90", "S/. 1,234.50", "S/49,90", "PEN 30", "$ 12"
        re.compile(rf"(?:S\s*/\s*\.?|PEN\b|\$)\s*-?{_NUM}", re.IGNORECASE),
        MARCADOR_MONTO,
    ),
    (
        "monto_con_palabra",
        # "49.90 soles", "30 nuevos soles"
        re.compile(rf"-?{_NUM}\s*(?:nuevos\s+)?soles\b", re.IGNORECASE),
        MARCADOR_MONTO,
    ),
    (
        "dias",
        re.compile(rf"{_NUM}\s*d[ií]as?\b", re.IGNORECASE),
        MARCADOR_DIAS,
    ),
    (
        "meses",
        re.compile(rf"{_NUM}\s*(?:meses|mes)\b", re.IGNORECASE),
        MARCADOR_DIAS,
    ),
    (
        "cantidad_con_unidad",
        re.compile(rf"{_NUM}\s*(?:{_UNIDADES})\b", re.IGNORECASE),
        MARCADOR_CANTIDAD,
    ),
    (
        # Red de seguridad: cualquier dígito que haya sobrevivido a las reglas
        # anteriores. Gracias a esta regla, el resultado NUNCA contiene dígitos.
        "numero_suelto",
        re.compile(rf"-?{_NUM}"),
        MARCADOR_NUMERO,
    ),
)

_RE_ESPACIOS: Final = re.compile(r"[ \t]{2,}")


def _aplicar(texto: str, clase: str, patron: re.Pattern[str], marcador: str,
             registro: list[Sustitucion]) -> str:
    """Sustituye todas las coincidencias de una regla y anota lo retirado."""

    def _reemplazo(coincidencia: re.Match[str]) -> str:
        registro.append(
            Sustitucion(
                original=coincidencia.group(0),
                marcador=marcador,
                clase=clase,
                inicio=coincidencia.start(),
                fin=coincidencia.end(),
            )
        )
        return marcador

    return patron.sub(_reemplazo, texto)


def sanear_detallado(texto: str) -> ResultadoSaneado:
    """Neutraliza el texto y devuelve la traza completa para auditoría.

    Args:
        texto: fragmento de catálogo, FAQ o casuística tal como está en el corpus.

    Returns:
        El texto sin ninguna cifra y la lista de sustituciones con su posición.

    Raises:
        AssertionError: nunca en uso normal; la comprobación final es la prueba viva
            de que la garantía "cero dígitos" se cumple.
    """
    if not texto:
        return ResultadoSaneado(texto="", sustituciones=[])

    # Se normalizan los espacios raros (NBSP y compañía) para que las expresiones
    # regulares vean separadores predecibles entre el símbolo y la cifra.
    resultado = unicodedata.normalize("NFC", texto).replace(" ", " ")
    registro: list[Sustitucion] = []
    for clase, patron, marcador in _REGLAS:
        resultado = _aplicar(resultado, clase, patron, marcador, registro)

    resultado = _RE_ESPACIOS.sub(" ", resultado).strip()

    if any(caracter.isdigit() for caracter in resultado):  # pragma: no cover - red de seguridad
        raise AssertionError(
            "el saneador dejó dígitos en el texto; esto rompe la regla nº 4: " + resultado
        )

    return ResultadoSaneado(texto=resultado, sustituciones=registro)


def sanear(texto: str) -> tuple[str, list[str]]:
    """Sustituye toda cifra por un marcador genérico. **Contrato principal.**

    Args:
        texto: texto recuperado del corpus (catálogo, FAQ o casuística).

    Returns:
        ``(texto_saneado, retirados)`` donde ``texto_saneado`` no contiene ningún
        dígito y ``retirados`` son los fragmentos originales, en orden de aparición,
        para dejarlos registrados en la auditoría.

    Ejemplo:
        >>> sanear("Por ejemplo, un paquete cuesta S/ 49.90 y dura 30 días.")[0]
        'Por ejemplo, un paquete cuesta «un monto» y dura «una cantidad de días».'
    """
    detalle = sanear_detallado(texto)
    return detalle.texto, detalle.originales


def contiene_cifras(texto: str) -> bool:
    """Verdadero si el texto conserva algún dígito (es decir, si NO está saneado)."""
    return any(caracter.isdigit() for caracter in texto)
