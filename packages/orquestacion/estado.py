"""Estado del turno de conversación: lo que viaja por el grafo y lo que se persiste.

Qué modela este módulo
----------------------
Hoy, ``POST /v1/explicar`` sostiene el turno entero en **variables locales** de una
función de cuatrocientas líneas. Eso funciona, pero tiene tres consecuencias que este
módulo corrige sin cambiar ni una decisión de negocio:

1. **No hay forma de inspeccionar el turno a mitad de camino.** Si algo va mal después
   de la generación, la única prueba es la bitácora; el estado en sí se pierde.
2. **No hay forma de reanudar.** Un turno interrumpido —o un hand-off que espera la
   respuesta de un asesor— empieza de cero.
3. **El reinicio del proceso borra la conversación.** ``MemoriaConversaciones`` vive en
   RAM y ``@lru_cache`` la ata a la vida del proceso.

:class:`EstadoTurno` es ese mismo conjunto de variables, declarado, tipado y
serializable, de modo que el *checkpointer* pueda escribirlo en disco entre pasos.

La separación que hay que entender
----------------------------------
Hay **dos** cosas que un nodo necesita, y solo una se persiste:

* :class:`EstadoTurno` — los **datos** del turno. Se persisten. Todo lo que hay aquí
  sobrevive a un reinicio del proceso.
* :class:`Servicios` — las **dependencias vivas** (bitácora, memoria, repositorio,
  proveedor generativo…). No se persisten ni se pueden persistir: son objetos con
  cerrojos, conexiones HTTP y descriptores de fichero. Viajan por el ``context`` de
  LangGraph, que es efímero por diseño.

Confundir las dos sería el error clásico: meter la bitácora en el estado haría que cada
paso intentara serializar un ``threading.Lock``.

Reglas que este módulo respeta
------------------------------
* **Ninguna cifra nace aquí.** El estado *transporta* el ``FactSet``; no lo calcula ni
  lo modifica. Todo importe sigue siendo un ``int`` en céntimos que salió del motor
  determinístico.
* **Los acumuladores llevan reductor.** ``eventos`` y ``nodos`` se anotan por concate-
  nación (``operator.add``), que es lo que permite que cada nodo devuelva solo *su*
  aportación en vez de reescribir la lista entera.
* El resto de claves usa el canal por defecto de LangGraph (*último valor gana*), que
  es exactamente la semántica de una variable local reasignada.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from apps.api.acl import RepositorioCuentas
from apps.api.deps import EstadoAdversario, MemoriaConversaciones
from apps.api.settings import Ajustes
from packages.core_domain.enums import Canal, NivelAseguramiento, Verbosidad
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import (
    Accion,
    Bloque,
    Derivacion,
    Gobernanza,
    RespuestaCanalAgnostica,
)
from packages.core_domain.reglas import ConfiguracionReglas
from packages.facts_engine.confianza import ResultadoIncomprension, Turno
from packages.facts_engine.intencion import ResultadoIntencion
from packages.governance.auditoria import RegistroAuditoria
from packages.governance.telemetria import RegistroTelemetria
from packages.llm_layer.generador import ResultadoGeneracion
from packages.llm_layer.providers.base import ProveedorLLM
from packages.llm_layer.verificador import ResultadoVerificacion
from packages.retriever import ContextoRecuperado, Recuperador

__all__ = [
    "CORTE_INVARIANTE_ROTO",
    "CORTE_VERIFICACION_FALLIDA",
    "EstadoTurno",
    "Servicios",
    "estado_inicial",
]

#: Motivos de corte duro. Son los dos únicos casos en los que el sistema sabe que **no
#: puede sostener un número** y por eso no escribe ninguno: el recibo no concilia
#: (sección 4.6) y el verificador cazó una cifra sin respaldo (sección 5.3).
CORTE_INVARIANTE_ROTO = "INVARIANTE_ROTO"
CORTE_VERIFICACION_FALLIDA = "VERIFICACION_FALLIDA"


class EstadoTurno(TypedDict, total=False):
    """Un turno de conversación completo, paso a paso.

    ``total=False`` a propósito: cada nodo devuelve **solo las claves que produce**, y
    LangGraph funde ese diccionario parcial sobre el estado acumulado. Un nodo nunca
    tiene que reconstruir lo que no le incumbe.
    """

    # --- Entrada del turno: la fija el borde HTTP, los nodos no la tocan ---------- #
    #: Identificador de la traza. Agrupa todos los eventos del turno en la bitácora.
    trace_id: str
    #: Conversación, en texto. Es también el ``thread_id`` del *checkpointer*.
    conversation_id: str
    #: ``account_ref`` **del token**, nunca del cuerpo de la petición.
    cuenta: str
    canal: Canal
    #: Nivel de aseguramiento del solicitante; decide la redacción final.
    nivel: NivelAseguramiento
    #: Lo que escribió el cliente. Entra al prompt como dato delimitado, jamás como
    #: instrucción.
    utterance: str
    #: Texto literal antes de resolver una respuesta corta contra el contexto pendiente.
    #: Permite conservar «si» en el historial aunque el motor procese una consulta
    #: efectiva como «¿se aplicó prorrateo en mi recibo?».
    utterance_original: str
    verbosidad: Verbosidad
    periodo: str | None
    #: Campos de identidad que acompañan a cada evento de la bitácora.
    contexto_auditoria: dict[str, Any]

    # --- Acumuladores (con reductor) ---------------------------------------------- #
    #: Etapas de auditoría emitidas, en orden. Es la traza legible del turno.
    eventos: Annotated[list[str], operator.add]
    #: Nodos recorridos, en orden. Lo que comprueban las pruebas del grafo.
    nodos: Annotated[list[str], operator.add]

    # --- Resultado de cada paso ---------------------------------------------------- #
    intencion: ResultadoIntencion | None
    #: **La única fuente de cifras del sistema.** Sellado con su ``sha256``.
    factset: FactSet | None
    historial: list[Turno]
    contexto_recuperado: ContextoRecuperado | None
    incomprension: ResultadoIncomprension | None
    resultado_generacion: ResultadoGeneracion | None
    verificacion: ResultadoVerificacion | None
    #: Traza de la demo adversaria, cuando ``POST /dev/alucinar`` la activó.
    adversaria: dict[str, Any] | None
    #: ``True`` si el proveedor generativo falló y respondió la plantilla. **No es un
    #: error**: se responde 200 con la cabecera ``X-Degradado``.
    degradado: bool
    #: Motivo del corte duro que obliga a derivar, o ``None``.
    corte: str | None

    # --- Salida --------------------------------------------------------------------- #
    bloques: list[Bloque]
    acciones: list[Accion]
    derivacion: Derivacion
    gobernanza: Gobernanza | None
    telemetria: dict[str, Any]
    context_ref: str | None
    #: La respuesta ya redactada para el nivel del solicitante. Es lo que devuelve el
    #: endpoint sin tocar nada más.
    respuesta: RespuestaCanalAgnostica | None


@dataclass(slots=True)
class Servicios:
    """Dependencias vivas del turno. **No se persisten.**

    Son exactamente las que hoy inyecta FastAPI por ``Depends`` en
    ``explicar_recibo``. Viajan por el ``context`` de LangGraph —efímero por diseño— en
    lugar de por el estado, porque una conexión HTTP o un ``threading.Lock`` no se
    pueden serializar y porque tampoco tendría ningún sentido guardarlos: al reanudar un
    turno se quieren los servicios **del proceso actual**, no los del que murió.
    """

    ajustes: Ajustes
    repositorio: RepositorioCuentas
    reglas: ConfiguracionReglas
    recuperador: Recuperador | None
    proveedor: ProveedorLLM | None
    auditoria: RegistroAuditoria
    memoria: MemoriaConversaciones
    telemetria: RegistroTelemetria
    adversario: EstadoAdversario
    #: Objetos vivos del turno que no se pueden serializar y que, aun así, dos nodos
    #: necesitan compartir. Hoy solo el ``ConjuntoPermitido``: es una clase con
    #: ``__slots__`` sin representación JSON, y meterla en el estado haría reventar al
    #: *checkpointer*. Como es determinista desde el ``FactSet``, si la bolsa viniera
    #: vacía (por ejemplo tras reanudar) simplemente se reconstruye.
    bolsa: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def desde_dependencias(cls) -> Servicios:
        """Construye los servicios desde los singletons del proceso.

        Sirve para usar el grafo **fuera** de una petición HTTP —pruebas, guiones de
        demostración, evaluación— sin tener que levantar FastAPI. Dentro de una
        petición, el endpoint pasa las instancias que ya le inyectó ``Depends``, que son
        estos mismos objetos.
        """
        from apps.api.deps import (
            obtener_adversario,
            obtener_memoria,
            obtener_proveedor_llm,
            obtener_recuperador,
            obtener_registro_auditoria,
            obtener_registro_telemetria,
            obtener_reglas,
            obtener_repositorio,
        )
        from apps.api.settings import obtener_ajustes

        return cls(
            ajustes=obtener_ajustes(),
            repositorio=obtener_repositorio(),
            reglas=obtener_reglas(),
            recuperador=obtener_recuperador(),
            proveedor=obtener_proveedor_llm(),
            auditoria=obtener_registro_auditoria(),
            memoria=obtener_memoria(),
            telemetria=obtener_registro_telemetria(),
            adversario=obtener_adversario(),
        )


def estado_inicial(
    *,
    trace_id: str,
    conversation_id: str,
    cuenta: str,
    canal: Canal,
    nivel: NivelAseguramiento,
    contexto_auditoria: dict[str, Any],
    utterance: str = "",
    verbosidad: Verbosidad = Verbosidad.CORTO,
    periodo: str | None = None,
) -> EstadoTurno:
    """Estado de arranque de un turno, con los acumuladores ya inicializados.

    Las listas con reductor tienen que existir antes del primer nodo: ``operator.add``
    necesita un valor izquierdo. Se declaran aquí y no en cada nodo para que ningún
    llamador pueda olvidarse.
    """
    return EstadoTurno(
        trace_id=trace_id,
        conversation_id=conversation_id,
        cuenta=cuenta,
        canal=canal,
        nivel=nivel,
        utterance=utterance,
        utterance_original=utterance,
        verbosidad=verbosidad,
        periodo=periodo,
        contexto_auditoria=dict(contexto_auditoria),
        eventos=[],
        nodos=[],
        historial=[],
        bloques=[],
        acciones=[],
        telemetria={},
        degradado=False,
        corte=None,
        adversaria=None,
        context_ref=None,
        intencion=None,
        factset=None,
        contexto_recuperado=None,
        incomprension=None,
        resultado_generacion=None,
        verificacion=None,
        gobernanza=None,
        respuesta=None,
        derivacion=Derivacion(),
    )
