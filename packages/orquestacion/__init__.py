"""Capa de orquestación del turno: LangGraph sobre el motor que ya existe.

Punto de importación único::

    from packages.orquestacion import Servicios, ejecutar_turno, estado_inicial

    estado = estado_inicial(
        trace_id=trace_id,
        conversation_id=str(conversacion),
        cuenta=cuenta,
        canal=canal,
        nivel=identidad.acr,
        contexto_auditoria=identidad.contexto_auditoria(),
        utterance=peticion.utterance,
        verbosidad=peticion.verbosidad,
        periodo=peticion.periodo,
    )
    final = ejecutar_turno(estado, servicios=Servicios(...))
    respuesta = final["respuesta"]

Qué aporta y qué **no** toca
----------------------------
Aporta tres cosas que hoy no existen: el turno como **grafo explícito** con sus ramas
auditables, un **estado tipado** en vez de treinta variables locales, y un
**checkpointer persistente** para que la conversación sobreviva al reinicio del proceso.

No toca —ni podría— lo que da valor al sistema: el motor determinístico
(``packages/facts_engine``), el verificador numérico
(``packages/llm_layer/verificador.py``), el saneador y la fusión RRF del retriever, y
la bitácora con cadena de hash. El grafo **llama** a esas funciones; no las sustituye.

Licencias
---------
Solo se usan paquetes MIT ya instalados: ``langgraph``, ``langchain-core``,
``langgraph-checkpoint`` y ``langgraph-checkpoint-sqlite``. Queda **prohibido**
``langgraph-api``, ``langgraph-cli`` y LangGraph Platform: están bajo Elastic License
2.0 o son propietarios, y la sección 9 de las BASES cede la propiedad intelectual a
Integratel — una dependencia con licencia restringida obligaría a Integratel a negociar
con un tercero para desplegar su propia solución.

Telemetría de terceros: **apagada al importar este paquete**. Ver
:mod:`packages.orquestacion.telemetria_externa`: ninguna conversación de cliente sale
hacia LangSmith ni hacia ningún otro servicio externo.
"""

from packages.orquestacion.telemetria_externa import (
    VARIABLES_APAGADO,
    apagar_telemetria_externa,
    telemetria_externa_activa,
)

# Primero se apaga el trazado, después se importa cualquier cosa que arrastre LangGraph.
apagar_telemetria_externa()

from packages.orquestacion.checkpointer import (  # noqa: E402
    VALOR_EN_MEMORIA,
    VAR_RUTA_CHECKPOINT,
    Checkpointer,
    abrir_checkpointer,
    cerrar_checkpointer,
    obtener_checkpointer,
    ruta_checkpoint,
)
from packages.orquestacion.estado import (  # noqa: E402
    CORTE_INVARIANTE_ROTO,
    CORTE_VERIFICACION_FALLIDA,
    EstadoTurno,
    Servicios,
    estado_inicial,
)
from packages.orquestacion.grafo import (  # noqa: E402
    NOMBRE_GRAFO,
    PRIORIDAD_DE_RUTA,
    VAR_DURABILIDAD,
    cerrar_grafo,
    compilar_grafo,
    construir_grafo,
    ejecutar_turno,
    estado_del_checkpointer,
    obtener_grafo,
    ruta_por_intencion,
    ruta_por_invariante,
    ruta_por_verificacion,
)
from packages.orquestacion.nodos import (  # noqa: E402
    NOMBRES_DE_NODO,
    clasificar,
    construir_hechos,
    derivar,
    generar,
    recuperar_contexto,
    responder_intencion,
    verificar_y_armar,
)

__all__ = [
    "CORTE_INVARIANTE_ROTO",
    "CORTE_VERIFICACION_FALLIDA",
    "NOMBRES_DE_NODO",
    "NOMBRE_GRAFO",
    "PRIORIDAD_DE_RUTA",
    "VALOR_EN_MEMORIA",
    "VARIABLES_APAGADO",
    "VAR_DURABILIDAD",
    "VAR_RUTA_CHECKPOINT",
    "Checkpointer",
    "EstadoTurno",
    "Servicios",
    "abrir_checkpointer",
    "apagar_telemetria_externa",
    "cerrar_checkpointer",
    "cerrar_grafo",
    "clasificar",
    "compilar_grafo",
    "construir_grafo",
    "construir_hechos",
    "derivar",
    "ejecutar_turno",
    "estado_del_checkpointer",
    "estado_inicial",
    "generar",
    "obtener_checkpointer",
    "obtener_grafo",
    "recuperar_contexto",
    "responder_intencion",
    "ruta_checkpoint",
    "ruta_por_intencion",
    "ruta_por_invariante",
    "ruta_por_verificacion",
    "telemetria_externa_activa",
    "verificar_y_armar",
]
