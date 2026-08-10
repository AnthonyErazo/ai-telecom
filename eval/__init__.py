"""Evaluación reproducible de ``recibo-claro`` (sección 10 de la especificación).

Tres métricas oficiales de la ficha del desafío, más las de apoyo::

    1. Precisión de Recuperación  (field-level · Recall@1 · strict answer accuracy)
    2. Tasa de Alucinación        (TA_asercion · TA_respuesta ← comprometida en 0)
    3. Precisión del Hand-off     (Recall · Precision · atrapamiento · completitud)

**Advertencia de circularidad**: el ground truth de esta evaluación y el sistema que se
evalúa comparten autor. Las cifras validan la **mecánica del motor**, no predicen el
desempeño sobre datos reales de Movistar. Se imprime en cada ejecución de
``run_eval.py`` y está disponible como :data:`eval.metricas.ADVERTENCIA_CIRCULARIDAD`.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Versión del protocolo de evaluación (no del proyecto): cambiarla invalida
#: comparaciones históricas de las tablas publicadas.
__version__ = "1.0.0"
