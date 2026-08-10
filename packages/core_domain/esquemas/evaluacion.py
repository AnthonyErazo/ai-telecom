"""Esquemas de evaluación: casos golden y ground truth del generador sintético.

El ground truth se escribe **en el mismo acto** de generar el escenario, nunca se
deduce después. La evaluación imprime siempre la advertencia de circularidad: el
ground truth y el sistema comparten autor.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import Centimos
from packages.core_domain.enums import (
    Canal,
    CausaOficial,
    ModalidadRenta,
    NivelAseguramiento,
    TipoMovimiento,
    Verbosidad,
)
from packages.core_domain.esquemas.recibo import Periodo

__all__ = ["CasoGolden", "GroundTruthCausaDelta"]


class GroundTruthCausaDelta(BaseModel):
    """Fila de ``gt_causa_delta``: la verdad conocida de por qué varió un concepto.

    El generador **aborta** si la suma de ``delta_cent`` de un cliente y periodo no
    coincide con ``total_actual - total_previo``.
    """

    model_config = ConfigDict(extra="forbid")

    cuenta_id: str
    periodo: Periodo
    concepto_id: str
    causa: TipoMovimiento | None = None
    delta_cent: Centimos
    movimiento_id: int | None = None
    escenario: str | None = Field(default=None, description="Escenario inyectado que lo produjo")


class CasoGolden(BaseModel):
    """Caso de ``eval/golden/*.yaml``: entrada fija y expectativas verificables.

    El test golden falla la build si aparece cualquier cifra fuera del FactSet o si
    ``verificacion_numerica != "PASS"``.
    """

    model_config = ConfigDict(extra="forbid")

    caso_id: str
    descripcion: str = ""
    seed: int
    cuenta_id: str
    periodo: Periodo
    verbosidad: Verbosidad = Verbosidad.CORTO
    canal: Canal = Canal.APP
    nivel: NivelAseguramiento = NivelAseguramiento.LOA2
    utterance: str = "¿por qué me vino más caro este mes?"
    modalidad_renta: ModalidadRenta | None = None
    escenarios: list[str] = Field(default_factory=list)
    causas_esperadas: list[TipoMovimiento] = Field(default_factory=list)
    causas_oficiales_esperadas: list[CausaOficial] = Field(default_factory=list)
    conceptos_esperados: list[str] = Field(default_factory=list)
    delta_esperado_cent: Centimos
    total_esperado_cent: Centimos
    debe_derivar: bool = False
    no_debe_contener: list[str] = Field(
        default_factory=list,
        description="Fragmentos prohibidos en la respuesta (adversariales, cifras ajenas)",
    )
