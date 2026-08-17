"""Sala compartida: el asesor humano entra a la **misma** conversación.

Por qué existe este módulo
--------------------------
Hasta ahora la derivación era un **expediente en una cola**: el sistema preparaba el
resumen, las causas y la evidencia, marcaba la conversación como derivada y ahí
terminaba su papel. El asesor recogía el caso *en otro sitio*, y el cliente se quedaba
mirando un chat que ya no le hablaba.

La ficha pide *«derivar a un asesor humano con contexto»* y que los asesores reciban
*«derivaciones más filtradas y contextualizadas»* — y eso lo cumplía el expediente. Pero
la experiencia natural, la que el cliente espera de cualquier chat moderno, es otra: que
**una persona entre a la conversación que ya está abierta**, con todo lo dicho delante.

Las tres reglas que ordenan este módulo
---------------------------------------
1. **El verificador numérico NO se aplica a lo que escribe el asesor.** La garantía de
   cero cifras sin respaldo cubre a la máquina, que no puede responder de sus palabras.
   Una persona sí responde de las suyas. Aplicarle el verificador sería fingir que el
   sistema puede avalar a un humano, y bloquearle un mensaje correcto porque cita un
   dato que no está en el ``FactSet`` sería incorrecto y humillante. Lo que sí se hace
   es **dejar constancia nominal**: cada turno de asesor va a la bitácora con su
   identificador, de modo que quede claro quién dijo qué.

2. **El asistente no se apaga: pasa a copiloto.** Cuando hay un asesor dentro, la IA
   deja de hablarle al cliente y queda a disposición del asesor. Es la diferencia entre
   una IA que estorba y una que sabe quitarse de en medio sin dejar de ayudar.

3. **Un asesor por sala.** El segundo que intente entrar recibe un conflicto. Dos
   personas escribiendo a la vez al mismo cliente es peor experiencia que una cola.

El paquete de contexto
----------------------
La sala resuelve el traspaso **en la App**, donde el asesor entra a la conversación que
ya está abierta. Los otros dos canales no tienen sala: en WhatsApp el asesor toma el
número desde otra herramienta y no ve nada de nuestro estado; en voz, recibe una llamada.
Para los tres hace falta lo mismo —el contenido— y cambia solo el transporte. Eso es
``GET /v1/asesor/paquete/{context_ref}``: el :class:`PaqueteAsesor`, construido desde la
bitácora encadenada y con el brief verificado por el mismo verificador numérico que
protege al cliente. Cómo lo consume cada canal está en ``docs/PAQUETE_ASESOR.md``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import AuditoriaDep, MemoriaDep, nuevo_trace_id
from apps.api.errores import ErrorApi, nivel_insuficiente
from apps.api.security import Identidad, identidad_actual, requiere_nivel
from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento
from packages.core_domain.esquemas.paquete_asesor import PaqueteAsesor
from packages.facts_engine.confianza import Turno
from packages.governance.paquete_asesor import construir_paquete_asesor, traza_de_context_ref

__all__ = [
    "EstadoClienteSala",
    "EstadoSala",
    "MensajeAsesor",
    "SolicitudLlamada",
    "TurnoPublicado",
    "router",
]

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/asesor", tags=["asesor"])

#: Máximo de caracteres de un mensaje de asesor. No es una restricción de negocio: es
#: una defensa elemental contra un cuerpo de petición desmedido.
MAX_MENSAJE = 2000


# --------------------------------------------------------------------------- #
# Contratos
# --------------------------------------------------------------------------- #
class MensajeAsesor(BaseModel):
    """Lo que el asesor escribe al cliente dentro de la conversación."""

    model_config = ConfigDict(extra="forbid")

    texto: str = Field(min_length=1, max_length=MAX_MENSAJE)


class TurnoPublicado(BaseModel):
    """Un turno tal y como lo ve la consola del asesor o la del cliente."""

    model_config = ConfigDict(extra="forbid")

    rol: str
    texto: str
    ts: datetime | None = None
    autor: str | None = Field(
        default=None,
        description="Identificador del asesor, solo en los turnos de rol asesor",
    )


class EstadoSala(BaseModel):
    """Estado de la conversación: quién la atiende y qué se ha dicho."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    modo: str = Field(description="AUTONOMA cuando responde la IA, ASISTIDA con asesor dentro")
    asesor: str | None = None
    derivada: bool = False
    turnos: list[TurnoPublicado] = Field(default_factory=list)
    resumen_asesor: str | None = None
    cuenta_id: str | None = None


