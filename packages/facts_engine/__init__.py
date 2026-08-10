"""Motor determinístico de hechos: tramos, prorrateo, diff, atribución, invariante y confianza.

Es el 70 % del valor del proyecto y **no contiene ni una línea de IA**: dados dos recibos
y el historial de órdenes, produce un ``FactSet`` conciliado y sellado que es la única
fuente de cifras del sistema.

Punto de entrada único::

    from packages.facts_engine import construir_factset

    fs = construir_factset(recibo_actual, recibos_previos, movimientos)
    assert fs.invariante.ok        # si no, se deriva a un asesor: no se explica

Reglas que este paquete cumple sin excepción: todo monto es ``int`` en céntimos (un test
hace grep de coma flotante en este directorio y falla la build), y ninguna decisión de
negocio está en el código —viven en ``db/reglas/rules.yaml`` y viajan en ``rules_version``.
"""

from packages.facts_engine.atribucion import atribuir, candidatos_para, esta_atribuida
from packages.facts_engine.confianza import (
    ResultadoIncomprension,
    Turno,
    evaluar_incomprension,
)
from packages.facts_engine.diff import ResumenDiff, comparar, comparar_detallado
from packages.facts_engine.invariante import verificar_conciliacion
from packages.facts_engine.motor import (
    SinReciboPrevio,
    agregar_causas,
    confianza_global,
    construir_factset,
    movimientos_del_ciclo,
    resumen_de_conciliacion,
    seleccionar_recibo_previo,
)
from packages.facts_engine.prorrateo import (
    AjusteRetroactivo,
    ajuste_por_suspension,
    ajuste_retroactivo,
    ajuste_retroactivo_desde_tramos,
    cronograma_frances,
    cuota_equipo_financiado,
    renta_del_ciclo,
    total_adelantada,
    total_vencida,
)
from packages.facts_engine.tramos import DescuentoVigente, construir_tramos, describir_tramos

__all__ = [
    "AjusteRetroactivo",
    "DescuentoVigente",
    "ResultadoIncomprension",
    "ResumenDiff",
    "SinReciboPrevio",
    "Turno",
    "agregar_causas",
    "ajuste_por_suspension",
    "ajuste_retroactivo",
    "ajuste_retroactivo_desde_tramos",
    "atribuir",
    "candidatos_para",
    "comparar",
    "comparar_detallado",
    "confianza_global",
    "construir_factset",
    "construir_tramos",
    "cronograma_frances",
    "cuota_equipo_financiado",
    "describir_tramos",
    "esta_atribuida",
    "evaluar_incomprension",
    "movimientos_del_ciclo",
    "renta_del_ciclo",
    "resumen_de_conciliacion",
    "seleccionar_recibo_previo",
    "total_adelantada",
    "total_vencida",
    "verificar_conciliacion",
]
