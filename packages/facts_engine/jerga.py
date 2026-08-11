"""Traduce la jerga peruana a los términos que el clasificador sabe reconocer.

Por qué esto existe
-------------------
El cliente no escribe «solicito la explicación del incremento de mi cargo fijo». Escribe
«oe por q me llego mas caro el recibo» o «ya cancelé, por qué me siguen cobrando». Los
patrones de :mod:`packages.facts_engine.intencion` están redactados en términos canónicos;
sin una capa que traduzca, media Lima cae en FUERA_DE_DOMINIO.

El caso que justifica el módulo entero es **«cancelar»**. En Perú significa *pagar*: «ya
cancelé mi recibo» es «ya lo pagué». Un clasificador entrenado con español neutro lo lee
como *dar de baja* y deriva la conversación a retención — al cliente que acaba de pagar.
Es un error caro y silencioso: la métrica de hand-off lo cuenta como acierto.

De dónde salen los términos
---------------------------
De la tabla ``vocabulario_peruano`` de Supabase, **nunca de este fichero**. Aquí no hay ni
un término escrito a mano: si la base no responde, se devuelve un mapa vacío y el
clasificador se comporta exactamente como antes. Un diccionario de jergas incrustado en el
código sería imposible de corregir para quien atiende a los clientes de verdad.

La carga es perezosa y se cachea en memoria: se paga una consulta por proceso, no una por
frase clasificada.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata

__all__ = ["expandir", "mapa_jerga", "olvidar_cache"]

_LOG = logging.getLogger(__name__)

#: Mapa término→significado ya cargado. ``None`` = todavía no se intentó.
_CACHE: dict[str, str] | None = None


def _sin_tildes(texto: str) -> str:
    """Quita tildes para que «móvil» y «movil» sean la misma palabra."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _consultar() -> dict[str, str]:
    """Lee ``vocabulario_peruano``. Devuelve ``{}`` ante cualquier fallo, nunca lanza.

    Que falte el diccionario degrada la comprensión de la jerga; que reviente el
    clasificador deja al cliente sin respuesta. Lo primero es recuperable.
    """
    cadena = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not cadena:
        return {}
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg es opcional en esta ruta
        return {}
    try:
        with psycopg.connect(cadena, connect_timeout=15) as conexion:
            filas = conexion.execute(
                "SELECT termino, significa, variantes FROM vocabulario_peruano"
            ).fetchall()
    except Exception as exc:
        _LOG.info("jerga: Supabase no disponible (%s); se clasifica sin ella", type(exc).__name__)
        return {}

    mapa: dict[str, str] = {}
    for termino, significa, variantes in filas:
        if not termino or not significa:
            continue
        # La variante apunta al mismo significado que el término principal: «cancele»,
        # «cancelé» y «cancelar» deben resolver todas a «pagar».
        for forma in (termino, *(variantes or [])):
            clave = _sin_tildes(str(forma).strip())
            if clave:
                mapa.setdefault(clave, str(significa).strip())
    if mapa:
        _LOG.info("jerga: %d términos peruanos desde Supabase", len(mapa))
    return mapa


def mapa_jerga() -> dict[str, str]:
    """El diccionario de jergas, cargado una sola vez por proceso."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _consultar()
    return _CACHE


def olvidar_cache() -> None:
    """Descarta el diccionario cacheado. Para las pruebas y para recargar en caliente."""
    global _CACHE
    _CACHE = None


def expandir(texto: str) -> str:
    """Devuelve la frase con el significado canónico **añadido**, no sustituido.

    Se añade en vez de reemplazar porque el término original puede ser el que dispara el
    patrón correcto. «Ya cancelé mi recibo» se convierte en «ya cancelé mi recibo pagar»:
    el patrón de pago encuentra su señal y nada de lo que el cliente escribió se pierde.

    Los términos de varias palabras se buscan primero: «dar de baja» debe ganarle a
    «baja» suelta, o la frase se traduciría dos veces con significados distintos.
    """
    mapa = mapa_jerga()
    if not mapa or not texto:
        return texto

    plano = _sin_tildes(texto)
    anadidos: list[str] = []
    for termino in sorted(mapa, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(termino)}(?!\w)", plano):
            significado = mapa[termino]
            if significado and _sin_tildes(significado) not in plano:
                anadidos.append(significado)
    return f"{texto} {' '.join(anadidos)}" if anadidos else texto