class EstadoClienteSala(BaseModel):
    """Proyección de la sala que puede consultar el cliente.

    El cliente necesita ver cuándo una persona entra, el nombre con el que se presenta y
    sus mensajes, pero no recibe el resumen operativo ni la cuenta usada para auditar la
    atención.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    modo: str
    asesor_nombre: str | None = None
    derivada: bool = False
    turnos: list[TurnoCliente] = Field(default_factory=list)


class TurnoCliente(BaseModel):
    """Turno público: no expone detalles internos del asesor."""

    model_config = ConfigDict(extra="forbid")

    rol: str
    texto: str
    ts: datetime | None = None


class SolicitudLlamada(BaseModel):
    """Constancia de que el asesor pidió una llamada desde la consola."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    estado: str = "SOLICITADA"


class ElementoCola(BaseModel):
    """Una derivación esperando a que alguien la recoja."""

    model_config = ConfigDict(extra="forbid")

    context_ref: str
    conversation_id: str
    cuenta_id: str | None = None
    canal: str | None = None
    motivo_codigo: str | None = None
    resumen_asesor: str | None = None
    trace_id: str | None = None
    creado_en: str | None = None


# --------------------------------------------------------------------------- #
# Ayudas
# --------------------------------------------------------------------------- #
def _solo_asesor(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA_ASESOR))],
) -> Identidad:
    """Exige que quien llama sea **exactamente** un asesor, no alguien que lo alcanza.

    ``requiere_nivel`` compara por :data:`~apps.api.security.ORDEN_NIVELES`, donde
    ``LOA_ASESOR`` y ``LOA2`` valen lo mismo —y con razón: un asesor ve lo mismo que el
    titular, con más deberes—. El efecto colateral es que un token ``LOA2`` del propio
    cliente *alcanza* el nivel ``LOA_ASESOR`` y entraba en este router.

    Para la sala eso ya era discutible; para el paquete es inaceptable: lleva las
    confianzas del motor, las hipótesis sin confirmar y la tarea pendiente del asesor.
    Son notas internas, no información del cliente sobre sí mismo. Aquí se exige el
    nivel literal, y ``acting_on_behalf_of`` —que ``LOA_ASESOR`` obliga a declarar— es lo
    que deja registrado en la bitácora a nombre de quién se actuó.
    """
    if identidad.acr is not NivelAseguramiento.LOA_ASESOR:
        raise nivel_insuficiente(identidad.acr, NivelAseguramiento.LOA_ASESOR)
    return identidad


_AsesorDep = Annotated[Identidad, Depends(_solo_asesor)]


def _identificador(identidad: Identidad) -> str:
    """Quién es el asesor a efectos de auditoría y de ocupación de la sala."""
    return getattr(identidad, "actor", None) or getattr(identidad, "sub", None) or "asesor"


def _contexto_de(memoria: Any, conversation_id: str) -> dict[str, Any] | None:
    """Busca el expediente de derivación asociado a una conversación."""
    for elemento in memoria.pendientes_de_atender():
        if elemento["conversation_id"] == conversation_id:
            return dict(elemento)
    ultima = memoria.ultima_de_conversacion(conversation_id)
    if ultima is None:
        return None
    return {"conversation_id": conversation_id, "cuenta_id": getattr(ultima, "cuenta_ref", None)}


