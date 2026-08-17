"""Inyección de dependencias de la API, con cacheo por proceso.

Todo lo caro se construye **una vez por proceso** y se reparte por ``Depends``: las
reglas de negocio (``rules.yaml``), el repositorio del ACL (clientes HTTP incluidos), el
recuperador RAG (índices BM25 y vectorial), el proveedor generativo, el registro de
auditoría y el de telemetría. Un ``lru_cache`` por dependencia basta: FastAPI llama a la
función en cada petición y recibe siempre la misma instancia.

Aquí vive además la **memoria de turno** (:class:`MemoriaConversaciones`): un almacén en
proceso, acotado, con lo que necesitan ``GET /v1/evidencia``, ``POST /v1/derivacion`` y
el score de incomprensión (historial de turnos por conversación). En producción sería
Redis o Postgres; la interfaz es la misma y por eso está aislada en una clase.

Nada de esto persiste secretos ni PII: el almacén guarda ``cuenta_ref`` (identificador
ficticio tokenizado) y el ``FactSet``, que es exactamente lo que la auditoría ya sella.
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends

from apps.api.acl import RepositorioCuentas, crear_repositorio
from apps.api.conversaciones_store import AlmacenConversaciones
from apps.api.settings import ALMACENAMIENTO_POSTGRES, Ajustes, obtener_ajustes
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import ItemEvidencia, RespuestaCanalAgnostica
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas
from packages.facts_engine.confianza import Turno
from packages.governance.auditoria import RegistroAuditoria, registro_por_defecto
from packages.governance.telemetria import RegistroTelemetria, registro_telemetria_por_defecto
from packages.llm_layer.providers.base import ErrorProveedor, ProveedorLLM, obtener_proveedor
from packages.retriever import IndiceVectorial, Recuperador

__all__ = [
    "AdversarioDep",
    "AjustesDep",
    "AuditoriaDep",
    "ConversacionesDep",
    "EstadoAdversario",
    "MemoriaConversaciones",
    "MemoriaDep",
    "ProveedorDep",
    "RecuperadorDep",
    "RegistroExplicacion",
    "ReglasDep",
    "RepositorioDep",
    "TelemetriaDep",
    "cerrar_recursos",
    "nuevo_trace_id",
    "obtener_adversario",
    "obtener_almacen_conversaciones",
    "obtener_memoria",
    "obtener_proveedor_llm",
    "obtener_recuperador",
    "obtener_registro_auditoria",
    "obtener_registro_telemetria",
    "obtener_reglas",
    "obtener_repositorio",
]

_LOG = logging.getLogger(__name__)

#: Cuántas explicaciones y conversaciones se recuerdan en memoria.
CAPACIDAD_MEMORIA = 512


def nuevo_trace_id() -> str:
    """Identificador de traza de un turno: corto, único y legible en la terminal."""
    return f"tr-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Singletons caros
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def obtener_reglas() -> ConfiguracionReglas:
    """Reglas de negocio (``db/reglas/rules.yaml``). Inmutable: no se muta nunca."""
    ajustes = obtener_ajustes()
    reglas = cargar_reglas(ajustes.rules_path or None)
    if reglas.rules_version != ajustes.rules_version:
        _LOG.warning(
            "RULES_VERSION del entorno (%s) no coincide con la de rules.yaml (%s); "
            "manda el fichero",
            ajustes.rules_version,
            reglas.rules_version,
        )
    return reglas


@lru_cache(maxsize=1)
def obtener_repositorio() -> RepositorioCuentas:
    """Repositorio del ACL: BrainyBill + Amdocs, por HTTP o por disco."""
    ajustes = obtener_ajustes()
    return crear_repositorio(
        brainybill_base_url=ajustes.brainybill_base_url,
        amdocs_base_url=ajustes.amdocs_base_url,
        raiz_datos=ajustes.ruta_datos,
        timeout_s=ajustes.http_timeout_s,
        ciclos=ajustes.ciclos_brainybill,
    )


@lru_cache(maxsize=1)
def obtener_recuperador() -> Recuperador | None:
    """Recuperador RAG sobre catálogo, FAQ y casuísticas.

    Degradar nunca es un error aquí: sin base de datos el índice vive en memoria y sin
    clave de Gemini se usa el embebedor determinístico. Si además faltara el corpus en
    disco, se devuelve ``None`` y la explicación se genera igual —solo pierde el color
    narrativo del contexto—, porque las cifras no salen del RAG sino del FactSet.

    El DSN se le pasa **explícitamente** al índice desde los ajustes en lugar de dejar
    que él lea ``DATABASE_URL`` del entorno: así ``MODO_ALMACENAMIENTO`` es el único
    interruptor y un ``.env`` heredado no puede colar una conexión que nadie pidió.
    """
    ajustes = obtener_ajustes()
    try:
        return Recuperador.desde_corpus(
            ajustes.ruta_datos, indice_vectorial=_indice_vectorial(ajustes)
        )
    except Exception as error:
        _LOG.error("no se pudo cargar el corpus RAG (%s); se seguirá sin contexto", error)
        return None


def _indice_vectorial(ajustes: Ajustes) -> IndiceVectorial:
    """Índice vectorial del RAG, en pgvector o en memoria según ``MODO_ALMACENAMIENTO``.

    En memoria no se intenta ninguna conexión, así que el arranque no paga el timeout
    de PostgreSQL: es la diferencia entre arrancar en medio segundo y arrancar en tres.
    """
    dsn = ajustes.dsn_postgres
    diagnostico = ajustes.almacenamiento()
    motivo = str(diagnostico["motivo"])
    if dsn is None:
        if ajustes.modo_almacenamiento == ALMACENAMIENTO_POSTGRES:
            _LOG.warning(
                "%s: el índice vectorial se construye EN MEMORIA. "
                "Defina DATABASE_URL o ponga MODO_ALMACENAMIENTO=memoria para no verlo.",
                motivo,
            )
        else:
            _LOG.info(
                "almacenamiento en memoria (%s): no se usa PostgreSQL. "
                "El índice RAG vive en el proceso; el dataset y la bitácora, en disco.",
                motivo,
            )
        return IndiceVectorial(forzar_memoria=True, motivo_memoria=motivo)
    _LOG.info("almacenamiento en PostgreSQL: %s", diagnostico["destino"])
    return IndiceVectorial(dsn=dsn)


@lru_cache(maxsize=1)
def obtener_proveedor_llm() -> ProveedorLLM | None:
    """Proveedor generativo según ``LLM_MODE``; ``None`` si no hay ninguno utilizable.

    Que no haya proveedor **no es un error**: la capa generativa cae a la plantilla
    determinística, que por construcción solo escribe cifras del FactSet.
    """
    ajustes = obtener_ajustes()
    try:
        # Las credenciales se pasan EXPLÍCITAMENTE desde los ajustes. Los proveedores
        # saben leerlas de `os.environ`, pero pydantic-settings carga el `.env` en el
        # objeto de ajustes y no en el entorno del proceso: sin este paso, una clave
        # perfectamente configurada en `.env` no llegaba nunca al proveedor y el
        # sistema degradaba a plantilla sin decir por qué.
        proveedor = obtener_proveedor(
            ajustes.llm_mode,
            api_key=ajustes.gemini_api_key or None,
            modelo=ajustes.gemini_model or None,
            timeout_s=ajustes.llm_timeout_s,
        )
    except ErrorProveedor as error:
        _LOG.warning(
            "no hay proveedor LLM (%s): se responderá con plantilla determinística",
            error.codigo,
        )
        return None
    _LOG.info("proveedor generativo activo: %s", proveedor.nombre)
    return proveedor


@lru_cache(maxsize=1)
def obtener_registro_auditoria() -> RegistroAuditoria:
    """Bitácora JSONL append-only con cadena de hashes (sección 7)."""
    ajustes = obtener_ajustes()
    return registro_por_defecto(ajustes.audit_log_path or None)


@lru_cache(maxsize=1)
def obtener_registro_telemetria() -> RegistroTelemetria:
    """Registro de la tasa de silencio post-explicación."""
    ajustes = obtener_ajustes()
    return registro_telemetria_por_defecto(ajustes.telemetria_path or None)


@lru_cache(maxsize=1)
def obtener_almacen_conversaciones() -> AlmacenConversaciones:
    """Historial durable de BillSense en la misma instancia de Supabase."""
    ajustes = obtener_ajustes()
    return AlmacenConversaciones(ajustes.supabase_db_url or ajustes.database_url)


# --------------------------------------------------------------------------- #
# Memoria de turno
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RegistroExplicacion:
    """Lo que se guarda de una explicación para poder citarla y derivarla después."""

    explicacion_id: str
    trace_id: str
    conversation_id: str
    cuenta_ref: str
    periodo: str
    factset: FactSet
    respuesta: RespuestaCanalAgnostica
    evidencia: list[ItemEvidencia] = field(default_factory=list)
    utterance: str = ""
    canal: str = "APP"
    creado_en: datetime = field(default_factory=lambda: datetime.now(UTC))
    contexto_rag: dict[str, Any] = field(default_factory=dict)
    score_incomprension: float | None = None
    derivada: bool = False


@dataclass(slots=True)
class EstadoAdversario:
    """Interruptor de la demo adversaria (``POST /dev/alucinar``).

    Cuando está activo, ``/v1/explicar`` inyecta una cifra inventada en el texto ya
    generado **antes** de la verificación final. El objetivo es enseñar en vivo que el
    verificador la caza y que la respuesta se bloquea: la métrica oficial es *"cero
    invenciones financieras comprobables mediante logs de la terminal"*.
    """

    activo: bool = False
    delta_cent: int = 731
    turnos_restantes: int = 1

    def consumir(self) -> bool:
        """Devuelve si este turno debe ser adversario y descuenta el contador."""
        if not self.activo:
            return False
        self.turnos_restantes -= 1
        if self.turnos_restantes <= 0:
            self.activo = False
        return True


class MemoriaConversaciones:
    """Almacén en proceso, acotado y con bloqueo, de explicaciones y turnos.

    Tres usos:

    * ``GET /v1/evidencia/{explicacion_id}`` — items de evidencia del turno.
    * ``POST /v1/derivacion`` — recupera el último FactSet y arma el brief del asesor.
    * ``confianza.evaluar_incomprension`` — historial de turnos de la conversación,
      necesario para las señales ``s3`` (repregunta) y ``s6`` (turnos sin progreso).
    """

    def __init__(self, capacidad: int = CAPACIDAD_MEMORIA) -> None:
        self.capacidad = capacidad
        self._cerrojo = threading.Lock()
        self._explicaciones: OrderedDict[str, RegistroExplicacion] = OrderedDict()
        self._turnos: OrderedDict[str, list[Turno]] = OrderedDict()
        self._contextos: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._derivadas: set[str] = set()
        #: conversation_id -> asesor que está dentro de la sala ahora mismo.
        self._asesores: OrderedDict[str, str] = OrderedDict()

    # -- explicaciones ------------------------------------------------------ #
    def guardar_explicacion(self, registro: RegistroExplicacion) -> RegistroExplicacion:
        """Guarda la explicación y desaloja la más antigua si se supera la capacidad."""
        with self._cerrojo:
            self._explicaciones[registro.explicacion_id] = registro
            self._recortar(self._explicaciones)
        return registro

    def explicacion(self, explicacion_id: str) -> RegistroExplicacion | None:
        """Devuelve la explicación por su identificador, o ``None`` si ya no está."""
        with self._cerrojo:
            return self._explicaciones.get(explicacion_id)

    def ultima_de_conversacion(self, conversation_id: str) -> RegistroExplicacion | None:
        """Última explicación emitida en una conversación."""
        with self._cerrojo:
            for registro in reversed(self._explicaciones.values()):
                if registro.conversation_id == conversation_id:
                    return registro
        return None

    # -- turnos -------------------------------------------------------------- #
    def anotar_turno(self, conversation_id: str, turno: Turno) -> None:
        """Añade un turno al historial de la conversación."""
        with self._cerrojo:
            historial = self._turnos.setdefault(conversation_id, [])
            historial.append(turno)
            del historial[:-20]  # 20 turnos bastan para s3 y s6
            self._turnos.move_to_end(conversation_id)
            self._recortar(self._turnos)

    def turnos(self, conversation_id: str) -> list[Turno]:
        """Historial de turnos de la conversación (lista vacía si es la primera)."""
        with self._cerrojo:
            return list(self._turnos.get(conversation_id, ()))

    def turnos_asistente(self, conversation_id: str) -> list[str]:
        """Lo que el asistente ya dijo en esta conversación.

        Sirve para que no repita la misma frase turno tras turno, que es lo que
        delata a un bot: se le pasa al redactor como "esto ya lo dijiste".
        """
        with self._cerrojo:
            return [
                turno.utterance
                for turno in self._turnos.get(conversation_id, ())
                if turno.rol == "asistente" and turno.utterance
            ]

    def marcar_derivada(self, conversation_id: str) -> None:
        """Recuerda que esta conversación ya se derivó (histéresis: no se vuelve)."""
        with self._cerrojo:
            self._derivadas.add(conversation_id)

    def fue_derivada(self, conversation_id: str) -> bool:
        """``True`` si la conversación ya pasó por una derivación."""
        with self._cerrojo:
            return conversation_id in self._derivadas

    # -- sala de conversación con asesor -------------------------------------- #
    def asesor_presente(self, conversation_id: str) -> str | None:
        """Identificador del asesor que se ha sumado, o ``None`` si no hay ninguno.

        Distinto de :meth:`fue_derivada`: una conversación puede estar derivada —el
        expediente está en la cola— sin que nadie la haya recogido todavía. Esto marca
        el momento en que una **persona real entra a la sala**.
        """
        with self._cerrojo:
            return self._asesores.get(conversation_id)

    def unir_asesor(self, conversation_id: str, asesor_id: str) -> bool:
        """Suma un asesor a la conversación. ``False`` si ya había otro dentro.

        Se rechaza el segundo asesor a propósito: dos personas escribiendo a la vez al
        mismo cliente es peor experiencia que una cola. Quien la tomó, la atiende.
        """
        with self._cerrojo:
            actual = self._asesores.get(conversation_id)
            if actual is not None and actual != asesor_id:
                return False
            self._asesores[conversation_id] = asesor_id
            self._recortar(self._asesores)
            return True

    def salir_asesor(self, conversation_id: str) -> None:
        """El asesor abandona la sala; el asistente vuelve a atender en autónomo."""
        with self._cerrojo:
            self._asesores.pop(conversation_id, None)

    def pendientes_de_atender(self) -> list[dict[str, Any]]:
        """Cola del 104: derivaciones abiertas que ningún asesor ha recogido aún.

        Ordenadas de más antigua a más nueva, que es como se atiende una cola.
        """
        with self._cerrojo:
            contextos = list(self._contextos.items())
            asesores = dict(self._asesores)
        pendientes = []
        for context_ref, contexto in contextos:
            conversacion = str(contexto.get("conversation_id", ""))
            if conversacion and conversacion in asesores:
                continue
            pendientes.append(
                {
                    "context_ref": context_ref,
                    "conversation_id": conversacion,
                    "cuenta_id": contexto.get("cuenta_id"),
                    "motivo_codigo": contexto.get("motivo_codigo"),
                    "canal": contexto.get("canal"),
                    "resumen_asesor": contexto.get("resumen_asesor"),
                    "creado_en": contexto.get("creado_en"),
                    "trace_id": contexto.get("trace_id"),
                }
            )
        return pendientes

    # -- contextos de derivación --------------------------------------------- #
    def guardar_contexto(self, context_ref: str, contexto: dict[str, Any]) -> None:
        """Guarda el contexto que recogerá el asesor del 104."""
        with self._cerrojo:
            self._contextos[context_ref] = contexto
            self._recortar(self._contextos)

    def contexto(self, context_ref: str) -> dict[str, Any] | None:
        """Contexto de una derivación por su referencia."""
        with self._cerrojo:
            return self._contextos.get(context_ref)

    # -- interno -------------------------------------------------------------- #
    def _recortar(self, mapa: OrderedDict[str, Any]) -> None:
        """Desaloja los elementos más antiguos hasta respetar la capacidad."""
        while len(mapa) > self.capacidad:
            mapa.popitem(last=False)


@lru_cache(maxsize=1)
def obtener_memoria() -> MemoriaConversaciones:
    """Memoria de turno del proceso."""
    return MemoriaConversaciones()


@lru_cache(maxsize=1)
def obtener_adversario() -> EstadoAdversario:
    """Interruptor de la demo adversaria (solo lo toca el router ``/dev``)."""
    return EstadoAdversario()


# --------------------------------------------------------------------------- #
# Ciclo de vida
# --------------------------------------------------------------------------- #
def calentar(ajustes: Ajustes) -> dict[str, Any]:
    """Construye los singletons al arrancar y devuelve un resumen para el log.

    Se hace en el ``lifespan`` para que la primera petición no pague la construcción de
    los índices (importa: la ficha exige aguantar picos de 3× la volumetría).
    """
    estado: dict[str, Any] = {"entorno": ajustes.entorno}
    estado["almacenamiento"] = ajustes.almacenamiento()
    reglas = obtener_reglas()
    estado["rules_version"] = reglas.rules_version
    estado["conceptos"] = len(reglas.catalogo)
    recuperador = obtener_recuperador()
    estado["rag"] = recuperador.estado() if recuperador is not None else {"disponible": False}
    proveedor = obtener_proveedor_llm()
    estado["llm"] = getattr(proveedor, "nombre", "plantilla")
    obtener_repositorio()
    estado["auditoria"] = str(obtener_registro_auditoria().ruta)
    estado["telemetria"] = str(obtener_registro_telemetria().ruta)
    estado["orquestador"] = ajustes.orquestador
    if ajustes.usa_grafo:
        estado["checkpoints"] = _calentar_orquestacion()
    return estado


def _calentar_orquestacion() -> dict[str, Any]:
    """Compila el grafo y abre el fichero de *checkpoints* al arrancar.

    Se hace aquí y no en la primera petición por lo mismo que el resto de singletons:
    abrir SQLite y crear sus tablas dentro del turno de un cliente es latencia que ese
    cliente no tiene por qué pagar.

    El import es **diferido** y el fallo es **no fatal**: con ``ORQUESTADOR=directo`` no
    se toca LangGraph siquiera, y si la capa de orquestación no cargara, la API arranca
    igual y responde por la vía directa. Perder la orquestación degrada la
    demostración; impedir el arranque la cancela.
    """
    try:
        from packages.orquestacion import estado_del_checkpointer, obtener_grafo

        obtener_grafo()
        return dict(estado_del_checkpointer())
    except Exception as error:
        _LOG.error(
            "no se pudo preparar la capa de orquestación (%s); "
            "ponga ORQUESTADOR=directo para usar la vía sin LangGraph",
            error,
        )
        return {"disponible": False, "motivo": str(error)}


def _cerrar_orquestacion() -> None:
    """Suelta el grafo compilado y cierra la conexión SQLite de los *checkpoints*.

    Solo actúa si la capa de orquestación llegó a importarse: en ``ORQUESTADOR=directo``
    no hay nada que cerrar y no tiene sentido importar LangGraph para averiguarlo.

    El orden importa: primero el grafo, que es quien retiene el *saver*, y después la
    conexión. Al revés quedaría un grafo compilado apuntando a una conexión cerrada.
    """
    if "packages.orquestacion.checkpointer" not in sys.modules:
        return
    try:
        from packages.orquestacion import cerrar_checkpointer, cerrar_grafo

        cerrar_grafo()
        cerrar_checkpointer()
    except Exception as error:  # pragma: no cover - el apagado nunca puede lanzar
        _LOG.warning("error cerrando la capa de orquestación: %s", error)


def cerrar_recursos() -> None:
    """Cierra transportes y limpia las cachés (apagado ordenado y tests)."""
    try:
        obtener_repositorio().cerrar()
    except Exception as error:
        _LOG.warning("error cerrando el repositorio: %s", error)
    _cerrar_orquestacion()
    for cache in (
        obtener_reglas,
        obtener_repositorio,
        obtener_recuperador,
        obtener_proveedor_llm,
        obtener_registro_auditoria,
        obtener_registro_telemetria,
        obtener_almacen_conversaciones,
        obtener_memoria,
        obtener_adversario,
    ):
        cache.cache_clear()


def historial_para_score(
    memoria: MemoriaConversaciones, conversation_id: str, utterance: str
) -> Sequence[Turno]:
    """Historial de la conversación tal como lo espera ``evaluar_incomprension``.

    Son los turnos **anteriores**, y solo ellos. El mensaje de este turno viaja aparte,
    en el argumento ``utterance``, y así es como lo espera ``_repregunta``.

    Antes se añadía el turno actual al final de la lista "para que ``s3`` lo compare con
    el anterior". El efecto real era el contrario: ``_repregunta`` recorre los últimos
    turnos del cliente y el último era el propio mensaje, de modo que se comparaba
    consigo mismo. ``Jaccard(x, x) = 1.0``, por encima del umbral de 0.80, y la primera
    pregunta de cualquier cliente nuevo entraba en el score como si estuviera
    repreguntando: 0.20 gratis sobre ``U`` desde el primer turno.

    El parámetro ``utterance`` se conserva —el llamador ya lo tiene a mano y la firma es
    estable— aunque hoy no se use aquí: quien lo necesita es ``evaluar_incomprension``.
    """
    del utterance  # el mensaje del turno actual NO forma parte del historial previo
    return memoria.turnos(conversation_id)


# --------------------------------------------------------------------------- #
# Alias de dependencia (evitan repetir Annotated[..., Depends(...)] en los routers)
# --------------------------------------------------------------------------- #
AjustesDep = Annotated[Ajustes, Depends(obtener_ajustes)]
ReglasDep = Annotated[ConfiguracionReglas, Depends(obtener_reglas)]
RepositorioDep = Annotated[RepositorioCuentas, Depends(obtener_repositorio)]
RecuperadorDep = Annotated[Recuperador | None, Depends(obtener_recuperador)]
ProveedorDep = Annotated[ProveedorLLM | None, Depends(obtener_proveedor_llm)]
AuditoriaDep = Annotated[RegistroAuditoria, Depends(obtener_registro_auditoria)]
TelemetriaDep = Annotated[RegistroTelemetria, Depends(obtener_registro_telemetria)]
ConversacionesDep = Annotated[AlmacenConversaciones, Depends(obtener_almacen_conversaciones)]
MemoriaDep = Annotated[MemoriaConversaciones, Depends(obtener_memoria)]
AdversarioDep = Annotated[EstadoAdversario, Depends(obtener_adversario)]
