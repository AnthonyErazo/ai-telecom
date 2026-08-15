"""``POST /v1/explicar`` — el flujo completo de la explicación de recibo.

Orquesta, en este orden y con una entrada de auditoría por paso:

.. code-block:: text

    REQUEST  → construir FactSet → FACTS_BUILT → INVARIANTE
             ├─ invariante roto ──────────────► ROUTE(derivar) → RESPONSE → CHAIN
             └─ RETRIEVE → ROUTE → LLM_CALL → VERIFY → CITATIONS → RESPONSE → CHAIN

Decisiones que fija la especificación y que aquí se cumplen literalmente:

* **El invariante manda.** Si ``|residual_cent| > 1`` no se explica: se deriva y se
  responde con un bloque de aviso. Nunca una "explicación aproximada".
* **El LLM caído no es un error.** La tabla de la sección 9 anota ``424`` junto a este
  endpoint, pero aclara que degradar a plantilla *"**no** es error"*: se responde ``200``
  con ``gobernanza.modo = "PLANTILLA"`` y la cabecera ``X-Degradado``. Devolver ``424``
  dejaría al cliente sin respuesta cuando el sistema sí sabe responder.
* **Ninguna cifra sin anclar sale de aquí.** Todo texto entregado pasa por el
  verificador contra ``ALLOWED``, construido solo desde el FactSet. Si el texto cambia
  después de generarse —por ejemplo al añadir el bloque puente—, se **vuelve a
  verificar** antes de responder.
* **El ``account_ref`` sale del token**, jamás del cuerpo. El ``utterance`` entra al
  prompt como dato delimitado, nunca como instrucción.
* **El bloque puente** (gráfico de cascada previo → actual) se construye desde las
  causas agregadas del FactSet: cada barra es un importe entero del FactSet, así que es
  anclado por construcción.

Dos vías, un solo contrato
--------------------------
Desde que existe la capa de orquestación hay **dos** implementaciones del mismo turno, y
la variable ``ORQUESTADOR`` elige cuál conduce:

* ``grafo`` (por defecto) — :func:`_explicar_con_grafo` delega en
  :mod:`packages.orquestacion`: el mismo recorrido como grafo explícito de LangGraph,
  con *checkpointer* persistente.
* ``directo`` — :func:`_explicar_directo`, la función lineal de siempre, que no importa
  nada de LangGraph.

Las dos producen la **misma** ``RespuestaCanalAgnostica``, las mismas cabeceras y la
misma secuencia de eventos en la bitácora; ``tests/unit/test_grafo.py`` lo comprueba
turno a turno comparando el ``model_dump(mode="json")`` completo. No es redundancia
decorativa: si el grafo fallara delante del jurado, se conmuta con una variable de
entorno en lugar de con un despliegue.

El grafo **no reimplementa** nada de este módulo: sus nodos llaman a
:func:`_asegurar_puente`, :func:`_payload_verify`, :func:`_gobernanza_sin_cifras`,
:func:`evaluar_cross_selling`, :func:`_cerrar` y a las tablas de copia de más abajo. Por
eso el import de ``packages.orquestacion`` es **diferido**: a nivel de módulo sería un
ciclo de importación.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from apps.api.acl import RepositorioCuentas
from apps.api.deps import (
    AdversarioDep,
    AjustesDep,
    AuditoriaDep,
    EstadoAdversario,
    MemoriaConversaciones,
    MemoriaDep,
    ProveedorDep,
    RecuperadorDep,
    RegistroExplicacion,
    ReglasDep,
    RepositorioDep,
    TelemetriaDep,
    historial_para_score,
    nuevo_trace_id,
)
from apps.api.errores import ErrorApi
from apps.api.routers.derivacion import (
    construir_contexto_derivacion,
    construir_resumen_asesor,
    nuevo_context_ref,
)
from apps.api.routers.hechos import construir_hechos, payload_facts_built
from apps.api.security import Identidad, cuenta_autorizada, redactar_para_nivel, requiere_nivel
from apps.api.settings import Ajustes
from packages.core_domain.enums import (
    AccionSiguiente,
    Canal,
    EtapaAuditoria,
    ModoGeneracion,
    MotivoDerivacion,
    NivelAseguramiento,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import (
    Accion,
    BarraPuente,
    Bloque,
    BloqueAviso,
    BloquePuente,
    BloqueTexto,
    Derivacion,
    Gobernanza,
    PeticionExplicacion,
    RespuestaCanalAgnostica,
)
from packages.core_domain.reglas import ConfiguracionReglas
from packages.facts_engine.confianza import Turno, evaluar_incomprension
from packages.facts_engine.intencion import Intencion, ResultadoIntencion, clasificar_intencion
from packages.governance.auditoria import RegistroAuditoria, formatear_para_terminal
from packages.governance.telemetria import RegistroTelemetria
from packages.llm_layer.conversacional import (
    ResultadoConversacional,
    generar_respuesta_conversacional,
)
from packages.llm_layer.generador import ETIQUETAS_ACCION, ResultadoGeneracion, generar_explicacion
from packages.llm_layer.providers.base import ProveedorLLM
from packages.llm_layer.verificador import (
    ConjuntoPermitido,
    ResultadoVerificacion,
    construir_permitidos,
    inyectar_alucinacion,
    verificar,
)
from packages.retriever import ContextoRecuperado, Recuperador, recuperar

__all__ = [
    "construir_bloque_puente",
    "evaluar_cross_selling",
    "registrar_expediente_derivacion",
    "router",
]

#: Cuánto texto de la respuesta se copia a la bitácora. La auditoría necesita poder
#: comprobar QUÉ se le dijo al cliente, pero guardar la respuesta íntegra en cada evento
#: multiplicaría el tamaño de la cadena por poco valor probatorio: con el arranque basta
#: para identificar la respuesta, y el resto es reconstruible desde el `FactSet` sellado.
MAX_TEXTO_BITACORA = 2000


_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["explicar"])

#: Texto del aviso cuando el recibo no concilia. No lleva ni una cifra a propósito.
AVISO_INVARIANTE = (
    "Revisando su recibo encuentro que el detalle no cuadra con la diferencia total, "
    "así que prefiero no darle una explicación que podría estar equivocada. Ya dejé el "
    "caso con toda la información para que un asesor lo revise y le confirme el detalle."
)

#: Texto cuando el verificador caza una cifra sin respaldo. Tampoco lleva cifras.
AVISO_VERIFICACION = (
    "Prefiero no darle un número que no pueda sustentar con su recibo. Dejo su consulta "
    "con un asesor para que le confirme el detalle exacto."
)


# --------------------------------------------------------------------------- #
# Bloque puente
# --------------------------------------------------------------------------- #
def construir_bloque_puente(factset: FactSet) -> BloquePuente | None:
    """Gráfico de cascada del recibo previo al actual, causa a causa.

    Es la pieza que responde "¿por qué me vino más caro?" de un vistazo: barra de
    entrada con el recibo anterior, una barra por **causa agregada** (en el vocabulario
    de la ficha: *cambio de plan*, *prorrateos*, *reconexiones*…) y barra de total con
    el recibo actual.

    Todas las barras llevan importes enteros que ya están en el FactSet, de modo que el
    bloque es anclado por construcción y no puede introducir una cifra nueva.

    Returns:
        El bloque, o ``None`` si no hay causas agregadas que dibujar.
    """
    if not factset.causas_agregadas:
        return None
    barras = [
        BarraPuente(
            etiqueta=f"Recibo de {factset.periodo_previo}",
            monto_cent=factset.total_previo_cent,
            tipo="entrada",
            fact_id="factset:total_previo_cent",
        )
    ]
    for causa in factset.causas_agregadas:
        clave = causa.causa or causa.causa_oficial or "SIN_CAUSA"
        barras.append(
            BarraPuente(
                etiqueta=causa.etiqueta_cliente.capitalize(),
                monto_cent=causa.monto_cent,
                tipo="incremento" if causa.monto_cent >= 0 else "decremento",
                fact_id=f"causa:{clave}.monto_cent",
            )
        )
    barras.append(
        BarraPuente(
            etiqueta=f"Recibo de {factset.periodo_actual}",
            monto_cent=factset.total_actual_cent,
            tipo="total",
            fact_id="factset:total_actual_cent",
        )
    )
    return BloquePuente(
        titulo="De un mes a otro",
        barras=barras,
        fact_ids=["factset:total_previo_cent", "factset:total_actual_cent"],
    )


def _asegurar_puente(
    factset: FactSet,
    resultado: ResultadoGeneracion,
    permitidos: ConjuntoPermitido,
    *,
    estricto: bool | None,
) -> tuple[list[Bloque], ResultadoVerificacion]:
    """Garantiza que la respuesta lleva el bloque puente, sin romper el anclaje.

    La capa generativa ya lo incluye cuando hay causas agregadas; esta función cubre el
    caso en que no lo hiciera. Como añadir un bloque cambia el texto que se entrega
    —y por tanto lo que audita el verificador—, **se vuelve a verificar** el texto
    resultante. Si la verificación empeorase, se devuelve el original: nunca se entrega
    un texto peor verificado que el que ya se tenía.
    """
    bloques = list(resultado.bloques)
    if any(bloque.tipo == "puente" for bloque in bloques):
        return bloques, resultado.verificacion
    puente = construir_bloque_puente(factset)
    if puente is None:
        return bloques, resultado.verificacion

    posicion = next((i for i, bloque in enumerate(bloques) if bloque.tipo == "kv"), -1) + 1
    candidatos: list[Bloque] = [*bloques[:posicion], puente, *bloques[posicion:]]
    texto = "\n".join(bloque.a_texto() for bloque in candidatos)
    nueva = verificar(
        texto,
        factset,
        permitidos=permitidos,
        salida_llm=resultado.explicacion,
        estricto=estricto,
    )
    if nueva.veredicto is VeredictoVerificacion.FAIL:
        _LOG.warning(
            "el bloque puente introdujo cifras no ancladas (%s); se responde sin él",
            nueva.infractores,
        )
        return bloques, resultado.verificacion
    return candidatos, nueva


# --------------------------------------------------------------------------- #
# Cross-selling restrictivo
# --------------------------------------------------------------------------- #
def evaluar_cross_selling(
    factset: FactSet,
    reglas: ConfiguracionReglas,
    *,
    resuelta: bool,
    derivar: bool,
) -> Accion | None:
    """Decide si procede ofrecer alternativas comerciales, con la doble condición.

    Literal de la ficha: el cross-selling se activa *"única y exclusivamente si el
    modelo clasifica la consulta original como RESUELTA POSITIVAMENTE y existe una REGLA
    DE NEGOCIO EXPLÍCITA que lo habilite"*. Aquí se comprueban las dos, más las guardas
    de ``rules.yaml`` (confianza mínima, prohibido si hay derivación).

    La acción resultante **no lleva texto ni importes**: es un botón
    ``VER_ALTERNATIVAS``. Una oferta con cifras tendría que pasar por el verificador y
    sus números no están en el FactSet; convertirla en acción evita el problema de raíz.

    Returns:
        La acción sugerida, o ``None`` si alguna condición no se cumple.
    """
    configuracion = reglas.cross_selling
    if not configuracion.habilitado:
        return None
    if configuracion.requiere_consulta_resuelta and not resuelta:
        return None
    if configuracion.prohibido_si_derivacion and derivar:
        return None
    if factset.confianza_global < configuracion.confianza_minima:
        return None
    if configuracion.prohibido_si_delta_negativo and factset.delta_total_cent < 0:
        return None
    if configuracion.requiere_regla_explicita and not configuracion.reglas_explicitas:
        return None

    conceptos = {linea.concepto_id for linea in factset.lineas}
    causas = {linea.causa for linea in factset.lineas if linea.causa is not None}
    for regla in configuracion.reglas_explicitas:
        if regla.requiere_conceptos and not set(regla.requiere_conceptos).issubset(conceptos):
            continue
        if regla.requiere_causas and not set(regla.requiere_causas).issubset(causas):
            continue
        etiqueta, riesgo = ETIQUETAS_ACCION[AccionSiguiente.VER_ALTERNATIVAS]
        return Accion(
            id=AccionSiguiente.VER_ALTERNATIVAS,
            etiqueta=etiqueta,
            riesgo=riesgo,  # type: ignore[arg-type]
            payload={"regla": regla.id, "motivo": regla.descripcion},
        )
    return None


# --------------------------------------------------------------------------- #
# Respuestas de corte
# --------------------------------------------------------------------------- #
def _gobernanza_sin_cifras(
    factset: FactSet, *, modelo: str, modo: ModoGeneracion = ModoGeneracion.PLANTILLA
) -> Gobernanza:
    """Gobernanza de una respuesta que no contiene ninguna cifra.

    ``NO_APLICA`` no es un aprobado por la puerta de atrás: significa que no hubo
    aserciones numéricas que verificar, y ``anclado=True`` se sostiene porque un texto
    sin cifras no puede contener una cifra inventada.
    """
    return Gobernanza(
        anclado=True,
        verificacion_numerica="NO_APLICA",
        aserciones_totales=0,
        aserciones_ancladas=0,
        aserciones_no_ancladas=0,
        confianza=factset.confianza_global,
        modo=modo,
        rules_version=factset.rules_version,
        model_version=modelo,
        factset_sha256=factset.sha256,
    )


def _payload_retrieve(contexto: ContextoRecuperado | None) -> dict[str, Any]:
    """Payload del evento ``RETRIEVE`` en el vocabulario que espera la bitácora.

    ``ContextoRecuperado.resumen_auditoria()`` publica listas (``faqs``, ``casuisticas``,
    ``conceptos``) y la sección 7 pide contadores con nombre en singular (``faq``,
    ``casuistica``, ``catalogo``, ``saneado``). Se envían ambos: el detalle para poder
    reconstruir qué documentos se usaron y los contadores para la vista de terminal.
    """
    if contexto is None:
        return {
            "faq": 0,
            "casuistica": 0,
            "catalogo": 0,
            "documentos": 0,
            "saneado": True,
            "disponible": False,
        }
    payload = dict(contexto.resumen_auditoria())
    payload.update(
        {
            "faq": len(contexto.faqs),
            "casuistica": len(contexto.casuisticas),
            "catalogo": len(contexto.conceptos),
            "documentos": len(contexto.fragmentos),
            # Garantía por construcción del retriever: ningún texto sale con dígitos.
            "saneado": True,
            "disponible": True,
        }
    )
    return payload


def _payload_verify(
    verificacion: ResultadoVerificacion, adversaria: dict[str, Any] | None
) -> dict[str, Any]:
    """Payload del evento ``VERIFY``: la lista completa de aserciones con su estado.

    Dos ajustes sobre ``ResultadoVerificacion.a_evento_auditoria()``, ambos de contrato:

    1. Se añaden los contadores con los nombres que pide la sección 7
       (``aserciones_ancladas``/``derivadas``/``no_ancladas``).
    2. Se renombra ``token`` a ``token_normalizado`` en cada aserción. ``token`` está en
       ``CLAVES_SENSIBLES`` del registro —donde significa *credencial*— y se redactaría,
       borrando justo la evidencia que este evento existe para conservar: el token
       numérico (``cent:2082``) contra el que se ancló la cifra.
    """
    payload = dict(verificacion.a_evento_auditoria())
    payload["aserciones"] = [
        {
            **{clave: valor for clave, valor in asercion.items() if clave != "token"},
            "token_normalizado": asercion.get("token"),
        }
        for asercion in payload.get("aserciones", [])
    ]
    payload.update(
        {
            "aserciones_ancladas": verificacion.ancladas,
            "aserciones_derivadas": verificacion.derivadas,
            "aserciones_no_ancladas": verificacion.no_ancladas,
            "derivaciones": [derivacion.a_dict() for derivacion in verificacion.derivaciones],
            "adversaria": adversaria,
        }
    )
    return payload


def _acciones_de_corte() -> list[Accion]:
    """Acciones cuando no se puede explicar: hablar con asesor o registrar la consulta."""
    acciones = []
    for identificador in (AccionSiguiente.DERIVAR_ASESOR, AccionSiguiente.REGISTRAR_CONSULTA):
        etiqueta, riesgo = ETIQUETAS_ACCION[identificador]
        acciones.append(Accion(id=identificador, etiqueta=etiqueta, riesgo=riesgo))  # type: ignore[arg-type]
    return acciones


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
def _cerrar(
    auditoria: RegistroAuditoria,
    trace_id: str,
    cuenta: str,
    *,
    imprimir: bool,
) -> None:
    """Cierra el turno en la bitácora e imprime el resumen de seis líneas.

    La métrica oficial de la ficha es *"cero invenciones financieras **comprobables
    mediante logs de la terminal**"*: esa impresión es la prueba, no un adorno.
    """
    auditoria.cerrar_turno(trace_id, cuenta_ref=cuenta)
    if imprimir:
        try:
            print(formatear_para_terminal(auditoria.leer(trace_id), trace_id))
        except (OSError, ValueError) as error:  # pragma: no cover - nunca debe cortar el turno
            _LOG.warning("no se pudo pintar el resumen del turno %s: %s", trace_id, error)


@router.post(
    "/explicar",
    summary="Explica la variación del recibo (respuesta canal-agnóstica, verificada)",
    response_model=RespuestaCanalAgnostica,
)
def explicar_recibo(
    peticion: PeticionExplicacion,
    http: Response,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA1))],
    ajustes: AjustesDep,
    repositorio: RepositorioDep,
    reglas: ReglasDep,
    recuperador: RecuperadorDep,
    proveedor: ProveedorDep,
    auditoria: AuditoriaDep,
    memoria: MemoriaDep,
    telemetria: TelemetriaDep,
    adversario: AdversarioDep,
) -> RespuestaCanalAgnostica:
    """Explica por qué varió el recibo del periodo pedido.

    Args:
        peticion: cuerpo con ``conversation_id``, ``periodo``, ``verbosidad``,
            ``utterance`` y ``canal``. El ``cuenta_id`` del cuerpo es redundante: la
            cuenta sale del token y, si no coincide, se responde ``403``.

    Returns:
        La :class:`RespuestaCanalAgnostica` con bloques, acciones, derivación y
        gobernanza. En ``LOA1`` se devuelve redactada, sin ningún importe.
    """
    # Este cuerpo es solo el conmutador de `ORQUESTADOR`: `grafo` (por defecto) delega
    # en `packages.orquestacion`, `directo` usa la función lineal de siempre. Las dos
    # devuelven la misma respuesta y escriben la misma bitácora.
    #
    # La explicación va en un comentario y NO en el docstring a propósito: FastAPI
    # publica el docstring como `description` de la operación en `/openapi.json`, y ahí
    # solo tiene que aparecer lo que le importa a quien consume la API. Qué motor
    # conduce el turno por dentro no es parte del contrato —de hecho el snapshot de
    # `tests/contract/test_openapi_snapshot.py` es la prueba de que no cambió—.
    # Un asesor humano dentro de la sala es una PRECONDICIÓN del turno, no un paso del
    # flujo: si hay una persona atendiendo, ninguna de las dos rutas debe explicar nada.
    # Por eso se comprueba aquí y no dentro del grafo ni de la función lineal — así
    # ambas vías se comportan igual sin duplicar la regla.
    clave_conversacion = str(peticion.conversation_id or uuid.uuid4())
    asesor_en_sala = memoria.asesor_presente(clave_conversacion)
    if asesor_en_sala:
        trace_id = nuevo_trace_id()
        memoria.anotar_turno(
            clave_conversacion, Turno(utterance=peticion.utterance, rol="cliente")
        )
        auditoria.emitir(
            EtapaAuditoria.ROUTE,
            trace_id,
            {
                "etapa": "asesor",
                "evento": "ASISTENTE_EN_COPILOTO",
                "asesor": asesor_en_sala,
                "modo": "ASISTIDA",
                "respondio_la_ia": False,
            },
            **identidad.contexto_auditoria(),
        )
        return _responder_en_copiloto(
            asesor=asesor_en_sala,
            trace_id=trace_id,
            conversacion=uuid.UUID(clave_conversacion),
            reglas=reglas,
            auditoria=auditoria,
            contexto_auditoria=identidad.contexto_auditoria(),
        )

    conducir = _explicar_con_grafo if ajustes.usa_grafo else _explicar_directo
    return conducir(
        peticion=peticion,
        http=http,
        identidad=identidad,
        ajustes=ajustes,
        repositorio=repositorio,
        reglas=reglas,
        recuperador=recuperador,
        proveedor=proveedor,
        auditoria=auditoria,
        memoria=memoria,
        telemetria=telemetria,
        adversario=adversario,
    )


def _explicar_con_grafo(
    *,
    peticion: PeticionExplicacion,
    http: Response,
    identidad: Identidad,
    ajustes: Ajustes,
    repositorio: RepositorioCuentas,
    reglas: ConfiguracionReglas,
    recuperador: Recuperador | None,
    proveedor: ProveedorLLM | None,
    auditoria: RegistroAuditoria,
    memoria: MemoriaConversaciones,
    telemetria: RegistroTelemetria,
    adversario: EstadoAdversario,
) -> RespuestaCanalAgnostica:
    """Conduce el turno con :mod:`packages.orquestacion` (``ORQUESTADOR=grafo``).

    Lo único que se hace aquí es lo que **no** puede hacer el grafo: resolver la cuenta
    desde el token, abrir la traza y poner las cabeceras. El resto —bitácora, hechos,
    recuperación, generación, verificación y derivación— lo ejecutan los nodos, que
    llaman exactamente a las mismas funciones que :func:`_explicar_directo`.

    El ``thread_id`` del *checkpointer* es el ``conversation_id``: un hilo persistente
    por conversación, que es lo que permite inspeccionar el turno y —a futuro—
    reanudarlo.

    El import es diferido a propósito: ``packages.orquestacion.nodos`` reutiliza las
    piezas privadas de este módulo, así que un ``import`` mutuo arriba sería un ciclo.
    """
    from packages.orquestacion import Servicios, ejecutar_turno, estado_inicial

    cuenta = cuenta_autorizada(identidad, peticion.cuenta_id)
    trace_id = nuevo_trace_id()
    conversacion = peticion.conversation_id or uuid.uuid4()
    canal = peticion.canal or identidad.canal or Canal.APP
    http.headers["X-Trace-Id"] = trace_id

    final = ejecutar_turno(
        estado_inicial(
            trace_id=trace_id,
            conversation_id=str(conversacion),
            cuenta=cuenta,
            canal=canal,
            nivel=identidad.acr,
            contexto_auditoria=identidad.contexto_auditoria(),
            utterance=peticion.utterance,
            verbosidad=peticion.verbosidad,
            periodo=peticion.periodo,
        ),
        str(conversacion),
        Servicios(
            ajustes=ajustes,
            repositorio=repositorio,
            reglas=reglas,
            recuperador=recuperador,
            proveedor=proveedor,
            auditoria=auditoria,
            memoria=memoria,
            telemetria=telemetria,
            adversario=adversario,
        ),
    )

    respuesta = final.get("respuesta")
    if respuesta is None:  # pragma: no cover - todo camino del grafo deja respuesta
        raise ErrorApi(
            500,
            "ORQUESTACION_SIN_RESPUESTA",
            "el grafo terminó sin producir respuesta",
            datos={"nodos": list(final.get("nodos", ()))},
        )
    # La cabecera se pone en el **mismo** caso que en la vía directa: solo cuando se
    # entregó la explicación completa con plantilla. En los cortes duros no se pone,
    # porque allí la plantilla no es una degradación del modelo sino la regla.
    if final.get("corte") is None and final.get("degradado"):
        http.headers["X-Degradado"] = "PLANTILLA"
    return respuesta


def _explicar_directo(
    *,
    peticion: PeticionExplicacion,
    http: Response,
    identidad: Identidad,
    ajustes: Ajustes,
    repositorio: RepositorioCuentas,
    reglas: ConfiguracionReglas,
    recuperador: Recuperador | None,
    proveedor: ProveedorLLM | None,
    auditoria: RegistroAuditoria,
    memoria: MemoriaConversaciones,
    telemetria: RegistroTelemetria,
    adversario: EstadoAdversario,
) -> RespuestaCanalAgnostica:
    """El turno completo escrito como una función lineal (``ORQUESTADOR=directo``).

    Es la implementación que llevaba verde antes de que existiera la capa de
    orquestación y se conserva **entera y viva** como respaldo de un solo interruptor.
    No importa nada de LangGraph, así que sirve también para diagnosticar: si un fallo
    aparece en ``grafo`` y no aquí, es de la orquestación y no del motor.
    """
    cuenta = cuenta_autorizada(identidad, peticion.cuenta_id)
    trace_id = nuevo_trace_id()
    conversacion = peticion.conversation_id or uuid.uuid4()
    clave_conversacion = str(conversacion)
    canal = peticion.canal or identidad.canal or Canal.APP
    contexto_auditoria = identidad.contexto_auditoria()
    http.headers["X-Trace-Id"] = trace_id

    auditoria.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "POST /v1/explicar",
            "periodo": peticion.periodo,
            "canal": str(canal),
            "nivel": str(identidad.acr),
            "verbosidad": str(peticion.verbosidad),
            "utterance": peticion.utterance,
            "conversation_id": clave_conversacion,
        },
        **contexto_auditoria,
    )
    # Un turno nuevo del cliente resuelve las sondas de silencio que siguieran abiertas.
    telemetria.registrar_turno_usuario(conversacion, peticion.utterance)

    # --- 0. Intención ------------------------------------------------------ #
    # No todo lo que escribe el cliente significa «explícame el recibo». Antes de
    # tocar la facturación se decide si corresponde explicar. Sin esta compuerta,
    # un «hola» devolvía el recibo completo y «quiero cancelar mi servicio»
    # también, saltándose una regla de cumplimiento regulatorio.
    intencion = clasificar_intencion(peticion.utterance)
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "etapa": "intencion",
            "intencion": str(intencion.intencion),
            "patron": intencion.patron,
            "explica_recibo": intencion.explica_recibo,
            "deriva": intencion.deriva,
            "motivo_codigo": str(intencion.motivo_derivacion) if intencion.deriva else None,
        },
        **contexto_auditoria,
    )
    if not intencion.explica_recibo:
        memoria.anotar_turno(
            clave_conversacion, Turno(utterance=peticion.utterance, rol="cliente")
        )
        return _responder_por_intencion(
            intencion=intencion,
            trace_id=trace_id,
            conversacion=conversacion,
            cuenta=cuenta,
            canal=canal,
            peticion=peticion,
            identidad=identidad,
            auditoria=auditoria,
            telemetria=telemetria,
            reglas=reglas,
            proveedor=proveedor,
            memoria=memoria,
            ajustes=ajustes,
            contexto_auditoria=contexto_auditoria,
        )

    # --- 1. Hechos -------------------------------------------------------- #
    factset, datos = construir_hechos(
        repositorio, reglas, cuenta, peticion.periodo, trace_id=trace_id
    )
    auditoria.emitir(
        EtapaAuditoria.FACTS_BUILT,
        trace_id,
        payload_facts_built(factset, datos),
        **contexto_auditoria,
    )
    auditoria.emitir(
        EtapaAuditoria.INVARIANTE,
        trace_id,
        {
            "ok": factset.invariante.ok,
            "residual_cent": factset.invariante.residual_cent,
            "suma_deltas_cent": factset.invariante.suma_deltas_cent,
            "delta_total_cent": factset.invariante.delta_total_cent,
        },
        **contexto_auditoria,
    )

    historial = historial_para_score(memoria, clave_conversacion, peticion.utterance)
    memoria.anotar_turno(clave_conversacion, Turno(utterance=peticion.utterance, rol="cliente"))

    # --- 2. Invariante roto: no se explica, se deriva ---------------------- #
    if not factset.invariante.ok:
        return _responder_derivando(
            factset=factset,
            motivo_codigo=MotivoDerivacion.INVARIANTE_ROTO,
            motivo="el detalle del recibo no cuadra con la diferencia entre totales",
            senal=f"invariante.residual_cent={factset.invariante.residual_cent}",
            aviso=AVISO_INVARIANTE,
            severidad="critico",
            trace_id=trace_id,
            conversacion=conversacion,
            cuenta=cuenta,
            canal=canal,
            peticion=peticion,
            identidad=identidad,
            auditoria=auditoria,
            memoria=memoria,
            telemetria=telemetria,
            ajustes=ajustes,
            contexto_auditoria=contexto_auditoria,
        )

    # --- 3. Contexto recuperado (RAG) -------------------------------------- #
    contexto: ContextoRecuperado | None = None
    if recuperador is not None:
        try:
            contexto = recuperar(factset, peticion.utterance, k=5, recuperador=recuperador)
        except Exception as error:
            _LOG.warning("el retriever falló (%s); se explica sin contexto", error)
    auditoria.emitir(
        EtapaAuditoria.RETRIEVE, trace_id, _payload_retrieve(contexto), **contexto_auditoria
    )

    # --- 4. Umbral de incomprensión ---------------------------------------- #
    fuera_catalogo = list(contexto.conceptos_fuera_catalogo) if contexto else []
    incomprension = evaluar_incomprension(
        factset,
        historial,
        peticion.utterance,
        reglas=reglas,
        derivado_previamente=memoria.fue_derivada(clave_conversacion),
        # La histéresis la activa una persona dentro de la sala, no el recuerdo de un
        # score que subió una vez.
        asesor_en_sala=memoria.asesor_presente(clave_conversacion) is not None,
        conceptos_fuera_catalogo=fuera_catalogo,
    )
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "derivar": incomprension.derivar,
            "motivo_codigo": str(incomprension.motivo) if incomprension.motivo else None,
            "score_incomprension": round(incomprension.U, 4),
            "modo": "SCORE",
            "reglas_disparadas": list(incomprension.reglas_disparadas),
            "senal_disparadora": incomprension.senal_disparadora,
            # Las dos magnitudes por separado, para que la bitácora del router y la del
            # grafo sean comparables evento a evento.
            "desglose": incomprension.s1_cobertura,
            "cobertura_causal": incomprension.cobertura_causal,
            "ofrece_asesor": incomprension.ofrecer_asesor,
        },
        **contexto_auditoria,
    )

    # --- 5. Generación verificada ------------------------------------------ #
    permitidos = construir_permitidos(factset)
    resultado = generar_explicacion(
        factset,
        contexto_recuperado=contexto.fragmentos if contexto else None,
        utterance=peticion.utterance,
        verbosidad=peticion.verbosidad,
        proveedor=proveedor,
        canal=canal,
        estricto=ajustes.verificador_estricto,
        timeout_s=ajustes.llm_timeout_s,
        permitidos=permitidos,
    )
    degradado = resultado.modo is ModoGeneracion.PLANTILLA and proveedor is not None
    for intento in resultado.intentos:
        codigo_error = (intento.error or {}).get("codigo")
        auditoria.emitir(
            EtapaAuditoria.LLM_CALL,
            trace_id,
            {
                "proveedor": intento.proveedor,
                "model_version": resultado.model_version,
                "latencia_ms": intento.latencia_ms,
                "intento": intento.numero,
                # `timeout` es la BANDERA de que el intento se agotó, no el ajuste: así
                # lo lee la vista de terminal. El valor configurado va en `timeout_s`.
                "timeout": codigo_error == "TIEMPO_AGOTADO",
                "timeout_s": ajustes.llm_timeout_s,
                "modo": str(intento.modo),
                "veredicto": intento.veredicto,
                "no_ancladas": intento.no_ancladas,
                "infractores": intento.infractores,
                "error": intento.error,
            },
            **contexto_auditoria,
        )

    bloques, verificacion = _asegurar_puente(
        factset, resultado, permitidos, estricto=ajustes.verificador_estricto
    )

    # --- 6. Demo adversaria (solo si /dev/alucinar la activó) -------------- #
    adversaria: dict[str, Any] | None = None
    if adversario.consumir():
        texto_envenenado = inyectar_alucinacion(
            "\n".join(bloque.a_texto() for bloque in bloques),
            factset,
            delta_cent=adversario.delta_cent,
        )
        veredicto_adv = verificar(
            texto_envenenado, factset, permitidos=permitidos, estricto=ajustes.verificador_estricto
        )
        adversaria = {
            "activo": True,
            "veredicto": str(veredicto_adv.veredicto),
            "infractores": list(veredicto_adv.infractores),
            "no_ancladas": veredicto_adv.no_ancladas,
            "terminal": veredicto_adv.lineas_terminal(),
        }
        _LOG.error(
            "DEMO ADVERSARIA: alucinación inyectada y detectada: %s", veredicto_adv.infractores
        )
        verificacion = veredicto_adv

    # --- 7. Verificación y citas ------------------------------------------- #
    auditoria.emitir(
        EtapaAuditoria.VERIFY,
        trace_id,
        _payload_verify(verificacion, adversaria),
        **contexto_auditoria,
    )
    auditoria.emitir(
        EtapaAuditoria.CITATIONS,
        trace_id,
        {
            "citas": [cita.model_dump(mode="json") for cita in verificacion.citas],
            "fact_ids": sorted({cita.fact_id for cita in verificacion.citas}),
        },
        **contexto_auditoria,
    )

    # --- 8. Si el texto no está anclado, no sale ---------------------------- #
    if verificacion.veredicto is VeredictoVerificacion.FAIL or resultado.bloqueada:
        return _responder_derivando(
            factset=factset,
            motivo_codigo=MotivoDerivacion.VERIFICACION_FALLIDA,
            motivo="no se pudo anclar toda la explicación numérica en el recibo",
            senal=f"no_ancladas={verificacion.no_ancladas}",
            aviso=AVISO_VERIFICACION,
            severidad="advertencia",
            trace_id=trace_id,
            conversacion=conversacion,
            cuenta=cuenta,
            canal=canal,
            peticion=peticion,
            identidad=identidad,
            auditoria=auditoria,
            memoria=memoria,
            telemetria=telemetria,
            ajustes=ajustes,
            contexto_auditoria=contexto_auditoria,
            verificacion=verificacion,
            adversaria=adversaria,
        )

    # --- 9. Derivación por umbral de incomprensión ------------------------- #
    derivacion = Derivacion()
    context_ref: str | None = None
    if incomprension.derivar:
        context_ref = nuevo_context_ref(clave_conversacion, trace_id)
        resumen_asesor = construir_resumen_asesor(
            factset,
            cuenta_id=cuenta,
            motivo_codigo=incomprension.motivo or MotivoDerivacion.UMBRAL_INCOMPRENSION,
            utterance=peticion.utterance,
            canal=str(canal),
            verificacion=str(verificacion.veredicto),
            modo=str(resultado.modo),
            detalle_motivo=incomprension.senal_disparadora,
        )
        derivacion = incomprension.a_derivacion(
            context_ref=context_ref, resumen_asesor=resumen_asesor
        )
        memoria.guardar_contexto(
            context_ref,
            construir_contexto_derivacion(
                context_ref=context_ref,
                trace_id=trace_id,
                conversation_id=clave_conversacion,
                cuenta_id=cuenta,
                factset=factset,
                motivo_codigo=incomprension.motivo or MotivoDerivacion.UMBRAL_INCOMPRENSION,
                resumen_asesor=resumen_asesor,
                utterance=peticion.utterance,
                canal=str(canal),
                extra={"score_incomprension": round(incomprension.U, 4)},
            ),
        )
        memoria.marcar_derivada(clave_conversacion)

    # --- 10. Acciones y respuesta ------------------------------------------ #
    acciones = list(resultado.acciones)
    # Derivar y decírselo al cliente son cosas distintas. La derivación ocurre igual y en
    # silencio —el caso entra en la cola con su expediente—; el botón solo se pinta si el
    # asesor lo pidió el cliente o si hay un reclamo formal de por medio.
    if incomprension.asesor_a_la_vista and all(
        accion.id is not AccionSiguiente.DERIVAR_ASESOR for accion in acciones
    ):
        etiqueta, riesgo = ETIQUETAS_ACCION[AccionSiguiente.DERIVAR_ASESOR]
        acciones.insert(0, Accion(id=AccionSiguiente.DERIVAR_ASESOR, etiqueta=etiqueta, riesgo=riesgo))  # type: ignore[arg-type]
    oferta = evaluar_cross_selling(
        factset,
        reglas,
        resuelta=verificacion.veredicto is VeredictoVerificacion.PASS,
        derivar=incomprension.derivar,
    )
    if oferta is not None and all(accion.id is not oferta.id for accion in acciones):
        acciones.append(oferta)

    gobernanza = resultado.gobernanza.model_copy(
        update={
            "anclado": verificacion.anclado,
            "verificacion_numerica": str(verificacion.veredicto),
            "aserciones_totales": verificacion.aserciones_totales,
            "aserciones_ancladas": verificacion.ancladas + verificacion.derivadas,
            "aserciones_no_ancladas": verificacion.no_ancladas,
            "citas": verificacion.citas,
            "aserciones": verificacion.aserciones,
        }
    )

    sonda = telemetria.abrir_sonda(
        conversacion,
        trace_id,
        cuenta_ref=cuenta,
        periodo=factset.periodo_actual,
        canal=canal,
        utterance_origen=peticion.utterance,
        causa_dominante=(
            factset.causa_dominante().etiqueta_cliente if factset.causa_dominante() else None
        ),
        derivada=incomprension.derivar,
        verificacion=verificacion.veredicto,
        ventana_s=ajustes.ventana_silencio_s,
    )
    telemetria_turno = resultado.telemetria()
    telemetria_turno.update(sonda.a_telemetria())
    telemetria_turno.update(
        {
            "explicacion_id": trace_id,
            "degradado": degradado,
            "contexto_degradado": bool(contexto.degradado) if contexto else True,
            "score_incomprension": round(incomprension.U, 4),
            # Un turno resuelto con salvedad causal y uno resuelto con la causa
            # documentada no pueden verse iguales.
            "cobertura_causal": incomprension.cobertura_causal,
            "asesor_ofrecido": incomprension.ofrecer_asesor,
            "cross_selling": oferta.payload.get("regla") if oferta else None,
            "firma_causal": factset.firma_causal(),
        }
    )
    if adversaria:
        telemetria_turno["adversaria"] = adversaria

    respuesta = RespuestaCanalAgnostica(
        conversation_id=conversacion,
        trace_id=trace_id,
        bloques=bloques,
        acciones=acciones,
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria=telemetria_turno,
    )
    memoria.guardar_explicacion(
        RegistroExplicacion(
            explicacion_id=trace_id,
            trace_id=trace_id,
            conversation_id=clave_conversacion,
            cuenta_ref=cuenta,
            periodo=factset.periodo_actual,
            factset=factset,
            respuesta=respuesta,
            evidencia=contexto.items_evidencia() if contexto else [],
            utterance=peticion.utterance,
            canal=str(canal),
            contexto_rag=contexto.resumen_auditoria() if contexto else {},
            score_incomprension=round(incomprension.U, 4),
            derivada=incomprension.derivar,
        )
    )
    memoria.anotar_turno(
        clave_conversacion,
        Turno(
            utterance=respuesta.texto[:400],
            rol="asistente",
            progreso=not incomprension.derivar,
            derivado=incomprension.derivar,
        ),
    )

    if degradado:
        # El LLM no respondió y contestó la plantilla: es una degradación anunciada, no
        # un error. El canal puede querer marcar la respuesta, de ahí la cabecera.
        http.headers["X-Degradado"] = "PLANTILLA"

    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "bloques": len(bloques),
            "acciones": len(acciones),
            "modo": str(resultado.modo),
            "derivada": incomprension.derivar,
            "latencia_ms": resultado.latencia_ms,
            "silence_probe_id": str(sonda.silence_probe_id),
            "explicacion_id": trace_id,
            "context_ref": context_ref,
            "degradado": degradado,
            # La bitácora conserva lo que se le dijo al cliente, para que el asesor no se
            # lo repita.
            "texto_entregado": respuesta.texto[:MAX_TEXTO_BITACORA],
        },
        **contexto_auditoria,
    )
    _cerrar(auditoria, trace_id, cuenta, imprimir=ajustes.log_terminal)
    return redactar_para_nivel(respuesta, identidad.acr, factset=factset)


#: Respuesta por intención cuando NO corresponde explicar el recibo.
#: Ninguna de estas frases contiene cifras: el verificador no tiene nada que anclar
#: y ``verificacion_numerica`` queda en ``NO_APLICA``, que es lo honesto.
_COPY_INTENCION: dict[Intencion, tuple[str, str]] = {
    Intencion.SOSPECHOSA: (
        "advertencia",
        "Eso no lo puedo hacer. Yo solo le explico su recibo y sus cargos, y siempre "
        "sobre su propia cuenta. Si tiene una consulta sobre su recibo, dígamela con "
        "sus palabras y con gusto la reviso.",
    ),
    Intencion.SALUDO: (
        "info",
        "Hola, buen día. Soy su asistente de recibos de Movistar. "
        "Puedo explicarle por qué cambió el monto de su recibo, qué significa cada "
        "cargo, o pasarlo con un asesor. ¿Qué necesita?",
    ),
    Intencion.VACIO: (
        "info",
        "No recibí su consulta. Cuénteme qué necesita: puedo explicarle su recibo, "
        "aclararle un cargo o pasarlo con un asesor.",
    ),
    Intencion.FUERA_DE_DOMINIO: (
        "advertencia",
        "Disculpe, sobre eso no le puedo ayudar: solo veo temas de su recibo y de sus "
        "cargos. Si quiere, le explico por qué varió su monto, o lo paso con un asesor "
        "que sí pueda atenderlo.",
    ),
    Intencion.CONSULTA_CONCEPTO: (
        "info",
        "Con gusto le explico ese concepto. Dígame si prefiere que se lo explique en "
        "general o aplicado a su último recibo.",
    ),
    Intencion.REGULATORIA: (
        "advertencia",
        "Entiendo. Ese trámite lo tiene que ver un asesor: yo no puedo tramitarlo ni "
        "darlo por atendido desde aquí. Lo paso con una persona y le dejo cargado todo "
        "el contexto para que no tenga que repetir nada.",
    ),
    Intencion.PEDIR_HUMANO: (
        "info",
        "Por supuesto. Lo paso con un asesor y le dejo cargado el contexto de su "
        "consulta para que no tenga que empezar de cero.",
    ),
    Intencion.DISPUTA_CARGO: (
        "advertencia",
        "Entiendo, y eso no lo resuelvo yo desde aquí. Si un cargo no le corresponde, "
        "lo tiene que revisar una persona. Lo paso con un asesor con todo lo que me ha "
        "contado, para que no tenga que repetirlo.",
    ),
    Intencion.PAGAR: (
        "info",
        "Puede pagar su recibo desde la App Mi Movistar o en los canales autorizados, "
        "con el código de pago que aparece en su recibo. Si además quiere, le explico "
        "por qué le llegó ese monto.",
    ),
    Intencion.CONSUMO: (
        "advertencia",
        "El consumo de sus datos y minutos no lo veo desde aquí: eso lo encuentra en la "
        "App Mi Movistar. Lo mío es su recibo, qué le cobraron y por qué. Si quiere, lo "
        "paso con un asesor que sí pueda revisar su consumo.",
    ),
}

#: Motivo legible que se guarda en la derivación, por intención.
_MOTIVO_INTENCION: dict[Intencion, str] = {
    Intencion.REGULATORIA: (
        "el cliente manifestó una intención con efecto regulatorio o contractual"
    ),
    Intencion.PEDIR_HUMANO: "el cliente pidió expresamente hablar con una persona",
    Intencion.DISPUTA_CARGO: (
        "el cliente impugna un cargo: sostiene que no le corresponde, no que no lo entienda"
    ),
    Intencion.CONSUMO: (
        "el cliente pregunta por consumo (datos, minutos, saldo), que no está en el FactSet"
    ),
}


def registrar_expediente_derivacion(
    memoria: Any,
    *,
    context_ref: str,
    trace_id: str,
    conversation_id: str,
    cuenta: str,
    motivo_codigo: Any,
    resumen_asesor: str,
    utterance: str,
    canal: Any,
) -> None:
    """Deja el expediente en la cola del 104 y marca la conversación como derivada.

    Se extrajo a función compartida porque la derivación por intención ocurre en **dos**
    sitios —la ruta lineal y el nodo del grafo— y solo uno guardaba el expediente: al
    cliente se le anunciaba que lo pasaban con un asesor y el caso no aparecía en
    ninguna cola. Un fallo así no lo detecta ninguna prueba de la respuesta, porque la
    respuesta era correcta; lo que faltaba estaba al otro lado.

    Aquí no hay ``FactSet`` y es correcto: en esta rama no se abrió el recibo. El asesor
    recibe el motivo y lo que dijo el cliente, no cifras que nadie calculó.

    .. warning::
       Esta función deja el expediente en **memoria**, que es un almacén distinto de la
       bitácora. Quien la llame tiene que declarar además el ``context_ref`` en el
       payload de un evento auditado del mismo turno —sus dos llamadores lo hacen en el
       ``RESPONSE``—, porque ``GET /v1/asesor/paquete/{ref}`` resuelve la referencia con
       :func:`~packages.governance.paquete_asesor.traza_de_context_ref`, que solo mira
       los eventos sellados. Sin ese evento el cliente recibe su referencia y el asesor
       un ``404 CONTEXTO_NO_ENCONTRADO``. No se emite desde aquí a propósito: esta
       función no recibe el registro de auditoría, y dárselo la convertiría en un segundo
       sitio donde se escribe la bitácora del turno.
    """
    memoria.guardar_contexto(
        context_ref,
        {
            "context_ref": context_ref,
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "cuenta_id": cuenta,
            "motivo_codigo": str(motivo_codigo),
            "resumen_asesor": resumen_asesor,
            "utterance": (utterance or "").strip(),
            "canal": str(canal),
            "creado_en": datetime.now(UTC).isoformat(),
            "factset_sha256": "",
        },
    )
    memoria.marcar_derivada(conversation_id)


def _responder_en_copiloto(
    *,
    asesor: str,
    trace_id: str,
    conversacion: uuid.UUID,
    reglas: ConfiguracionReglas,
    auditoria: RegistroAuditoria,
    contexto_auditoria: dict[str, Any],
) -> RespuestaCanalAgnostica:
    """Respuesta cuando un asesor humano ocupa la sala: la IA se aparta.

    No se construye ``FactSet`` ni se llama al modelo. El mensaje del cliente ya quedó
    anotado en el historial para que el asesor lo vea en su consola; lo único que
    devuelve el asistente es el acuse de que hay una persona atendiendo.

    Cero cifras, así que ``verificacion_numerica`` es ``NO_APLICA`` con conjunto
    permitido vacío: el mismo criterio que en cualquier turno sin hechos.
    """
    bloques: list[Bloque] = [
        BloqueTexto(  # type: ignore[arg-type]
            texto=(
                "En este momento le está atendiendo un asesor. Su mensaje ya le llegó, "
                "aguarde un momento por favor."
            )
        )
    ]
    respuesta = RespuestaCanalAgnostica(
        conversation_id=conversacion,
        trace_id=trace_id,
        bloques=bloques,
        acciones=[],
        derivacion=Derivacion(
            requerida=True,
            motivo="un asesor humano está atendiendo la conversación",
            motivo_codigo=MotivoDerivacion.PETICION_HUMANO,
            senal_disparadora=f"asesor_en_sala={asesor!r}",
        ),
        gobernanza=Gobernanza(
            anclado=True,
            verificacion_numerica="NO_APLICA",
            aserciones_totales=0,
            aserciones_ancladas=0,
            aserciones_no_ancladas=0,
            confianza=1.0,
            modo=ModoGeneracion.PLANTILLA,
            rules_version=reglas.rules_version,
            model_version="copiloto-1.0.0",
            factset_sha256="",
        ),
        telemetria={
            "modo": str(ModoGeneracion.PLANTILLA),
            "sala": "ASISTIDA",
            "asesor": asesor,
            "respondio_la_ia": False,
            "explicacion_id": trace_id,
        },
    )
    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "etapa": "asesor",
            "evento": "ACUSE_COPILOTO",
            "asesor": asesor,
            "aserciones_totales": 0,
            "verificacion_numerica": "NO_APLICA",
        },
        **contexto_auditoria,
    )
    return respuesta


def _responder_por_intencion(
    *,
    intencion: ResultadoIntencion,
    trace_id: str,
    conversacion: uuid.UUID,
    cuenta: str,
    canal: Canal,
    peticion: PeticionExplicacion,
    identidad: Identidad,
    auditoria: RegistroAuditoria,
    telemetria: Any,
    reglas: ConfiguracionReglas,
    proveedor: Any,
    memoria: Any,
    ajustes: Any,
    contexto_auditoria: dict[str, Any],
) -> RespuestaCanalAgnostica:
    """Responde cuando la intención **no** es que le expliquen el recibo.

    No se construye ``FactSet``: si el cliente saludó, preguntó por la capital de
    Francia o pidió la baja, reconstruir su facturación es trabajo inútil y, en el
    último caso, una respuesta peligrosa. Ninguna de estas respuestas lleva cifras.

    Las intenciones con efecto regulatorio —baja, portabilidad, reclamo formal— y la
    petición explícita de un humano **derivan siempre**, sin negociar y sin score.
    """
    clave_conversacion = str(conversacion)
    severidad, _ = _COPY_INTENCION[intencion.intencion]

    # Una entrada sospechosa NO se le pasa al modelo. Enviarle a un LLM el texto
    # que intenta manipularlo es exactamente el riesgo que se quiere evitar, y
    # además no hay nada que redactar: la respuesta es siempre la misma negativa.
    if intencion.intencion is Intencion.SOSPECHOSA:
        auditoria.emitir(
            EtapaAuditoria.ROUTE,
            trace_id,
            {
                "etapa": "seguridad",
                "evento": "INTENTO_MANIPULACION",
                "senales": list(intencion.senales),
                "utterance": (peticion.utterance or "")[:500],
                "canal": str(canal),
                "cuenta": cuenta,
                "enviado_al_modelo": False,
            },
            **contexto_auditoria,
        )
        conversacional = ResultadoConversacional(
            _COPY_INTENCION[Intencion.SOSPECHOSA][1],
            ModoGeneracion.PLANTILLA,
            "plantilla-seguridad-1.0.0",
            bloqueado_por_cifras=False,
            detalle="entrada sospechosa: no se envió al modelo",
        )
    else:
        # La DECISIÓN ya la tomó el código; la REDACCIÓN la hace el modelo. Aquí no
        # hay FactSet, luego no hay ninguna cifra defendible: el guardián es más
        # estricto que en la explicación del recibo, porque cualquier dígito
        # bloquea el texto.
        conversacional = generar_respuesta_conversacional(
            intencion.intencion,
            peticion.utterance or "",
            proveedor=proveedor,
            historial=memoria.turnos_asistente(clave_conversacion)
            if hasattr(memoria, "turnos_asistente")
            else None,
            timeout_s=float(getattr(ajustes, "llm_timeout_s", 12.0) or 12.0),
        )
    bloques: list[Bloque] = [BloqueTexto(texto=conversacional.texto)]  # type: ignore[arg-type]

    derivacion = Derivacion()
    context_ref: str | None = None
    if intencion.deriva:
        context_ref = nuevo_context_ref(clave_conversacion, trace_id)
        motivo = _MOTIVO_INTENCION[intencion.intencion]
        resumen_asesor = (
            f"Cuenta {cuenta} · canal {canal}. "
            f"El cliente escribió: «{(peticion.utterance or '').strip()}». "
            f"Intención detectada: {intencion.intencion}. "
            "No se le entregó ninguna cifra ni se abrió su recibo."
        )
        telemetria.registrar_turno_usuario(conversacion, peticion.utterance)
        derivacion = Derivacion(
            requerida=True,
            motivo=motivo,
            motivo_codigo=intencion.motivo_derivacion,
            context_ref=context_ref,
            resumen_asesor=resumen_asesor,
            senal_disparadora=f"intencion={intencion.intencion} patron={intencion.patron!r}",
        )
        # Sin bloque de aviso: el texto generado ya dice que se está derivando, y
        # repetirlo suena a máquina. La interfaz usa `derivacion.requerida` para
        # pintar el estado de hand-off.
        #
        # El expediente SÍ se guarda: sin esto la derivación se le anunciaba al cliente
        # pero no aparecía en la cola del 104, así que nadie podía recogerla. No hay
        # FactSet en esta rama —no se abrió el recibo—, y es correcto: el asesor recibe
        # el motivo y lo dicho, no cifras que nadie calculó.
        registrar_expediente_derivacion(
            memoria,
            context_ref=context_ref,
            trace_id=trace_id,
            conversation_id=clave_conversacion,
            cuenta=cuenta,
            motivo_codigo=intencion.motivo_derivacion,
            resumen_asesor=resumen_asesor,
            utterance=peticion.utterance or "",
            canal=canal,
        )

    gobernanza = Gobernanza(
        anclado=True,
        verificacion_numerica="NO_APLICA",
        aserciones_totales=0,
        aserciones_ancladas=0,
        aserciones_no_ancladas=0,
        confianza=1.0,
        modo=conversacional.modo,
        rules_version=reglas.rules_version,
        model_version=conversacional.model_version,
        # No se construyó FactSet: no había recibo que explicar. La cadena vacía es
        # la marca honesta de «no se consultó ningún hecho de facturación».
        factset_sha256="",
    )

    respuesta = RespuestaCanalAgnostica(
        conversation_id=conversacion,
        trace_id=trace_id,
        bloques=bloques,
        acciones=_acciones_de_corte() if intencion.deriva else _acciones_de_intencion(),
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria={
            "modo": str(conversacional.modo),
            "intencion": str(intencion.intencion),
            "derivada": intencion.deriva,
            "explicacion_id": trace_id,
            "bloqueado_por_cifras": conversacional.bloqueado_por_cifras,
        },
    )
    memoria.anotar_turno(
        clave_conversacion, Turno(utterance=conversacional.texto, rol="asistente")
    )
    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "intencion": str(intencion.intencion),
            "derivada": intencion.deriva,
            "bloques": len(bloques),
            "aserciones_totales": 0,
            "verificacion_numerica": "NO_APLICA",
            "modo": str(conversacional.modo),
            "model_version": conversacional.model_version,
            "bloqueado_por_cifras": conversacional.bloqueado_por_cifras,
            "detalle_generacion": conversacional.detalle,
            # La referencia del expediente va en la bitácora, y no solo en la memoria del
            # proceso: es lo que hace que `GET /v1/asesor/paquete/{ref}` la encuentre.
            # `traza_de_context_ref` busca esta clave en los eventos sellados, así que un
            # expediente que solo viva en `memoria.guardar_contexto()` es un expediente
            # que el asesor recibe como 404. Las otras dos vías de derivación la declaran
            # igual, en su ROUTE o en su RESPONSE.
            "context_ref": context_ref,
        },
        **contexto_auditoria,
    )
    return respuesta


def _acciones_de_intencion() -> list[Accion]:
    """Acciones que se ofrecen cuando el cliente aún no pidió nada concreto."""
    return [
        Accion(
            id=AccionSiguiente.VER_DETALLE,
            etiqueta="Explíqueme mi recibo",
            riesgo="INFORMATIVA",
        ),
        Accion(
            id=AccionSiguiente.DERIVAR_ASESOR,
            etiqueta="Hablar con un asesor",
            riesgo="REVERSIBLE",
        ),
    ]


def _responder_derivando(
    *,
    factset: FactSet,
    motivo_codigo: MotivoDerivacion,
    motivo: str,
    senal: str,
    aviso: str,
    severidad: str,
    trace_id: str,
    conversacion: uuid.UUID,
    cuenta: str,
    canal: Canal,
    peticion: PeticionExplicacion,
    identidad: Identidad,
    auditoria: RegistroAuditoria,
    memoria: Any,
    telemetria: Any,
    ajustes: Any,
    contexto_auditoria: dict[str, Any],
    verificacion: ResultadoVerificacion | None = None,
    adversaria: dict[str, Any] | None = None,
) -> RespuestaCanalAgnostica:
    """Responde derivando a un asesor, con contexto cargado y **sin ninguna cifra**.

    Se usa en los dos cortes duros del flujo: el recibo no concilia (sección 4.6) y el
    verificador encontró una cifra sin respaldo (sección 5.3). En ambos el sistema sabe
    que no puede sostener un número, así que no escribe ninguno: entrega el aviso, abre
    el hand-off con el brief del asesor y lo deja todo en la bitácora.
    """
    clave_conversacion = str(conversacion)
    context_ref = nuevo_context_ref(clave_conversacion, trace_id)
    resumen_asesor = construir_resumen_asesor(
        factset,
        cuenta_id=cuenta,
        motivo_codigo=motivo_codigo,
        utterance=peticion.utterance,
        canal=str(canal),
        verificacion=str(verificacion.veredicto) if verificacion else None,
        detalle_motivo=motivo,
    )
    memoria.guardar_contexto(
        context_ref,
        construir_contexto_derivacion(
            context_ref=context_ref,
            trace_id=trace_id,
            conversation_id=clave_conversacion,
            cuenta_id=cuenta,
            factset=factset,
            motivo_codigo=motivo_codigo,
            resumen_asesor=resumen_asesor,
            utterance=peticion.utterance,
            canal=str(canal),
            extra={"adversaria": adversaria} if adversaria else None,
        ),
    )
    memoria.marcar_derivada(clave_conversacion)

    bloques: list[Bloque] = [BloqueAviso(severidad=severidad, texto=aviso)]  # type: ignore[arg-type]
    derivacion = Derivacion(
        requerida=True,
        motivo=motivo,
        motivo_codigo=motivo_codigo,
        context_ref=context_ref,
        resumen_asesor=resumen_asesor,
        senal_disparadora=senal,
    )
    gobernanza = _gobernanza_sin_cifras(factset, modelo="plantilla-determinista")
    if verificacion is not None:
        gobernanza = gobernanza.model_copy(
            update={
                "anclado": False,
                "verificacion_numerica": "FAIL",
                "aserciones_totales": verificacion.aserciones_totales,
                "aserciones_ancladas": verificacion.ancladas + verificacion.derivadas,
                "aserciones_no_ancladas": verificacion.no_ancladas,
            }
        )

    sonda = telemetria.abrir_sonda(
        conversacion,
        trace_id,
        cuenta_ref=cuenta,
        periodo=factset.periodo_actual,
        canal=canal,
        utterance_origen=peticion.utterance,
        derivada=True,
        verificacion=verificacion.veredicto if verificacion else VeredictoVerificacion.NO_APLICA,
        ventana_s=ajustes.ventana_silencio_s,
    )
    telemetria_turno: dict[str, Any] = {
        "modo": str(ModoGeneracion.PLANTILLA),
        "derivada": True,
        "motivo_codigo": str(motivo_codigo),
        "explicacion_id": trace_id,
        "firma_causal": factset.firma_causal(),
        **sonda.a_telemetria(),
    }
    if adversaria:
        telemetria_turno["adversaria"] = adversaria

    respuesta = RespuestaCanalAgnostica(
        conversation_id=conversacion,
        trace_id=trace_id,
        bloques=bloques,
        acciones=_acciones_de_corte(),
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria=telemetria_turno,
    )
    memoria.guardar_explicacion(
        RegistroExplicacion(
            explicacion_id=trace_id,
            trace_id=trace_id,
            conversation_id=clave_conversacion,
            cuenta_ref=cuenta,
            periodo=factset.periodo_actual,
            factset=factset,
            respuesta=respuesta,
            utterance=peticion.utterance,
            canal=str(canal),
            derivada=True,
        )
    )
    auditoria.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "derivar": True,
            "motivo_codigo": str(motivo_codigo),
            "score_incomprension": None,
            "modo": "REGLA_DURA",
            "senal_disparadora": senal,
            "context_ref": context_ref,
        },
        **contexto_auditoria,
    )
    auditoria.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "bloques": len(bloques),
            "acciones": 2,
            "modo": str(ModoGeneracion.PLANTILLA),
            "derivada": True,
            "latencia_ms": 0,
            "silence_probe_id": str(sonda.silence_probe_id),
            "context_ref": context_ref,
            "texto_entregado": respuesta.texto[:MAX_TEXTO_BITACORA],
        },
        **contexto_auditoria,
    )
    _cerrar(auditoria, trace_id, cuenta, imprimir=ajustes.log_terminal)
    return redactar_para_nivel(respuesta, identidad.acr, factset=factset)