def _sala(memoria: Any, conversation_id: str) -> EstadoSala:
    """Proyecta el estado de la sala a partir de la memoria de conversación."""
    asesor = memoria.asesor_presente(conversation_id)
    contexto = _contexto_de(memoria, conversation_id) or {}
    turnos = [
        TurnoPublicado(
            rol=turno.rol,
            texto=turno.utterance,
            ts=turno.ts,
            autor=asesor if turno.rol == "asesor" else None,
        )
        for turno in memoria.turnos(conversation_id)
        if turno.utterance
    ]
    return EstadoSala(
        conversation_id=conversation_id,
        modo="ASISTIDA" if asesor else "AUTONOMA",
        asesor=asesor,
        derivada=memoria.fue_derivada(conversation_id),
        turnos=turnos,
        resumen_asesor=contexto.get("resumen_asesor"),
        cuenta_id=contexto.get("cuenta_id"),
    )


def _sala_cliente(memoria: Any, conversation_id: str) -> EstadoClienteSala:
    """Expone al titular solo los turnos y el estado que puede ver en su canal."""
    sala = _sala(memoria, conversation_id)
    return EstadoClienteSala(
        conversation_id=sala.conversation_id,
        modo=sala.modo,
        asesor_nombre=sala.asesor,
        derivada=sala.derivada,
        turnos=[TurnoCliente(rol=turno.rol, texto=turno.texto, ts=turno.ts) for turno in sala.turnos],
    )


def _cliente_de_la_conversacion(memoria: Any, conversation_id: str) -> str | None:
    """Cuenta propietaria de la sala, incluso después de que el asesor la tome."""
    contexto = _contexto_de(memoria, conversation_id) or {}
    return contexto.get("cuenta_id")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get(
    "/cola",
    response_model=list[ElementoCola],
    summary="Derivaciones esperando a que un asesor las recoja",
)
def cola(identidad: _AsesorDep, memoria: MemoriaDep) -> list[ElementoCola]:
    """Cola del 104: casos derivados que nadie ha tomado todavía.

    Se excluyen las conversaciones que ya tienen un asesor dentro, porque una cola que
    muestra lo que otro está atendiendo genera dos personas sobre el mismo cliente.
    """
    return [ElementoCola(**elemento) for elemento in memoria.pendientes_de_atender()]


@router.post(
    "/conversacion/{conversation_id}/unirse",
    response_model=EstadoSala,
    summary="El asesor entra a la conversación y la IA pasa a copiloto",
)
def unirse(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> EstadoSala:
    """Suma al asesor a la sala. A partir de aquí el asistente no responde al cliente.

    Raises:
        ErrorApi: ``404`` si la conversación no existe; ``409`` si ya hay otro asesor.
    """
    if not memoria.turnos(conversation_id):
        raise ErrorApi(
            codigo="CONVERSACION_NO_ENCONTRADA",
            detalle="no hay ninguna conversación con ese identificador",
            estado=404,
        )
    asesor_id = _identificador(identidad)
    presente = memoria.asesor_presente(conversation_id)
    if presente == asesor_id:
        # Recargar el dashboard no es una nueva entrada: no se duplica el saludo ni la
        # bitácora y el cliente no recibe avisos repetidos.
        return _sala(memoria, conversation_id)
    if presente is not None:
        raise ErrorApi(
            codigo="SALA_OCUPADA",
            detalle="otro asesor ya está atendiendo esta conversación",
            estado=409,
            datos={"asesor_actual": presente},
        )
    if not memoria.unir_asesor(conversation_id, asesor_id):
        raise ErrorApi(
            codigo="SALA_OCUPADA",
            detalle="otro asesor ya está atendiendo esta conversación",
            estado=409,
            datos={"asesor_actual": memoria.asesor_presente(conversation_id)},
        )
    saludo_automatico = memoria.registrar_saludo_asesor(conversation_id, asesor_id)
    if saludo_automatico:
        saludo = f"Hola, mi nombre es {asesor_id} y vengo a ayudarle con su consulta."
        memoria.anotar_turno(
            conversation_id,
            Turno(utterance=saludo, rol="asesor", ts=datetime.now(UTC), progreso=True),
        )
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "ASESOR_ENTRA",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "modo": "ASISTIDA",
            "saludo_automatico": saludo_automatico,
        },
        **identidad.contexto_auditoria(),
    )
    _LOG.info("asesor %s entra en la conversación %s", asesor_id, conversation_id)
    return _sala(memoria, conversation_id)


