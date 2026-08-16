"""Construcción del prompt ``explicar_v1`` (sección 5.2).

Cuatro bloques fijos y en orden fijo: **rol y prohibiciones**, **FACTSET**,
**CONTEXTO** y **MENSAJE_CLIENTE**. El mensaje del cliente entra delimitado entre
``<<<`` y ``>>>`` y con la instrucción explícita de tratarlo como dato.

Dos defensas viven aquí y no en el modelo:

* :func:`sanear_utterance` elimina del texto del cliente los propios delimitadores,
  de modo que no pueda "cerrar" el bloque y escribir instrucciones fuera de él.
* :func:`enmascarar_cifras` sustituye toda cifra del CONTEXTO recuperado por un
  marcador genérico. El ``saneador`` del retriever ya lo hace; esto es defensa en
  profundidad: si una FAQ dice "por ejemplo S/ 49,90", ese número no es de este
  cliente y no puede sobrevivir al prompt (regla innegociable nº 4).

Las marcas ``===INICIO X===`` / ``===FIN X===`` son parte del contrato del prompt:
:class:`~packages.llm_layer.providers.mock.MockProvider` recupera el FactSet leyendo
el bloque ``FACTSET``, sin tocar la red y sin canales laterales.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from packages.core_domain.enums import Canal, Verbosidad
from packages.core_domain.esquemas.factset import FactSet
from packages.llm_layer.providers.base import ESQUEMA_EXPLICACION_V1, NOMBRE_ESQUEMA_SALIDA

__all__ = [
    "DELIMITADOR_CIERRE",
    "DELIMITADOR_INICIO",
    "PLANTILLA_EXPLICAR",
    "RUTA_PROMPTS",
    "construir_prompt",
    "enmascarar_cifras",
    "extraer_bloque",
    "mensaje_correccion",
    "sanear_utterance",
    "version_prompt",
]

#: Directorio donde viven los prompts (este mismo paquete).
RUTA_PROMPTS = Path(__file__).resolve().parent

#: Prompt de explicación versionado.
PLANTILLA_EXPLICAR = "explicar_v1.jinja"

DELIMITADOR_INICIO = "<<<"
DELIMITADOR_CIERRE = ">>>"

#: Longitud máxima del mensaje del cliente admitida en el prompt.
LIMITE_UTTERANCE = 2000

#: Longitud máxima de cada fragmento de contexto recuperado.
LIMITE_FRAGMENTO_CONTEXTO = 600

#: Máximo de fragmentos de contexto que entran al prompt.
MAXIMO_FRAGMENTOS = 6


# --------------------------------------------------------------------------- #
# Entorno Jinja
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _entorno() -> Environment:
    """Entorno Jinja de los prompts. Sin autoescape: la salida es texto plano."""
    return Environment(
        loader=FileSystemLoader(str(RUTA_PROMPTS)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@lru_cache(maxsize=1)
def version_prompt() -> str:
    """Identificador versionado del prompt: ``explicar_v1@<sha256 corto>``.

    Viaja en la telemetría para que dos ejecuciones con prompts distintos sean
    distinguibles en la auditoría.
    """
    contenido = (RUTA_PROMPTS / PLANTILLA_EXPLICAR).read_bytes()
    return f"explicar_v1@{hashlib.sha256(contenido).hexdigest()[:12]}"


# --------------------------------------------------------------------------- #
# Saneado de entradas
# --------------------------------------------------------------------------- #
def sanear_utterance(texto: str, limite: int = LIMITE_UTTERANCE) -> str:
    """Neutraliza el mensaje del cliente antes de delimitarlo.

    Quita los delimitadores ``<<<``/``>>>`` (para que no pueda cerrar el bloque y
    escribir fuera de él), colapsa los saltos de línea y recorta a ``limite``.
    El contenido en sí NO se censura: es el dato que hay que entender.
    """
    limpio = (texto or "").replace(DELIMITADOR_INICIO, "«").replace(DELIMITADOR_CIERRE, "»")
    limpio = re.sub(r"[\r\n\t]+", " ", limpio)
    limpio = re.sub(r" {2,}", " ", limpio).strip()
    if len(limpio) > limite:
        limpio = limpio[:limite].rstrip()
    return limpio


#: Patrones de cifra que se enmascaran en el CONTEXTO recuperado, en orden de prioridad.
_PATRONES_MASCARA: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "«una fecha»"),
    (re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"), "«una fecha»"),
    (
        re.compile(
            r"\b\d{1,2}\s+de\s+[a-záéíóúñ]+(?:\s+de\s+\d{4})?\b",
            re.IGNORECASE,
        ),
        "«una fecha»",
    ),
    (re.compile(r"\d+(?:[.,]\d+)?\s*%"), "«un porcentaje»"),
    (
        re.compile(r"(?:S/\.?\s*)\-?\d[\d.,]*|\-?\d{1,3}(?:[.,]\d{3})*[.,]\d{2}\b"),
        "«un monto»",
    ),
    (re.compile(r"\b\d+\b"), "«una cantidad»"),
)


def enmascarar_cifras(texto: str) -> str:
    """Sustituye toda cifra del texto por un marcador genérico.

    Se aplica al CONTEXTO recuperado (catálogo, FAQs, casuísticas). Ninguna cifra de
    un documento recuperado puede llegar al prompt, porque el ``ALLOWED`` del
    verificador se construye SOLO desde el FactSet: si sobreviviera, el modelo podría
    escribirla y la respuesta se bloquearía por alucinación.
    """
    resultado = texto or ""
    for patron, marcador in _PATRONES_MASCARA:
        resultado = patron.sub(marcador, resultado)
    return resultado


def _texto_de_fragmento(item: Any) -> tuple[str, str]:
    """Normaliza un fragmento de contexto a ``(referencia, texto)``.

    Acepta cadenas, diccionarios y objetos tipo ``ItemEvidencia`` (``ref_id`` +
    ``snippet``) para no acoplarse al tipo concreto que devuelva el retriever.
    """
    if isinstance(item, str):
        return ("contexto", item)
    if isinstance(item, dict):
        ref = str(item.get("ref_id") or item.get("ref") or item.get("id") or "contexto")
        texto = str(item.get("snippet") or item.get("texto") or item.get("contenido") or "")
        return (ref, texto)
    ref = str(getattr(item, "ref_id", None) or getattr(item, "ref", None) or "contexto")
    texto = str(getattr(item, "snippet", None) or getattr(item, "texto", None) or "")
    return (ref, texto)


def _preparar_contexto(items: Sequence[Any] | None) -> list[dict[str, str]]:
    """Normaliza, enmascara y recorta los fragmentos recuperados."""
    preparados: list[dict[str, str]] = []
    for item in list(items or [])[:MAXIMO_FRAGMENTOS]:
        ref, texto = _texto_de_fragmento(item)
        if not texto.strip():
            continue
        limpio = re.sub(r"\s+", " ", enmascarar_cifras(texto)).strip()
        preparados.append({"ref": ref, "texto": limpio[:LIMITE_FRAGMENTO_CONTEXTO]})
    return preparados


# --------------------------------------------------------------------------- #
# Construcción del prompt
# --------------------------------------------------------------------------- #
def mensaje_correccion(infractores: Iterable[str]) -> str:
    """Mensaje de reintento exigido por la sección 5.3, paso 6.

    Ejemplo: ``los números 137.40, 12 no existen en FACTSET``.
    """
    listado = ", ".join(dict.fromkeys(str(valor) for valor in infractores)) or "(sin detalle)"
    return (
        f"los números {listado} no existen en FACTSET. "
        "No los escriba de nuevo ni intente corregirlos calculando: use únicamente "
        "los enteros que aparecen en FACTSET."
    )


def construir_prompt(
    factset: FactSet,
    *,
    contexto_recuperado: Sequence[Any] | None = None,
    utterance: str = "",
    verbosidad: Verbosidad = Verbosidad.CORTO,
    canal: Canal = Canal.APP,
    correccion: str | None = None,
    respuestas_previas: Sequence[str] | None = None,
) -> str:
    """Renderiza el prompt ``explicar_v1`` para un FactSet concreto.

    Args:
        factset: hechos verificados; solo viaja su proyección ``resumen_para_prompt``.
        contexto_recuperado: fragmentos del retriever (se enmascaran sus cifras).
        utterance: mensaje literal del cliente (entra delimitado, como dato).
        verbosidad: ``CORTO`` o ``DETALLE``.
        canal: canal de origen, informativo para el modelo.
        correccion: si es un reintento, el mensaje con los números no anclados.
        respuestas_previas: lo que el asistente ya le dijo al cliente en esta misma
            conversación (``MemoriaConversaciones.turnos_asistente``), más reciente
            al final. Se usa solo la última, para que el modelo sepa que no debe
            repetir la misma redacción en la segunda pregunta sobre el mismo recibo.
            Vacío o ``None`` en el primer turno: el prompt queda igual que antes.

    Returns:
        El prompt completo, determinístico para las mismas entradas.
    """
    previas = list(respuestas_previas or ())
    parametros = {
        "esquema": NOMBRE_ESQUEMA_SALIDA,
        "prompt_version": version_prompt(),
        "verbosidad": str(verbosidad),
        "canal": str(canal),
        "modalidad_renta": str(factset.modalidad_renta),
        "firma_causal": factset.firma_causal(),
        "factset_id": str(factset.factset_id),
        "factset_sha256": factset.sha256 or factset.calcular_sha256(),
        "rules_version": factset.rules_version,
        "reintento": bool(correccion),
        "turno_numero": len(previas),
    }
    plantilla = _entorno().get_template(PLANTILLA_EXPLICAR)
    return plantilla.render(
        esquema_json=json.dumps(ESQUEMA_EXPLICACION_V1, ensure_ascii=False, indent=2),
        parametros_json=json.dumps(parametros, ensure_ascii=False, indent=2, sort_keys=True),
        factset_json=json.dumps(
            factset.resumen_para_prompt(), ensure_ascii=False, indent=2, sort_keys=True
        ),
        contexto=_preparar_contexto(contexto_recuperado),
        utterance=sanear_utterance(utterance),
        correccion=correccion,
        respuesta_previa=previas[-1][:600] if previas else None,
    )


def extraer_bloque(prompt: str, nombre: str) -> str | None:
    """Devuelve el contenido de un bloque ``===INICIO nombre=== ... ===FIN nombre===``.

    Es el canal por el que ``MockProvider`` recupera el FactSet: el mock no recibe
    nada que un proveedor real no reciba también.
    """
    patron = re.compile(
        rf"===INICIO {re.escape(nombre)}===\s*(.*?)\s*===FIN {re.escape(nombre)}===",
        re.DOTALL,
    )
    encontrado = patron.search(prompt)
    return encontrado.group(1) if encontrado else None
