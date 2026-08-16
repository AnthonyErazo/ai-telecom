"""Los nodos del turno. **Llaman a lo que ya existe; no reimplementan nada.**

El principio que ordena este módulo
-----------------------------------
El valor del proyecto está en el motor determinístico (``packages/facts_engine``) y en
el verificador numérico (``packages/llm_layer/verificador.py``). LangGraph es una capa
de **orquestación**: decide *qué se llama y en qué orden*, no *qué se calcula*. Por eso
aquí no hay una sola fórmula, ni un solo umbral, ni una sola cifra. Cada nodo es un
envoltorio delgado alrededor de una función que ya estaba escrita, probada y verde:

======================  =========================================================
Nodo                    Qué llama
======================  =========================================================
``clasificar``          ``packages.facts_engine.intencion.clasificar_intencion``
``responder_intencion`` ``packages.llm_layer.conversacional``
``construir_hechos``    ``apps.api.routers.hechos.construir_hechos``
``recuperar_contexto``  ``packages.retriever.recuperar`` + ``confianza.evaluar_incomprension``
``generar``             ``packages.llm_layer.generador.generar_explicacion``
``verificar_y_armar``   el ``ResultadoVerificacion`` que ya trae el generador
``derivar``             el camino de corte duro, sin ninguna cifra
======================  =========================================================

Por qué se reutilizan piezas privadas de ``apps/api/routers/explicar.py``
------------------------------------------------------------------------
La exigencia del encargo es que el grafo produzca **exactamente la misma**
``RespuestaCanalAgnostica`` que el endpoint actual. Hay dos maneras de conseguirlo:
copiar sus funciones auxiliares aquí, o llamarlas. Copiarlas garantizaría que un día se
tocara una y no la otra, y entonces el grafo y el endpoint responderían distinto sin
que nadie se enterase. Así que se **llaman**: ``_asegurar_puente``, ``_payload_verify``,
``_gobernanza_sin_cifras``, ``_acciones_de_corte``, ``evaluar_cross_selling``…

La importación es **diferida** (dentro de las funciones, no arriba) por una razón
concreta: cuando el endpoint pase a delegar en este grafo, importará este módulo, y un
``import`` en las dos direcciones a nivel de módulo sería un ciclo. Diferirlo lo rompe.

Contrato de un nodo
-------------------
``(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict`` — devuelve **solo las
claves que produce**, nunca el estado entero. La bitácora se emite con las **mismas
etapas y en el mismo orden** que hoy, incluida la asimetría importante: la rama de
intención (``responder_intencion``) **no** cierra la cadena con ``CHAIN``, igual que
hoy no lo hace ``_responder_por_intencion``. Si el grafo unificara ese cierre, la
bitácora cambiaría y los tests de contrato se moverían.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from apps.api.deps import RegistroExplicacion, historial_para_score
from apps.api.routers.derivacion import (
    construir_contexto_derivacion,
    construir_resumen_asesor,
    nuevo_context_ref,
)
from apps.api.routers.hechos import construir_hechos as construir_hechos_conciliados
from apps.api.routers.hechos import payload_facts_built
from apps.api.security import redactar_para_nivel
from packages.core_domain.enums import (
    AccionSiguiente,
    EtapaAuditoria,
    ModoGeneracion,
    MotivoDerivacion,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.respuesta import (
    Accion,
    Bloque,
    BloqueAviso,
    BloqueTexto,
    Derivacion,
    Gobernanza,
    RespuestaCanalAgnostica,
)
from packages.facts_engine.confianza import Turno, evaluar_incomprension
from packages.facts_engine.intencion import Intencion, resolver_intencion_contextual
from packages.llm_layer.conversacional import (
    ResultadoConversacional,
    generar_respuesta_conversacional,
)
from packages.llm_layer.generador import ETIQUETAS_ACCION, generar_explicacion
from packages.llm_layer.verificador import construir_permitidos, inyectar_alucinacion, verificar
from packages.orquestacion.estado import (
    CORTE_INVARIANTE_ROTO,
    CORTE_VERIFICACION_FALLIDA,
    EstadoTurno,
    Servicios,
)
from packages.orquestacion.telemetria_externa import apagar_telemetria_externa
from packages.retriever import ContextoRecuperado, recuperar

apagar_telemetria_externa()

from langgraph.runtime import Runtime  # noqa: E402

__all__ = [
    "NOMBRES_DE_NODO",
    "clasificar",
    "construir_hechos",
    "derivar",
    "generar",
    "recuperar_contexto",
    "responder_intencion",
    "verificar_y_armar",
]

_LOG = logging.getLogger(__name__)

#: Los nodos del grafo, en el orden del camino feliz. Sirve de índice y de contrato
#: para las pruebas: si alguien añade un nodo, esta lista tiene que enterarse.
NOMBRES_DE_NODO: tuple[str, ...] = (
    "clasificar",
    "responder_intencion",
    "construir_hechos",
    "recuperar_contexto",
    "generar",
    "verificar_y_armar",
    "derivar",
)


# --------------------------------------------------------------------------- #
# Acceso diferido al router de explicación
# --------------------------------------------------------------------------- #
def _piezas_de_explicar() -> Any:
    """Devuelve ``apps.api.routers.explicar``, importándolo la primera vez.

    Diferido a propósito: ese módulo importará este cuando el endpoint delegue en el
    grafo, y un ``import`` mutuo a nivel de módulo sería un ciclo.
    """
    from apps.api.routers import explicar

    return explicar


def _conversacion(estado: EstadoTurno) -> uuid.UUID:
    """``conversation_id`` como UUID, que es lo que esperan telemetría y respuesta."""
    return uuid.UUID(estado["conversation_id"])


def _clave_permitidos(trace_id: str) -> str:
    """Clave del ``ConjuntoPermitido`` en la bolsa, **acotada al turno**.

    Va con el ``trace_id`` y no con un nombre fijo por si alguien reutilizara la misma
    instancia de :class:`Servicios` en dos turnos a la vez: dos peticiones concurrentes
    no pueden pisarse el conjunto permitido, que es literalmente la lista de cifras que
    el texto tiene derecho a decir.
    """
    return f"permitidos:{trace_id}"


# --------------------------------------------------------------------------- #
# 0. Intención
# --------------------------------------------------------------------------- #
def clasificar(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Abre el turno en la bitácora y decide si corresponde explicar el recibo.

    Es la compuerta que evita que un «hola» devuelva la factura entera y, sobre todo,
    que «quiero cancelar mi servicio» se conteste con una explicación en vez de
    derivarse: eso último es una regla de cumplimiento regulatorio, no una preferencia.

    Aquí se emite ``REQUEST`` porque es el primer nodo del grafo, exactamente donde hoy
    lo emite el endpoint: antes de tocar nada de facturación.
    """
    servicios = runtime.context
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    utterance = estado.get("utterance", "")

    servicios.auditoria.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "POST /v1/explicar",
            "periodo": estado.get("periodo"),
            "canal": str(estado["canal"]),
            "nivel": str(estado["nivel"]),
            "verbosidad": str(estado["verbosidad"]),
            "utterance": utterance,
            "conversation_id": estado["conversation_id"],
        },
        **contexto_auditoria,
    )
    # Un turno nuevo del cliente resuelve las sondas de silencio que siguieran abiertas.
    servicios.telemetria.registrar_turno_usuario(_conversacion(estado), utterance)

    resolucion = resolver_intencion_contextual(
        utterance,
        servicios.memoria.turnos(estado["conversation_id"]),
    )
    intencion = resolucion.intencion
    utterance_efectiva = resolucion.utterance_efectiva
    servicios.auditoria.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "etapa": "intencion",
            "intencion": str(intencion.intencion),
            "patron": intencion.patron,
            "explica_recibo": intencion.explica_recibo,
            "deriva": intencion.deriva,
            "motivo_codigo": str(intencion.motivo_derivacion) if intencion.deriva else None,
            "contexto_pendiente": resolucion.concepto_pendiente,
            "utterance_efectiva": (
                utterance_efectiva if resolucion.contexto_aplicado else None
            ),
        },
        **contexto_auditoria,
    )
    return {
        "intencion": intencion,
        "utterance": utterance_efectiva,
        "utterance_original": resolucion.utterance_original,
        "eventos": ["REQUEST", "ROUTE:intencion"],
        "nodos": ["clasificar"],
    }