@router.get(
    "/conversacion/{conversation_id}/cliente",
    response_model=EstadoClienteSala,
    summary="Estado de la atención para la App o WhatsApp del titular",
)
def estado_cliente(
    conversation_id: str,
    identidad: Annotated[Identidad, Depends(identidad_actual)],
    memoria: MemoriaDep,
) -> EstadoClienteSala:
    """Devuelve al titular el nombre visible y los mensajes del asesor en su conversación.

    Es la otra mitad del hand-off: el asesor se une por la consola y el cliente ve sus
    respuestas en el mismo chat. El expediente interno nunca cruza esta frontera.
    """
    if not memoria.turnos(conversation_id):
        raise ErrorApi(
            codigo="CONVERSACION_NO_ENCONTRADA",
            detalle="no hay ninguna conversación con ese identificador",
            estado=404,
        )
    cuenta = _cliente_de_la_conversacion(memoria, conversation_id)
    if cuenta and cuenta != identidad.cuenta_ref:
        raise ErrorApi(
            codigo="CUENTA_NO_AUTORIZADA",
            detalle="la conversación pertenece a otra cuenta",
            estado=403,
        )
    return _sala_cliente(memoria, conversation_id)


@router.post(
    "/conversacion/{conversation_id}/solicitar-llamada",
    response_model=SolicitudLlamada,
    summary="El asesor solicita una llamada desde el dashboard",
)
def solicitar_llamada(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> SolicitudLlamada:
    """Registra la derivación a voz sin copiar teléfonos al dashboard.

    El marcador real pertenece a la plataforma de telefonía de Movistar y no está en
    este prototipo. Esta operación deja la intención, el asesor y la conversación en
    la bitácora para que el conector de voz la consuma sin volver a pedir contexto.
    """
    asesor_id = _identificador(identidad)
    if memoria.asesor_presente(conversation_id) != asesor_id:
        raise ErrorApi(
            codigo="ASESOR_NO_EN_SALA",
            detalle="únase a la conversación antes de solicitar una llamada",
            estado=409,
        )
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "SOLICITUD_LLAMADA",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "integracion_voz": "PENDIENTE_CONECTOR_TELEFONIA",
        },
        **identidad.contexto_auditoria(),
    )
    return SolicitudLlamada(conversation_id=conversation_id)


@router.post(
    "/conversacion/{conversation_id}/mensaje",
    response_model=EstadoSala,
    summary="El asesor escribe al cliente dentro de la conversación",
)
def mensaje(
    conversation_id: str,
    cuerpo: MensajeAsesor,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> EstadoSala:
    """Publica un turno de rol ``asesor``.

    **Este texto no pasa por el verificador numérico**, y es deliberado: el verificador
    garantiza que la *máquina* no invente cifras. Una persona responde de sus propias
    palabras. Lo que sí queda es el rastro nominal en la bitácora.

    Raises:
        ErrorApi: ``409`` si quien escribe no es el asesor que ocupa la sala.
    """
    asesor_id = _identificador(identidad)
    presente = memoria.asesor_presente(conversation_id)
    if presente != asesor_id:
        raise ErrorApi(
            codigo="ASESOR_NO_EN_SALA",
            detalle="únase a la conversación antes de escribir en ella",
            estado=409,
            datos={"asesor_actual": presente},
        )
    texto = cuerpo.texto.strip()
    memoria.anotar_turno(
        conversation_id,
        Turno(utterance=texto, rol="asesor", ts=datetime.now(UTC), progreso=True),
    )
    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "MENSAJE_ASESOR",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "caracteres": len(texto),
            # Se declara explícitamente para que nadie lea la bitácora y crea que el
            # verificador avaló este texto: no lo hizo, ni debía.
            "verificacion_numerica": "NO_APLICA",
            "motivo_no_aplica": "turno escrito por una persona, no por el modelo",
        },
        **identidad.contexto_auditoria(),
    )
    return _sala(memoria, conversation_id)


