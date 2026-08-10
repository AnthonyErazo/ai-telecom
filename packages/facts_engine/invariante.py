"""Invariante de conciliación (sección 4.6): la línea roja del sistema.

``residual = (total_actual − total_previo) − Σ delta_lineas``

Si ``|residual| > 1 céntimo`` **no se explica**: se responde 409 ``INVARIANTE_FALLIDO``
y se deriva a un asesor con contexto. Nunca hay "explicación aproximada": una cifra que
no cuadra en el propio recibo no puede sostener ninguna afirmación al cliente, y el
compromiso del proyecto es cero invenciones financieras.

La tolerancia de ±1 céntimo existe porque el reparto por mayor resto puede dejar un
céntimo de diferencia entre el redondeo del total y el de las líneas; cualquier cosa
mayor es un defecto de datos.
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.core_domain.dinero import Centimos, formatear_soles
from packages.core_domain.esquemas.factset import TOLERANCIA_RESIDUAL_CENT, Invariante, LineaDelta

__all__ = [
    "TOLERANCIA_RESIDUAL_CENT",
    "debe_derivar",
    "mensaje_descuadre",
    "residual_cent",
    "verificar_conciliacion",
]


def _deltas_enteros(deltas: Sequence[LineaDelta] | Sequence[int]) -> list[int]:
    """Admite tanto ``LineaDelta`` como enteros ya extraídos."""
    return [linea.delta_cent if isinstance(linea, LineaDelta) else int(linea) for linea in deltas]


def residual_cent(
    total_actual_cent: Centimos,
    total_previo_cent: Centimos,
    deltas: Sequence[LineaDelta] | Sequence[int],
) -> Centimos:
    """Céntimos que sobran o faltan para que el diff explique el cambio del total.

    ``residual = (total_actual − total_previo) − Σ delta_lineas``. Debe ser 0.
    """
    return (total_actual_cent - total_previo_cent) - sum(_deltas_enteros(deltas))


def verificar_conciliacion(
    total_actual_cent: Centimos,
    total_previo_cent: Centimos,
    deltas: Sequence[LineaDelta] | Sequence[int],
    tolerancia_cent: int = TOLERANCIA_RESIDUAL_CENT,
) -> Invariante:
    """Comprueba que la suma de los deltas por línea reconstruye el delta del total.

    ``ok = |residual_cent| <= tolerancia_cent`` (por defecto, 1 céntimo).

    Las líneas ``IGUAL`` aportan 0, así que da lo mismo incluirlas o no; lo que **no**
    puede faltar es ninguna línea con variación.

    Returns:
        El :class:`Invariante` con el residual, la suma de deltas y el delta total. Si
        no cierra, ``ok`` es ``False`` y la capa superior deriva: el motor no decide.
    """
    return Invariante.evaluar(
        delta_total_cent=total_actual_cent - total_previo_cent,
        deltas=_deltas_enteros(deltas),
        tolerancia_cent=tolerancia_cent,
    )


def debe_derivar(invariante: Invariante) -> bool:
    """Regla dura de derivación: un invariante roto nunca se explica (4.8)."""
    return not invariante.ok


def mensaje_descuadre(invariante: Invariante) -> str:
    """Texto de diagnóstico para la auditoría y el resumen al asesor.

    No es texto para el cliente: va al evento ``INVARIANTE`` de la cadena de auditoría
    y al ``resumen_asesor`` de la derivación.
    """
    if invariante.ok:
        return (
            "conciliación correcta: la variación de "
            f"{formatear_soles(invariante.delta_total_cent)} queda explicada por las líneas"
        )
    return (
        f"descuadre de {invariante.residual_cent} céntimos: el recibo varió "
        f"{formatear_soles(invariante.delta_total_cent)} y las líneas comparadas suman "
        f"{formatear_soles(invariante.suma_deltas_cent)}"
    )
