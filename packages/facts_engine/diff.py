"""Comparación entre el recibo actual y el previo (sección 4.6).

Es un **FULL OUTER JOIN por ``concepto_id``**: primero se agrupa sumando (un concepto
puede aparecer en varias líneas, una por servicio), y después se comparan los importes
agregados. Comparar línea a línea sin agrupar produciría falsos "NUEVO/DESAPARECIDO"
cada vez que el facturador reordena o parte una línea, que es justo el error que hace
que un asistente le diga al cliente algo que no es.

El diff **no atribuye causas**: solo mide. La causa la pone ``atribucion.py``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import Centimos
from packages.core_domain.enums import ClaseDelta, FamiliaConcepto
from packages.core_domain.esquemas.factset import LineaDelta
from packages.core_domain.esquemas.recibo import LineaRecibo, Tramo
from packages.core_domain.reglas import ConfiguracionReglas

__all__ = [
    "ResumenDiff",
    "agrupar_por_concepto",
    "comparar",
    "comparar_detallado",
    "contar_clases",
]


class ResumenDiff(BaseModel):
    """Resultado completo del diff: lo que se explica y lo que solo se cuenta."""

    model_config = ConfigDict(extra="forbid")

    lineas: list[LineaDelta] = Field(
        default_factory=list, description="Con variación, ordenadas por impacto"
    )
    iguales: list[LineaDelta] = Field(
        default_factory=list, description="Sin variación: NO se explican, pero se cuentan"
    )
    conteo: dict[str, int] = Field(default_factory=dict, description="Líneas por clase")
    suma_deltas_cent: Centimos = 0
    conceptos_comparados: int = 0
    conceptos_fuera_catalogo: list[str] = Field(
        default_factory=list, description="Regla dura de derivación (sección 4.8)"
    )

    @property
    def todas(self) -> list[LineaDelta]:
        """Todas las líneas del diff, incluidas las que no varían."""
        return [*self.lineas, *self.iguales]


def agrupar_por_concepto(lineas: Iterable[LineaRecibo]) -> dict[str, Centimos]:
    """Agrupa las líneas de un recibo sumando por ``concepto_id``.

    ``ma = agrupar_sumando(lineas_actual, key=concepto_id)`` de la sección 4.6.
    """
    agregado: defaultdict[str, int] = defaultdict(int)
    for linea in lineas:
        agregado[linea.concepto_id] += linea.monto_cent
    return dict(agregado)


def _indexar(lineas: Iterable[LineaRecibo]) -> dict[str, list[LineaRecibo]]:
    """Índice ``concepto_id -> líneas``, preservando el orden de aparición."""
    indice: defaultdict[str, list[LineaRecibo]] = defaultdict(list)
    for linea in lineas:
        indice[linea.concepto_id].append(linea)
    return dict(indice)


def _nombre_comercial(
    concepto_id: str,
    actuales: Sequence[LineaRecibo],
    previas: Sequence[LineaRecibo],
    reglas: ConfiguracionReglas | None,
) -> str:
    """Nombre que verá el cliente: el del recibo actual, el del previo o el del catálogo."""
    for grupo in (actuales, previas):
        for linea in grupo:
            if linea.nombre_comercial:
                return linea.nombre_comercial
    if reglas is not None:
        concepto = reglas.concepto(concepto_id)
        if concepto is not None:
            return concepto.nombre_comercial
    return concepto_id


def _familia(
    concepto_id: str,
    actuales: Sequence[LineaRecibo],
    previas: Sequence[LineaRecibo],
    reglas: ConfiguracionReglas | None,
) -> FamiliaConcepto | None:
    """Familia contable del concepto, del recibo o del catálogo."""
    for grupo in (actuales, previas):
        for linea in grupo:
            return linea.familia
    return reglas.familia(concepto_id) if reglas is not None else None


def _evidencia(
    concepto_id: str, actuales: Sequence[LineaRecibo], previas: Sequence[LineaRecibo]
) -> list[str]:
    """Referencias citables de la línea: las líneas de origen y la ficha de catálogo."""
    refs = [f"linea:{linea.linea_id}" for linea in (*actuales, *previas)]
    refs.append(f"cat:{concepto_id}")
    return sorted(dict.fromkeys(refs))


def _tramos(actuales: Sequence[LineaRecibo]) -> list[Tramo] | None:
    """Tramos declarados en el recibo actual para ese concepto (explicación del prorrateo)."""
    tramos = [tramo for linea in actuales for tramo in linea.tramos]
    return tramos or None


def _unico(valores: Sequence[int | None]) -> int | None:
    """Devuelve el valor si todas las líneas coinciden; ``None`` si hay ambigüedad."""
    presentes = {valor for valor in valores if valor is not None}
    return presentes.pop() if len(presentes) == 1 else None


def comparar(
    lineas_actual: Iterable[LineaRecibo],
    lineas_previo: Iterable[LineaRecibo],
    *,
    reglas: ConfiguracionReglas | None = None,
    incluir_iguales: bool = False,
    confianza_inicial: float = 1.0,
) -> list[LineaDelta]:
    """FULL OUTER JOIN entre los dos recibos, agrupando y sumando por concepto (4.6).

    Clasificación de cada concepto, con ``a`` = importe actual y ``b`` = importe previo::

        NUEVO         si b == 0 y a != 0
        DESAPARECIDO  si a == 0 y b != 0
        IGUAL         si a - b == 0
        SUBIO         si a - b > 0
        BAJO          si a - b < 0

    Las líneas ``IGUAL`` **no se devuelven**: no hay nada que explicar en ellas (sí se
    cuentan, con ``comparar_detallado``). Aportan 0 al invariante, así que su ausencia
    no altera la conciliación.

    El resultado va ordenado por impacto absoluto descendente y, a igualdad, por
    ``concepto_id``: el orden es determinista y es el que se narra.

    Args:
        lineas_actual: líneas del recibo del periodo que se explica.
        lineas_previo: líneas del recibo inmediatamente anterior.
        reglas: catálogo para completar nombres y familias (opcional).
        incluir_iguales: añade también las líneas sin variación.
        confianza_inicial: confianza con la que nacen las líneas. El diff es exacto por
            construcción; la incertidumbre la introduce la atribución de causa (4.7).

    Returns:
        Una ``LineaDelta`` por concepto, ya validada (``delta == actual − previo`` y la
        clase coherente con ambos importes).
    """
    indice_actual = _indexar(lineas_actual)
    indice_previo = _indexar(lineas_previo)
    montos_actual = {
        cid: sum(linea.monto_cent for linea in grupo) for cid, grupo in indice_actual.items()
    }
    montos_previo = {
        cid: sum(linea.monto_cent for linea in grupo) for cid, grupo in indice_previo.items()
    }

    deltas: list[LineaDelta] = []
    for concepto_id in sorted(set(montos_actual) | set(montos_previo)):
        actuales = indice_actual.get(concepto_id, [])
        previas = indice_previo.get(concepto_id, [])
        monto_actual = montos_actual.get(concepto_id, 0)
        monto_previo = montos_previo.get(concepto_id, 0)
        clase = LineaDelta.clasificar(monto_actual, monto_previo)
        if clase is ClaseDelta.IGUAL and not incluir_iguales:
            continue
        deltas.append(
            LineaDelta(
                concepto_id=concepto_id,
                nombre_comercial=_nombre_comercial(concepto_id, actuales, previas, reglas),
                clase=clase,
                monto_actual_cent=monto_actual,
                monto_previo_cent=monto_previo,
                delta_cent=monto_actual - monto_previo,
                confianza=confianza_inicial,
                familia=_familia(concepto_id, actuales, previas, reglas),
                dias_prorrateo=_unico([linea.dias_prorrateo for linea in actuales]),
                movimiento_id=_unico([linea.movimiento_id for linea in actuales]),
                cuota_numero=_unico([linea.cuota_numero for linea in actuales]),
                cuotas_totales=_unico([linea.cuotas_totales for linea in actuales]),
                tramos=_tramos(actuales),
                evidencia=_evidencia(concepto_id, actuales, previas),
            )
        )
    deltas.sort(key=lambda linea: (-abs(linea.delta_cent), linea.concepto_id))
    return deltas


def contar_clases(deltas: Iterable[LineaDelta]) -> dict[str, int]:
    """Cuenta las líneas por clase. Los ``IGUAL`` no se explican, pero sí se cuentan."""
    conteo = {clase.value: 0 for clase in ClaseDelta}
    for linea in deltas:
        conteo[linea.clase.value] += 1
    return conteo


def comparar_detallado(
    lineas_actual: Iterable[LineaRecibo],
    lineas_previo: Iterable[LineaRecibo],
    *,
    reglas: ConfiguracionReglas | None = None,
    confianza_inicial: float = 1.0,
) -> ResumenDiff:
    """Igual que :func:`comparar`, pero devolviendo también lo que no se explica.

    Además detecta los conceptos **fuera de catálogo**, que son una regla dura de
    derivación (4.8): si el recibo trae un concepto que el sistema no sabe explicar,
    no se improvisa, se deriva a un asesor.
    """
    todas = comparar(
        lineas_actual,
        lineas_previo,
        reglas=reglas,
        incluir_iguales=True,
        confianza_inicial=confianza_inicial,
    )
    con_variacion = [linea for linea in todas if linea.se_explica]
    iguales = [linea for linea in todas if not linea.se_explica]
    fuera = (
        sorted(
            linea.concepto_id for linea in todas if not reglas.existe_concepto(linea.concepto_id)
        )
        if reglas is not None
        else []
    )
    return ResumenDiff(
        lineas=con_variacion,
        iguales=iguales,
        conteo=contar_clases(todas),
        suma_deltas_cent=sum(linea.delta_cent for linea in todas),
        conceptos_comparados=len(todas),
        conceptos_fuera_catalogo=fuera,
    )