@router.post(
    "/conversacion/{conversation_id}/salir",
    response_model=EstadoSala,
    summary="El asesor deja la sala y la IA vuelve a atender",
)
def salir(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
    auditoria: AuditoriaDep,
) -> EstadoSala:
    """Libera la sala. El asistente vuelve a modo autónomo."""
    asesor_id = _identificador(identidad)
    memoria.salir_asesor(conversation_id)
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "ASESOR_SALE",
            "conversation_id": conversation_id,
            "asesor": asesor_id,
            "modo": "AUTONOMA",
        },
        **identidad.contexto_auditoria(),
    )
    return _sala(memoria, conversation_id)


def _paquete_de(
    auditoria: Any, identidad: Identidad, context_ref: str
) -> PaqueteAsesor:
    """Resuelve el ``context_ref`` en la bitácora y arma el paquete del asesor.

    El paso por la bitácora es el punto: la referencia se busca **en los eventos
    sellados**, no en la memoria del proceso. Si el proceso se reinició, o si quien
    pregunta es otro nodo del servicio, el paquete sale igual, porque la evidencia está
    en disco y no en una variable.

    Raises:
        ErrorApi: ``404`` si ninguna traza declaró esa referencia; ``403`` si el
            expediente pertenece a otra cuenta que la que el asesor declara atender.
    """
    eventos = auditoria.leer()
    trace_id = traza_de_context_ref(eventos, context_ref)
    if trace_id is None:
        raise ErrorApi(
            codigo="CONTEXTO_NO_ENCONTRADO",
            detalle=f"ninguna traza auditada declara la referencia {context_ref}",
            estado=404,
            datos={"context_ref": context_ref},
        )
    cadena_valida, indice_roto = auditoria.verificar_cadena()
    paquete = construir_paquete_asesor(
        eventos,
        trace_id=trace_id,
        context_ref=context_ref,
        cadena_valida=cadena_valida,
        indice_roto=indice_roto,
    )
    # Defensa en profundidad, igual que en ``GET /v1/derivacion/{context_ref}``: el
    # nivel ya limita a asesores, pero un asesor solo atiende la cuenta que declaró en
    # ``acting_on_behalf_of``. Un intento cruzado tiene que fallar, no colarse.
    if paquete.cuenta_id and paquete.cuenta_id != identidad.cuenta_ref:
        raise ErrorApi(
            codigo="CUENTA_NO_AUTORIZADA",
            detalle="el expediente pertenece a otra cuenta",
            estado=403,
            datos={"context_ref": context_ref},
        )
    return paquete