# --------------------------------------------------------------------------- #
# 0-bis. Turnos que no piden explicación de recibo
# --------------------------------------------------------------------------- #
def responder_intencion(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Responde cuando la intención **no** es que le expliquen el recibo.

    No se construye ``FactSet``: si el cliente saludó, preguntó por la capital de
    Francia o pidió la baja, reconstruir su facturación es trabajo inútil y, en el
    último caso, una respuesta peligrosa. Ninguna de estas respuestas lleva cifras, y
    por eso el conjunto permitido está vacío: **cualquier dígito** bloquea el texto.

    Este nodo **no cierra la cadena de la bitácora** (no emite ``CHAIN``), igual que
    hoy: es una asimetría deliberada del contrato de auditoría, no un olvido.
    """
    servicios = runtime.context
    explicar = _piezas_de_explicar()
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    clave = estado["conversation_id"]
    utterance = estado.get("utterance", "")
    intencion = estado["intencion"]
    assert intencion is not None

    servicios.memoria.anotar_turno(clave, Turno(utterance=utterance, rol="cliente"))
    # Acceso deliberado: si apareciera una intención sin copia declarada, se quiere que
    # falle aquí y no que responda cualquier cosa.
    explicar._COPY_INTENCION[intencion.intencion]

    eventos = ["RESPONSE"]
    if intencion.intencion is Intencion.SOSPECHOSA:
        # Una entrada sospechosa NO se le pasa al modelo. Enviarle a un LLM el texto que
        # intenta manipularlo es exactamente el riesgo que se quiere evitar, y además no
        # hay nada que redactar: la respuesta es siempre la misma negativa.
        servicios.auditoria.emitir(
            EtapaAuditoria.ROUTE,
            trace_id,
            {
                "etapa": "seguridad",
                "evento": "INTENTO_MANIPULACION",
                "senales": list(intencion.senales),
                "utterance": (utterance or "")[:500],
                "canal": str(estado["canal"]),
                "cuenta": estado["cuenta"],
                "enviado_al_modelo": False,
            },
            **contexto_auditoria,
        )
        eventos = ["ROUTE:seguridad", "RESPONSE"]
        conversacional = ResultadoConversacional(
            explicar._COPY_INTENCION[Intencion.SOSPECHOSA][1],
            ModoGeneracion.PLANTILLA,
            "plantilla-seguridad-1.0.0",
            bloqueado_por_cifras=False,
            detalle="entrada sospechosa: no se envió al modelo",
        )
    else:
        # La DECISIÓN ya la tomó el código; la REDACCIÓN la hace el modelo.
        conversacional = generar_respuesta_conversacional(
            intencion.intencion,
            utterance or "",
            proveedor=servicios.proveedor,
            historial=servicios.memoria.turnos_asistente(clave)
            if hasattr(servicios.memoria, "turnos_asistente")
            else None,
            timeout_s=float(getattr(servicios.ajustes, "llm_timeout_s", 12.0) or 12.0),
        )

    bloques: list[Bloque] = [BloqueTexto(texto=conversacional.texto)]  # type: ignore[list-item]

    derivacion = Derivacion()
    context_ref: str | None = None
    if intencion.deriva:
        context_ref = nuevo_context_ref(clave, trace_id)
        motivo = explicar._MOTIVO_INTENCION[intencion.intencion]
        resumen_asesor = (
            f"Cuenta {estado['cuenta']} · canal {estado['canal']}. "
            f"El cliente escribió: «{(utterance or '').strip()}». "
            f"Intención detectada: {intencion.intencion}. "
            "No se le entregó ninguna cifra ni se abrió su recibo."
        )
        servicios.telemetria.registrar_turno_usuario(_conversacion(estado), utterance)
        derivacion = Derivacion(
            requerida=True,
            motivo=motivo,
            motivo_codigo=intencion.motivo_derivacion,
            context_ref=context_ref,
            resumen_asesor=resumen_asesor,
            senal_disparadora=f"intencion={intencion.intencion} patron={intencion.patron!r}",
        )
        # Mismo expediente que la ruta lineal, por la misma función: sin esto la
        # derivación se anunciaba al cliente pero no llegaba a la cola del 104.
        explicar.registrar_expediente_derivacion(
            servicios.memoria,
            context_ref=context_ref,
            trace_id=trace_id,
            conversation_id=clave,
            cuenta=estado["cuenta"],
            motivo_codigo=intencion.motivo_derivacion,
            resumen_asesor=resumen_asesor,
            utterance=utterance or "",
            canal=estado["canal"],
        )

    gobernanza = Gobernanza(
        anclado=True,
        verificacion_numerica="NO_APLICA",
        aserciones_totales=0,
        aserciones_ancladas=0,
        aserciones_no_ancladas=0,
        confianza=1.0,
        modo=conversacional.modo,
        rules_version=servicios.reglas.rules_version,
        model_version=conversacional.model_version,
        # No se construyó FactSet: no había recibo que explicar. La cadena vacía es la
        # marca honesta de «no se consultó ningún hecho de facturación».
        factset_sha256="",
    )
    acciones = (
        explicar._acciones_de_corte() if intencion.deriva else explicar._acciones_de_intencion()
    )
    telemetria_turno: dict[str, Any] = {
        "modo": str(conversacional.modo),
        "intencion": str(intencion.intencion),
        "derivada": intencion.deriva,
        "explicacion_id": trace_id,
        "bloqueado_por_cifras": conversacional.bloqueado_por_cifras,
    }

    respuesta = RespuestaCanalAgnostica(
        conversation_id=_conversacion(estado),
        trace_id=trace_id,
        bloques=bloques,
        acciones=acciones,
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria=telemetria_turno,
    )
    servicios.memoria.anotar_turno(clave, Turno(utterance=conversacional.texto, rol="asistente"))
    servicios.auditoria.emitir(
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
            # Misma clave que en la vía directa: sin ella el expediente solo existe en la
            # memoria del proceso y el paquete del asesor responde 404.
            "context_ref": context_ref,
        },
        **contexto_auditoria,
    )
    return {
        "bloques": bloques,
        "acciones": acciones,
        "derivacion": derivacion,
        "gobernanza": gobernanza,
        "telemetria": telemetria_turno,
        "context_ref": context_ref,
        "respuesta": respuesta,
        "eventos": eventos,
        "nodos": ["responder_intencion"],
    }


# --------------------------------------------------------------------------- #
# 1. Hechos
# --------------------------------------------------------------------------- #
def construir_hechos(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Construye el ``FactSet`` sellado y comprueba el invariante.

    El motor **no lanza** si el recibo no concilia: devuelve el ``FactSet`` con
    ``invariante.ok = False`` y todos sus datos, porque quien deriva necesita esos
    datos. La decisión de qué hacer con eso la toma la arista condicional que sigue.

    Los errores del ACL (404, 503, 422) se dejan **propagar** tal cual: hoy salen del
    endpoint como ``ErrorApi`` y dejan el turno con ``REQUEST`` y ``ROUTE`` en la
    bitácora, sin ``CHAIN``. Capturarlos aquí cambiaría ese contrato.
    """
    servicios = runtime.context
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    clave = estado["conversation_id"]
    utterance = estado.get("utterance", "")
    utterance_original = estado.get("utterance_original", utterance)

    factset, datos = construir_hechos_conciliados(
        servicios.repositorio,
        servicios.reglas,
        estado["cuenta"],
        estado.get("periodo"),
        trace_id=trace_id,
    )
    servicios.auditoria.emitir(
        EtapaAuditoria.FACTS_BUILT,
        trace_id,
        payload_facts_built(factset, datos),
        **contexto_auditoria,
    )
    servicios.auditoria.emitir(
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

    historial = list(historial_para_score(servicios.memoria, clave, utterance))
    servicios.memoria.anotar_turno(
        clave, Turno(utterance=utterance_original, rol="cliente")
    )

    return {
        "factset": factset,
        "historial": historial,
        "corte": None if factset.invariante.ok else CORTE_INVARIANTE_ROTO,
        "eventos": ["FACTS_BUILT", "INVARIANTE"],
        "nodos": ["construir_hechos"],
    }


# --------------------------------------------------------------------------- #
# 2. Contexto recuperado y umbral de incomprensión
# --------------------------------------------------------------------------- #
def recuperar_contexto(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Recupera contexto del corpus y evalúa el umbral de incomprensión.

    Que el retriever falle **no corta el turno**: las cifras no salen del RAG sino del
    ``FactSet``, así que sin contexto la explicación pierde color narrativo y nada más.
    Lo que sí queda es constancia en la bitácora de que se respondió a ciegas.
    """
    servicios = runtime.context
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    factset = estado["factset"]
    assert factset is not None
    utterance = estado.get("utterance", "")

    contexto: ContextoRecuperado | None = None
    if servicios.recuperador is not None:
        try:
            contexto = recuperar(factset, utterance, k=5, recuperador=servicios.recuperador)
        # Se captura todo a propósito: un RAG caído no puede tumbar el turno, porque
        # las cifras no salen de él sino del FactSet.
        except Exception as error:
            _LOG.warning("el retriever falló (%s); se explica sin contexto", error)
    servicios.auditoria.emitir(
        EtapaAuditoria.RETRIEVE,
        trace_id,
        _piezas_de_explicar()._payload_retrieve(contexto),
        **contexto_auditoria,
    )

    fuera_catalogo = list(contexto.conceptos_fuera_catalogo) if contexto else []
    incomprension = evaluar_incomprension(
        factset,
        estado.get("historial", []),
        utterance,
        reglas=servicios.reglas,
        derivado_previamente=servicios.memoria.fue_derivada(estado["conversation_id"]),
        # Misma precondición que en la vía directa: la histéresis la activa una persona
        # dentro de la sala, no el recuerdo de un score que subió una vez.
        asesor_en_sala=servicios.memoria.asesor_presente(estado["conversation_id"]) is not None,
        conceptos_fuera_catalogo=fuera_catalogo,
    )
    servicios.auditoria.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "derivar": incomprension.derivar,
            "motivo_codigo": str(incomprension.motivo) if incomprension.motivo else None,
            "score_incomprension": round(incomprension.U, 4),
            "modo": "SCORE",
            "reglas_disparadas": list(incomprension.reglas_disparadas),
            "senal_disparadora": incomprension.senal_disparadora,
            # Mismo payload que la vía directa: las dos magnitudes por separado, para
            # que la bitácora del grafo y la del router sean comparables evento a evento.
            "desglose": incomprension.s1_cobertura,
            "cobertura_causal": incomprension.cobertura_causal,
            "ofrece_asesor": incomprension.ofrecer_asesor,
        },
        **contexto_auditoria,
    )
    return {
        "contexto_recuperado": contexto,
        "incomprension": incomprension,
        "eventos": ["RETRIEVE", "ROUTE:score"],
        "nodos": ["recuperar_contexto"],
    }


# --------------------------------------------------------------------------- #
# 3. Generación verificada
# --------------------------------------------------------------------------- #
def generar(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Genera la explicación aplicando LLM → verificar → reintento → plantilla.

    El ``ConjuntoPermitido`` se construye aquí y se guarda en la **bolsa** de servicios,
    no en el estado: es una clase con ``__slots__`` sin representación serializable, y
    meterla en el estado haría reventar al *checkpointer*. Como es determinista desde el
    ``FactSet``, el nodo siguiente puede reconstruirla si hiciera falta.
    """
    servicios = runtime.context
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    factset = estado["factset"]
    assert factset is not None
    contexto = estado.get("contexto_recuperado")
    clave = estado["conversation_id"]

    permitidos = construir_permitidos(factset)
    servicios.bolsa[_clave_permitidos(trace_id)] = permitidos
    resultado = generar_explicacion(
        factset,
        contexto_recuperado=contexto.fragmentos if contexto else None,
        utterance=estado.get("utterance", ""),
        verbosidad=estado["verbosidad"],
        proveedor=servicios.proveedor,
        canal=estado["canal"],
        estricto=servicios.ajustes.verificador_estricto,
        timeout_s=servicios.ajustes.llm_timeout_s,
        permitidos=permitidos,
        # Lo que el asistente ya le contestó antes en esta conversación (mismo método
        # que usa el camino conversacional en `redactar_sin_cifras`): sin esto, la
        # segunda pregunta sobre el mismo recibo reconstruye el prompt desde cero y
        # el modelo repite casi textual la primera respuesta.
        respuestas_previas=servicios.memoria.turnos_asistente(clave)
        if hasattr(servicios.memoria, "turnos_asistente")
        else None,
    )
    degradado = resultado.modo is ModoGeneracion.PLANTILLA and servicios.proveedor is not None

    for intento in resultado.intentos:
        codigo_error = (intento.error or {}).get("codigo")
        servicios.auditoria.emitir(
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
                "timeout_s": servicios.ajustes.llm_timeout_s,
                "modo": str(intento.modo),
                "veredicto": intento.veredicto,
                "no_ancladas": intento.no_ancladas,
                "infractores": intento.infractores,
                "error": intento.error,
            },
            **contexto_auditoria,
        )
    return {
        "resultado_generacion": resultado,
        "degradado": degradado,
        "eventos": ["LLM_CALL"] * len(resultado.intentos),
        "nodos": ["generar"],
    }


# --------------------------------------------------------------------------- #
# 4. Verificación y armado de la respuesta
# --------------------------------------------------------------------------- #
def verificar_y_armar(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Cierra el anclaje numérico y arma la respuesta del camino feliz.

    Nada de esto vuelve a verificar por su cuenta: se usa el ``ResultadoVerificacion``
    que **ya trae el generador**. La única re-verificación es la que hace
    ``_asegurar_puente`` cuando añadir el bloque de cascada cambia el texto entregado
    —y por tanto cambia lo que el verificador audita—.

    Si el veredicto es ``FAIL`` (o el generador bloqueó el texto), este nodo **no
    responde**: marca el corte y la arista condicional lo manda a ``derivar``. Es la
    regla dura de la sección 5.3: ninguna cifra sin anclar sale de aquí.
    """
    servicios = runtime.context
    explicar = _piezas_de_explicar()
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    factset = estado["factset"]
    resultado = estado["resultado_generacion"]
    incomprension = estado["incomprension"]
    assert factset is not None and resultado is not None
    assert incomprension is not None
    clave = estado["conversation_id"]
    cuenta = estado["cuenta"]
    canal = estado["canal"]
    utterance = estado.get("utterance", "")
    contexto = estado.get("contexto_recuperado")

    # Si la bolsa viniera vacía —por ejemplo tras reanudar el turno en otro proceso—
    # se reconstruye: `construir_permitidos` es determinista desde el FactSet.
    permitidos = servicios.bolsa.pop(_clave_permitidos(trace_id), None) or construir_permitidos(
        factset
    )
    bloques, verificacion = explicar._asegurar_puente(
        factset, resultado, permitidos, estricto=servicios.ajustes.verificador_estricto
    )

    # --- Demo adversaria (solo si /dev/alucinar la activó) --------------------- #
    adversaria: dict[str, Any] | None = None
    if servicios.adversario.consumir():
        texto_envenenado = inyectar_alucinacion(
            "\n".join(bloque.a_texto() for bloque in bloques),
            factset,
            delta_cent=servicios.adversario.delta_cent,
        )
        veredicto_adv = verificar(
            texto_envenenado,
            factset,
            permitidos=permitidos,
            estricto=servicios.ajustes.verificador_estricto,
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

    servicios.auditoria.emitir(
        EtapaAuditoria.VERIFY,
        trace_id,
        explicar._payload_verify(verificacion, adversaria),
        **contexto_auditoria,
    )
    servicios.auditoria.emitir(
        EtapaAuditoria.CITATIONS,
        trace_id,
        {
            "citas": [cita.model_dump(mode="json") for cita in verificacion.citas],
            "fact_ids": sorted({cita.fact_id for cita in verificacion.citas}),
        },
        **contexto_auditoria,
    )

    # --- Si el texto no está anclado, no sale --------------------------------- #
    if verificacion.veredicto is VeredictoVerificacion.FAIL or resultado.bloqueada:
        return {
            "verificacion": verificacion,
            "adversaria": adversaria,
            "corte": CORTE_VERIFICACION_FALLIDA,
            "eventos": ["VERIFY", "CITATIONS"],
            "nodos": ["verificar_y_armar"],
        }

    # --- Derivación por umbral de incomprensión -------------------------------- #
    derivacion = Derivacion()
    context_ref: str | None = None
    if incomprension.derivar:
        context_ref = nuevo_context_ref(clave, trace_id)
        resumen_asesor = construir_resumen_asesor(
            factset,
            cuenta_id=cuenta,
            motivo_codigo=incomprension.motivo or MotivoDerivacion.UMBRAL_INCOMPRENSION,
            utterance=utterance,
            canal=str(canal),
            verificacion=str(verificacion.veredicto),
            modo=str(resultado.modo),
            detalle_motivo=incomprension.senal_disparadora,
        )
        derivacion = incomprension.a_derivacion(
            context_ref=context_ref, resumen_asesor=resumen_asesor
        )
        servicios.memoria.guardar_contexto(
            context_ref,
            construir_contexto_derivacion(
                context_ref=context_ref,
                trace_id=trace_id,
                conversation_id=clave,
                cuenta_id=cuenta,
                factset=factset,
                motivo_codigo=incomprension.motivo or MotivoDerivacion.UMBRAL_INCOMPRENSION,
                resumen_asesor=resumen_asesor,
                utterance=utterance,
                canal=str(canal),
                extra={"score_incomprension": round(incomprension.U, 4)},
            ),
        )
        servicios.memoria.marcar_derivada(clave)

    # --- Acciones y respuesta --------------------------------------------------- #
    acciones = list(resultado.acciones)
    # Igual que en la vía directa: derivar (el sistema pasa la conversación) y ofrecer
    # (el cliente decide si quiere el motivo documentado) son cosas distintas, y la
    # segunda es la que evita que la laguna del CRM se cobre en hand-offs.
    if incomprension.asesor_a_la_vista and all(
        accion.id is not AccionSiguiente.DERIVAR_ASESOR for accion in acciones
    ):
        etiqueta, riesgo = ETIQUETAS_ACCION[AccionSiguiente.DERIVAR_ASESOR]
        acciones.insert(0, Accion(id=AccionSiguiente.DERIVAR_ASESOR, etiqueta=etiqueta, riesgo=riesgo))  # type: ignore[arg-type]
    oferta = explicar.evaluar_cross_selling(
        factset,
        servicios.reglas,
        resuelta=verificacion.veredicto is VeredictoVerificacion.PASS,
        derivar=incomprension.derivar,
    )
    if oferta is not None and all(accion.id is not oferta.id for accion in acciones):
        acciones.append(oferta)
    # Igual que en la vía directa (`explicar.py`): ofrecer pagar no depende de que el
    # cliente lo pida, basta con que tenga saldo pendiente.
    if factset.total_a_pagar_cent > 0 and all(
        accion.id is not AccionSiguiente.PAGAR for accion in acciones
    ):
        etiqueta, riesgo = ETIQUETAS_ACCION[AccionSiguiente.PAGAR]
        acciones.append(Accion(id=AccionSiguiente.PAGAR, etiqueta=etiqueta, riesgo=riesgo))  # type: ignore[arg-type]

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

    sonda = servicios.telemetria.abrir_sonda(
        _conversacion(estado),
        trace_id,
        cuenta_ref=cuenta,
        periodo=factset.periodo_actual,
        canal=canal,
        utterance_origen=utterance,
        causa_dominante=(
            factset.causa_dominante().etiqueta_cliente if factset.causa_dominante() else None
        ),
        derivada=incomprension.derivar,
        verificacion=verificacion.veredicto,
        ventana_s=servicios.ajustes.ventana_silencio_s,
    )
    telemetria_turno = resultado.telemetria()
    telemetria_turno.update(sonda.a_telemetria())
    telemetria_turno.update(
        {
            "explicacion_id": trace_id,
            "degradado": estado.get("degradado", False),
            "contexto_degradado": bool(contexto.degradado) if contexto else True,
            "score_incomprension": round(incomprension.U, 4),
            # Misma telemetría que la vía directa: un turno resuelto con salvedad causal
            # y uno resuelto con la causa documentada no pueden verse iguales.
            "cobertura_causal": incomprension.cobertura_causal,
            "asesor_ofrecido": incomprension.ofrecer_asesor,
            "cross_selling": oferta.payload.get("regla") if oferta else None,
            "firma_causal": factset.firma_causal(),
        }
    )
    if adversaria:
        telemetria_turno["adversaria"] = adversaria

    respuesta = RespuestaCanalAgnostica(
        conversation_id=_conversacion(estado),
        trace_id=trace_id,
        bloques=bloques,
        acciones=acciones,
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria=telemetria_turno,
    )
    servicios.memoria.guardar_explicacion(
        RegistroExplicacion(
            explicacion_id=trace_id,
            trace_id=trace_id,
            conversation_id=clave,
            cuenta_ref=cuenta,
            periodo=factset.periodo_actual,
            factset=factset,
            respuesta=respuesta,
            evidencia=contexto.items_evidencia() if contexto else [],
            utterance=utterance,
            canal=str(canal),
            contexto_rag=contexto.resumen_auditoria() if contexto else {},
            score_incomprension=round(incomprension.U, 4),
            derivada=incomprension.derivar,
        )
    )
    servicios.memoria.anotar_turno(
        clave,
        Turno(
            utterance=respuesta.texto[:400],
            rol="asistente",
            progreso=not incomprension.derivar,
            derivado=incomprension.derivar,
        ),
    )
    servicios.auditoria.emitir(
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
            "degradado": estado.get("degradado", False),
            # Igual que en la vía lineal: la bitácora conserva lo que se le dijo al
            # cliente, para que el asesor no se lo repita.
            "texto_entregado": respuesta.texto[: explicar.MAX_TEXTO_BITACORA],
        },
        **contexto_auditoria,
    )
    explicar._cerrar(servicios.auditoria, trace_id, cuenta, imprimir=servicios.ajustes.log_terminal)
    return {
        "verificacion": verificacion,
        "adversaria": adversaria,
        "bloques": bloques,
        "acciones": acciones,
        "derivacion": derivacion,
        "gobernanza": gobernanza,
        "telemetria": telemetria_turno,
        "context_ref": context_ref,
        "corte": None,
        "respuesta": redactar_para_nivel(respuesta, estado["nivel"], factset=factset),
        "eventos": ["VERIFY", "CITATIONS", "RESPONSE", "CHAIN"],
        "nodos": ["verificar_y_armar"],
    }


# --------------------------------------------------------------------------- #
# 5. Corte duro: derivar sin ninguna cifra
# --------------------------------------------------------------------------- #
#: Copia de cada corte duro: motivo legible, señal citable y severidad del aviso.
#: El texto lo pone ``explicar`` (``AVISO_INVARIANTE`` / ``AVISO_VERIFICACION``), que es
#: donde vive hoy; aquí solo se selecciona cuál toca.
_MOTIVO_CORTE: dict[str, tuple[MotivoDerivacion, str, str]] = {
    CORTE_INVARIANTE_ROTO: (
        MotivoDerivacion.INVARIANTE_ROTO,
        "el detalle del recibo no cuadra con la diferencia entre totales",
        "critico",
    ),
    CORTE_VERIFICACION_FALLIDA: (
        MotivoDerivacion.VERIFICACION_FALLIDA,
        "no se pudo anclar toda la explicación numérica en el recibo",
        "advertencia",
    ),
}


def derivar(estado: EstadoTurno, runtime: Runtime[Servicios]) -> dict[str, Any]:
    """Responde derivando a un asesor, con contexto cargado y **sin ninguna cifra**.

    Es el destino de los dos cortes duros: el recibo no concilia (sección 4.6) y el
    verificador encontró una cifra sin respaldo (sección 5.3). En ambos el sistema sabe
    que no puede sostener un número, así que **no escribe ninguno**: entrega el aviso,
    abre el hand-off con el brief del asesor y lo deja todo en la bitácora.
    """
    servicios = runtime.context
    explicar = _piezas_de_explicar()
    trace_id = estado["trace_id"]
    contexto_auditoria = estado["contexto_auditoria"]
    factset = estado["factset"]
    assert factset is not None
    clave = estado["conversation_id"]
    cuenta = estado["cuenta"]
    canal = estado["canal"]
    utterance = estado.get("utterance", "")

    corte = estado.get("corte") or CORTE_INVARIANTE_ROTO
    motivo_codigo, motivo, severidad = _MOTIVO_CORTE[corte]
    if corte == CORTE_VERIFICACION_FALLIDA:
        verificacion = estado.get("verificacion")
        adversaria = estado.get("adversaria")
        aviso = explicar.AVISO_VERIFICACION
        senal = f"no_ancladas={verificacion.no_ancladas if verificacion else 0}"
    else:
        verificacion = None
        adversaria = None
        aviso = explicar.AVISO_INVARIANTE
        senal = f"invariante.residual_cent={factset.invariante.residual_cent}"

    context_ref = nuevo_context_ref(clave, trace_id)
    resumen_asesor = construir_resumen_asesor(
        factset,
        cuenta_id=cuenta,
        motivo_codigo=motivo_codigo,
        utterance=utterance,
        canal=str(canal),
        verificacion=str(verificacion.veredicto) if verificacion else None,
        detalle_motivo=motivo,
    )
    servicios.memoria.guardar_contexto(
        context_ref,
        construir_contexto_derivacion(
            context_ref=context_ref,
            trace_id=trace_id,
            conversation_id=clave,
            cuenta_id=cuenta,
            factset=factset,
            motivo_codigo=motivo_codigo,
            resumen_asesor=resumen_asesor,
            utterance=utterance,
            canal=str(canal),
            extra={"adversaria": adversaria} if adversaria else None,
        ),
    )
    servicios.memoria.marcar_derivada(clave)

    bloques: list[Bloque] = [BloqueAviso(severidad=severidad, texto=aviso)]  # type: ignore[list-item]
    derivacion = Derivacion(
        requerida=True,
        motivo=motivo,
        motivo_codigo=motivo_codigo,
        context_ref=context_ref,
        resumen_asesor=resumen_asesor,
        senal_disparadora=senal,
    )
    gobernanza = explicar._gobernanza_sin_cifras(factset, modelo="plantilla-determinista")
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

    sonda = servicios.telemetria.abrir_sonda(
        _conversacion(estado),
        trace_id,
        cuenta_ref=cuenta,
        periodo=factset.periodo_actual,
        canal=canal,
        utterance_origen=utterance,
        derivada=True,
        verificacion=verificacion.veredicto if verificacion else VeredictoVerificacion.NO_APLICA,
        ventana_s=servicios.ajustes.ventana_silencio_s,
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

    acciones = explicar._acciones_de_corte()
    respuesta = RespuestaCanalAgnostica(
        conversation_id=_conversacion(estado),
        trace_id=trace_id,
        bloques=bloques,
        acciones=acciones,
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria=telemetria_turno,
    )
    servicios.memoria.guardar_explicacion(
        RegistroExplicacion(
            explicacion_id=trace_id,
            trace_id=trace_id,
            conversation_id=clave,
            cuenta_ref=cuenta,
            periodo=factset.periodo_actual,
            factset=factset,
            respuesta=respuesta,
            utterance=utterance,
            canal=str(canal),
            derivada=True,
        )
    )
    servicios.auditoria.emitir(
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
    servicios.auditoria.emitir(
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
            "texto_entregado": respuesta.texto[: explicar.MAX_TEXTO_BITACORA],
        },
        **contexto_auditoria,
    )
    explicar._cerrar(servicios.auditoria, trace_id, cuenta, imprimir=servicios.ajustes.log_terminal)
    return {
        "bloques": bloques,
        "acciones": acciones,
        "derivacion": derivacion,
        "gobernanza": gobernanza,
        "telemetria": telemetria_turno,
        "context_ref": context_ref,
        "respuesta": redactar_para_nivel(respuesta, estado["nivel"], factset=factset),
        "eventos": ["ROUTE:regla_dura", "RESPONSE", "CHAIN"],
        "nodos": ["derivar"],
    }
