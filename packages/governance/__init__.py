"""Gobernanza: auditoría JSONL encadenada por hash y telemetría de silencio.

Dos piezas, ambas exigidas literalmente por la ficha del Desafío 1:

* :mod:`packages.governance.auditoria` — la evidencia *"comprobable mediante logs de la
  terminal"*: un registro append-only donde cada evento sella al anterior.
* :mod:`packages.governance.telemetria` — la *"tasa de silencio post-explicación"*, con
  su sesgo declarado (el abandono ambiguo no cuenta como éxito).
"""

from packages.governance.auditoria import (
    MAX_LINEAS_TURNO,
    ContadorAserciones,
    RegistroAuditoria,
    ResumenTurno,
    formatear_para_terminal,
    leer_eventos,
    registro_por_defecto,
    verificar_cadena,
)
from packages.governance.telemetria import (
    ADVERTENCIA_SESGO,
    MetricasSilencio,
    RegistroTelemetria,
    ResultadoSonda,
    SenalCierre,
    SondaSilencio,
    registro_telemetria_por_defecto,
)

__all__ = [
    "ADVERTENCIA_SESGO",
    "MAX_LINEAS_TURNO",
    "ContadorAserciones",
    "MetricasSilencio",
    "RegistroAuditoria",
    "RegistroTelemetria",
    "ResultadoSonda",
    "ResumenTurno",
    "SenalCierre",
    "SondaSilencio",
    "formatear_para_terminal",
    "leer_eventos",
    "registro_por_defecto",
    "registro_telemetria_por_defecto",
    "verificar_cadena",
]