@router.get(
    "/paquete/{context_ref}",
    response_model=PaqueteAsesor,
    summary="Paquete de contexto del asesor, reconstruido desde la bitácora",
    responses={
        403: {"description": "CUENTA_NO_AUTORIZADA"},
        404: {"description": "CONTEXTO_NO_ENCONTRADO"},
    },
)
def paquete(
    context_ref: str,
    identidad: _AsesorDep,
    auditoria: AuditoriaDep,
) -> PaqueteAsesor:
    """Todo lo que el asesor necesita para retomar el caso **sin que el cliente repita**.

    Es el mismo contenido para los tres canales; lo que cambia es el transporte. Trae el
    delta y las líneas que lo componen, lo que ya se le dijo al cliente cifra a cifra,
    **qué no se pudo confirmar y por qué**, el motivo de la derivación y la referencia
    con la que auditar todo lo anterior.

    El ``brief`` viene ya verificado: ``verificacion_brief.veredicto`` es ``PASS`` solo
    si cada cifra del texto está respaldada por el paquete. Un consumidor que vaya a
    mostrar el brief como texto debe mirar ``apto_para_entregar`` antes.
    """
    resultado = _paquete_de(auditoria, identidad, context_ref)
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        nuevo_trace_id(),
        {
            "etapa": "asesor",
            "evento": "PAQUETE_ENTREGADO",
            # La clave NO es ``context_ref``: ese nombre lo usan los turnos que
            # **acuñan** un expediente, y este evento solo lo consulta. Mezclarlos hacía
            # que la búsqueda de la referencia acabara encontrándose a sí misma.
            "expediente_ref": context_ref,
            "traza_origen": resultado.evidencia.trace_id,
            "canal": resultado.canal,
            "incertidumbres": len(resultado.incertidumbres),
            "verificacion_brief": str(
                resultado.verificacion_brief.veredicto if resultado.verificacion_brief else "?"
            ),
        },
        **identidad.contexto_auditoria(),
    )
    return resultado


@router.get(
    "/paquete/{context_ref}/texto",
    response_class=PlainTextResponse,
    summary="El mismo paquete en texto plano, para canales que solo admiten texto",
    responses={
        403: {"description": "CUENTA_NO_AUTORIZADA"},
        404: {"description": "CONTEXTO_NO_ENCONTRADO"},
        409: {"description": "BRIEF_NO_VERIFICADO: el brief no pasó el verificador"},
    },
)
def paquete_en_texto(
    context_ref: str,
    identidad: _AsesorDep,
    auditoria: AuditoriaDep,
) -> str:
    """El paquete como texto, que es lo único que viaja por WhatsApp o por un SMS.

    Existe para demostrar que el paquete es **canal-agnóstico**: el mismo contenido, sin
    volver a calcular nada, servido en el formato que el transporte admite.

    Se niega a devolver texto si el brief no pasó el verificador. Un JSON con
    ``veredicto: FAIL`` es un dato que el consumidor puede interpretar; un texto plano
    con una cifra sin respaldo es una cifra que un asesor va a leer en voz alta.

    Raises:
        ErrorApi: ``409`` si el brief no está verificado o la cadena no valida.
    """
    resultado = _paquete_de(auditoria, identidad, context_ref)
    if not resultado.apto_para_entregar:
        verificacion = resultado.verificacion_brief
        raise ErrorApi(
            codigo="BRIEF_NO_VERIFICADO",
            detalle=(
                "el brief no puede entregarse como texto: hay cifras sin anclar o la "
                "cadena de auditoría no valida"
            ),
            estado=409,
            datos={
                "context_ref": context_ref,
                "no_ancladas": verificacion.no_ancladas if verificacion else [],
                "cadena_valida": resultado.evidencia.cadena_valida,
            },
        )
    return resultado.a_texto_plano()


@router.get(
    "/conversacion/{conversation_id}",
    response_model=EstadoSala,
    summary="Estado de la sala y transcripción completa",
)
def estado(
    conversation_id: str,
    identidad: _AsesorDep,
    memoria: MemoriaDep,
) -> EstadoSala:
    """Devuelve el estado y todos los turnos.

    Es lo que sondea la consola del asesor. Se eligió sondeo y no un canal permanente
    porque para una demostración el resultado visible es idéntico y la mitad de piezas
    pueden fallar. El salto a eventos del servidor no cambia este contrato.
    """
    return _sala(memoria, conversation_id)
