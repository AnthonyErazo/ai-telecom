"""Construye el :class:`PaqueteAsesor` **desde la bitácora encadenada**.

La tesis del proyecto es que el LLM nunca calcula y que cada cifra entregada se ancla
contra un ``FactSet`` sellado. El traspaso a un asesor humano es el punto donde esa
garantía se puede perder sin que nadie lo note: basta con que el paquete se arme desde
un estado paralelo —una caché, la memoria del proceso, un resumen guardado aparte— para
que el asesor lea algo que **no** es lo que se auditó, y lo repita al cliente con la
autoridad de una persona.

Por eso aquí se lee **solo la bitácora**:

===============  =========================================================================
Etapa            Qué aporta al paquete
===============  =========================================================================
``REQUEST``      cuenta, canal, nivel, conversación y lo que preguntó el cliente
``FACTS_BUILT``  totales, delta, líneas con su causa y confianza, causas agregadas, sha256
``INVARIANTE``   si el recibo concilia y con qué residual
``RETRIEVE``     qué documentación se usó (y si no se usó ninguna)
``ROUTE``        por qué se deriva: motivo, señal disparadora y score de incomprensión
``VERIFY``       veredicto y **cada cifra que se le dijo al cliente**, con su estado
``CITATIONS``    los ``fact_id`` citados
``RESPONSE``     el texto entregado, el modo y el ``context_ref``
``CHAIN``        cuántos eventos tiene el turno y si la cadena está íntegra
===============  =========================================================================

Los eventos ya están sellados por hash cuando se leen, así que el paquete hereda su
integridad: si alguien tocó un evento, :func:`verificar_cadena` lo delata y el paquete
lo declara en :attr:`EvidenciaAuditable.cadena_valida`.

El brief y el verificador
-------------------------
El brief que lee el asesor es texto **generado por el sistema**, luego se le aplica la
misma regla que a la respuesta al cliente: se redacta solo con cifras del paquete y
después se comprueba token a token con el mismo extractor del verificador numérico
(:func:`packages.llm_layer.verificador.extraer_aserciones`). La única excepción, y va
declarada, son las cifras **que escribió el cliente**: su pregunta se cita entre
comillas y esas cifras no son afirmaciones del sistema, así que se listan aparte en vez
de bloquear el brief.

Sobre la dirección del import
-----------------------------
Este módulo (gobernanza) importa el extractor de :mod:`packages.llm_layer.verificador`.
No hay ciclo —``llm_layer`` no conoce ``governance``— y la alternativa era peor: copiar
aquí la expresión regular que reconoce cifras en castellano peruano. Dos extractores
distintos significarían que el brief y la respuesta al cliente se verifican con
criterios distintos, que es exactamente lo que no puede pasar.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import (
    EtapaAuditoria,
    MotivoDerivacion,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.auditoria import EventoAuditoria
from packages.core_domain.esquemas.factset import (
    token_entero,
    token_monto,
    token_periodo,
    token_porcentaje,
)
from packages.core_domain.esquemas.paquete_asesor import (
    ACCION_PENDIENTE,
    CausaPaquete,
    CifraEntregada,
    EvidenciaAuditable,
    Incertidumbre,
    LineaPaquete,
    MotivoIncertidumbre,
    PaqueteAsesor,
    VerificacionBrief,
    YaExplicado,
)
from packages.llm_layer.verificador import extraer_aserciones

__all__ = [
    "CONFIANZA_MINIMA_CAUSA",
    "MAX_CONSULTA_EN_BRIEF",
    "construir_paquete_asesor",
    "redactar_brief",
    "tokens_del_paquete",
    "traza_de_context_ref",
    "verificar_brief",
]

_LOG = logging.getLogger(__name__)

#: Por debajo de esta confianza, la causa dominante se declara **hipótesis** y no hecho.
#: Coincide con el umbral con el que el motor considera atribuida una línea; se repite
#: aquí como constante propia porque su función es distinta: allí decide si se explica,
#: aquí decide si se **advierte** al asesor.
CONFIANZA_MINIMA_CAUSA = 0.60

#: Recorte de la pregunta del cliente dentro del brief. El brief se lee en ocho
#: segundos: una consulta de tres líneas lo convierte en otra cosa.
MAX_CONSULTA_EN_BRIEF = 90

#: Etiquetas del brief, en columna fija. El formato es rígido a propósito: quien lo lee
#: está con el cliente delante y busca el dato por posición, no leyendo. El ancho es el
#: de la etiqueta más larga (``NO CONFIRMADO``) más un espacio: si se quedara corto, esa
#: línea perdería la columna y el brief dejaría de escanearse de un vistazo.
_ANCHO_ETIQUETA = 14


# --------------------------------------------------------------------------- #
# Lectura de la bitácora
# --------------------------------------------------------------------------- #
def _ultimo(eventos: Sequence[EventoAuditoria], etapa: EtapaAuditoria) -> dict[str, Any]:
    """Payload del último evento de una etapa (``{}`` si el turno no llegó a ella).

    Se toma el **último** y no el primero porque un turno puede reintentar una etapa
    —el generador reintenta una vez tras un FAIL— y lo que vale es el estado final, que
    es el que se le entregó al cliente.
    """
    for evento in reversed(eventos):
        if evento.etapa is etapa:
            return dict(evento.payload)
    return {}


def _ultimo_con(
    eventos: Sequence[EventoAuditoria], etapa: EtapaAuditoria, clave: str
) -> dict[str, Any]:
    """Payload del último evento de una etapa que **declara** una clave con valor.

    Hace falta porque un caso tiene varios turnos y no todos hablan de lo mismo: el
    turno de derivación emite ``RESPONSE`` sin texto entregado, y el de explicación lo
    emitió con él. Quedarse con el último ``RESPONSE`` a secas perdería el texto; buscar
    el último que **lo trae** lo conserva, y de paso conserva el resto de su payload
    —el modo, la latencia—, que es información del mismo hecho y no de otro.
    """
    for evento in reversed(eventos):
        if evento.etapa is etapa and evento.payload.get(clave) not in (None, "", [], {}):
            return dict(evento.payload)
    return {}


def _eventos_del_caso(
    eventos: Sequence[EventoAuditoria], del_turno: Sequence[EventoAuditoria]
) -> list[EventoAuditoria]:
    """Todos los eventos de la conversación del turno ancla, hasta ese turno incluido.

    La conversación se identifica por ``conversation_id``, que viaja en el payload de
    ``REQUEST`` y en los eventos de sala del asesor. Si el turno ancla no la declara
    —puede pasar en un turno que no abrió petición—, el caso se reduce al propio turno:
    ante la duda, menos contexto y ninguno inventado.
    """
    conversacion = None
    for evento in del_turno:
        candidata = evento.payload.get("conversation_id")
        if candidata:
            conversacion = str(candidata)
            break
    if conversacion is None:
        return list(del_turno)

    ultimo_indice = max(evento.indice for evento in del_turno)
    trazas: dict[str, None] = {}
    for evento in eventos:
        if str(evento.payload.get("conversation_id") or "") == conversacion:
            trazas.setdefault(evento.trace_id, None)
    return [
        evento
        for evento in eventos
        if evento.trace_id in trazas and evento.indice <= ultimo_indice
    ]


def _entero(valor: Any) -> int | None:
    """Convierte a ``int`` sin fingir: si no se puede, devuelve ``None``."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _decimal(valor: Any) -> float | None:
    """Convierte a ``float`` con tolerancia (las confianzas viajan como número o texto)."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def traza_de_context_ref(eventos: Sequence[EventoAuditoria], context_ref: str) -> str | None:
    """Encuentra el turno que **acuñó** un ``context_ref``, mirando solo la bitácora.

    El ``context_ref`` aparece en el payload de ``ROUTE`` o de ``RESPONSE`` según por
    dónde se derivara (regla dura o umbral de incomprensión), así que se busca en
    cualquier evento en lugar de fijar una etapa: la referencia es la misma y atarla a
    una etapa concreta rompería en cuanto cambiara el camino de derivación.

    Se devuelve la **primera** aparición, no la última, y esa elección tiene una razón
    concreta: la bitácora sigue creciendo después de la derivación, y entre lo que se
    escribe está el propio acceso del asesor al expediente, que también nombra la
    referencia. Buscando hacia atrás, la segunda consulta del paquete encontraba el
    evento de la primera y devolvía un expediente vacío. Un ``context_ref`` lo acuña un
    único turno —se deriva de ``(conversation_id, trace_id)``—, así que la primera
    aparición es la correcta por construcción y es inmune a lo que se escriba después.

    Returns:
        El ``trace_id`` del turno que creó esa referencia, o ``None``.
    """
    for evento in eventos:
        if evento.payload.get("context_ref") == context_ref:
            return evento.trace_id
    return None


# --------------------------------------------------------------------------- #
# Piezas del paquete
# --------------------------------------------------------------------------- #
def _lineas(hechos: dict[str, Any]) -> list[LineaPaquete]:
    """Las líneas del delta que la bitácora guardó en ``FACTS_BUILT``."""
    crudas = hechos.get("lineas_delta")
    if not isinstance(crudas, list):
        return []
    lineas: list[LineaPaquete] = []
    for cruda in crudas:
        if not isinstance(cruda, dict):
            continue
        try:
            lineas.append(
                LineaPaquete(
                    concepto_id=str(cruda.get("concepto_id", "")),
                    nombre_comercial=str(cruda.get("nombre_comercial", "")),
                    clase=str(cruda.get("clase", "")),
                    monto_previo_cent=_entero(cruda.get("monto_previo_cent")) or 0,
                    monto_actual_cent=_entero(cruda.get("monto_actual_cent")) or 0,
                    delta_cent=_entero(cruda.get("delta_cent")) or 0,
                    causa=cruda.get("causa") or cruda.get("causa_oficial"),
                    confianza=_decimal(cruda.get("confianza")) or 0.0,
                    atribuida=bool(cruda.get("atribuida")),
                )
            )
        except ValueError as error:  # un evento antiguo o corrupto no tumba el paquete
            _LOG.warning("línea ilegible en la bitácora: %s", error)
    return sorted(lineas, key=lambda linea: (-abs(linea.delta_cent), linea.concepto_id))


def _causas(hechos: dict[str, Any]) -> list[CausaPaquete]:
    """Las causas agregadas con su importe, su peso y su confianza."""
    crudas = hechos.get("causas_detalle")
    if not isinstance(crudas, list):
        return []
    causas: list[CausaPaquete] = []
    for cruda in crudas:
        if not isinstance(cruda, dict):
            continue
        try:
            causas.append(
                CausaPaquete(
                    etiqueta_cliente=str(cruda.get("etiqueta_cliente", "")),
                    causa=cruda.get("causa") or cruda.get("causa_oficial"),
                    monto_cent=_entero(cruda.get("monto_cent")) or 0,
                    participacion_bp=_entero(cruda.get("participacion_bp")) or 0,
                    confianza=_decimal(cruda.get("confianza")) or 0.0,
                    movimientos=[
                        numero
                        for numero in (_entero(item) for item in cruda.get("movimientos", []))
                        if numero is not None
                    ],
                )
            )
        except ValueError as error:
            _LOG.warning("causa ilegible en la bitácora: %s", error)
    return sorted(causas, key=lambda causa: (-abs(causa.monto_cent), causa.etiqueta_cliente))


def _ya_explicado(eventos: Sequence[EventoAuditoria]) -> YaExplicado:
    """Qué recibió el cliente: el texto, el modo, el veredicto y cifra por cifra."""
    verificacion = _ultimo_con(eventos, EtapaAuditoria.VERIFY, "aserciones")
    # El texto y su modo se toman del mismo evento: son dos caras del mismo hecho.
    respuesta = _ultimo_con(eventos, EtapaAuditoria.RESPONSE, "texto_entregado")
    citas = _ultimo_con(eventos, EtapaAuditoria.CITATIONS, "fact_ids")
    ruta = _ultimo_con(eventos, EtapaAuditoria.ROUTE, "score_incomprension")

    cifras: list[CifraEntregada] = []
    for cruda in verificacion.get("aserciones", []) or []:
        if not isinstance(cruda, dict):
            continue
        cifras.append(
            CifraEntregada(
                texto=str(cruda.get("texto_original", "")),
                # ``token`` viaja como ``token_normalizado`` porque en la bitácora la
                # clave ``token`` significa *credencial* y se redacta (CLAVES_SENSIBLES).
                token=str(cruda.get("token_normalizado") or cruda.get("token") or ""),
                estado=str(cruda.get("estado", "")),
                fuente=cruda.get("fuente"),
            )
        )

    texto = respuesta.get("texto_entregado")
    return YaExplicado(
        hubo_explicacion=bool(texto) or bool(cifras),
        texto=str(texto) if texto else None,
        modo=str(respuesta["modo"]) if respuesta.get("modo") else None,
        veredicto=(
            str(verificacion["veredicto"])
            if verificacion.get("veredicto")
            else respuesta.get("verificacion_numerica")
        ),
        cifras=cifras,
        citas=[str(item) for item in (citas.get("fact_ids") or [])],
        score_incomprension=_decimal(ruta.get("score_incomprension")),
    )


def _incertidumbres(
    eventos: Sequence[EventoAuditoria],
    *,
    lineas: Sequence[LineaPaquete],
    causas: Sequence[CausaPaquete],
    ya_explicado: YaExplicado,
    cadena_valida: bool,
) -> list[Incertidumbre]:
    """Lo que el sistema **no** pudo confirmar, con su porqué y su impacto.

    Es el campo que convierte un volcado de datos en un traspaso responsable. Un asesor
    que recibe cifras sin saber cuáles son hipótesis las confirma al cliente, y entonces
    el error deja de ser del motor para pasar a ser de la operadora.
    """
    hechos = _ultimo(eventos, EtapaAuditoria.FACTS_BUILT)
    invariante = _ultimo(eventos, EtapaAuditoria.INVARIANTE)
    pendientes: list[Incertidumbre] = []

    if not hechos:
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.SIN_HECHOS,
                detalle=(
                    "no se llegó a abrir el recibo en este turno: no hay cifras que "
                    "confirmar ni desmentir"
                ),
                evidencia=["FACTS_BUILT"],
            )
        )

    residual = _entero(invariante.get("residual_cent"))
    if residual is None:
        residual = _entero(hechos.get("residual_cent"))
    ok = invariante.get("ok", hechos.get("invariante_ok", True))
    if residual is not None and not ok:
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.INVARIANTE_ROTO,
                detalle=(
                    f"el detalle no cuadra con la diferencia de totales por "
                    f"{abs(residual)} céntimos: no confirmar importes sin revisar con "
                    "facturación"
                ),
                impacto_cent=residual,
                evidencia=["INVARIANTE", "invariante:residual_cent"],
            )
        )

    sin_atribuir = [linea for linea in lineas if not linea.atribuida]
    if sin_atribuir:
        impacto = sum(linea.delta_cent for linea in sin_atribuir)
        conceptos = ", ".join(linea.nombre_comercial or linea.concepto_id for linea in sin_atribuir)
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.LINEA_SIN_ATRIBUIR,
                detalle=(
                    f"{len(sin_atribuir)} concepto(s) varían sin causa identificada "
                    f"({conceptos}): el motor ve el cuánto, no el porqué"
                ),
                impacto_cent=impacto,
                evidencia=[f"linea:{linea.concepto_id}.delta_cent" for linea in sin_atribuir],
            )
        )

    dudosas = [causa for causa in causas if causa.confianza < CONFIANZA_MINIMA_CAUSA]
    if dudosas:
        peor = min(dudosas, key=lambda causa: causa.confianza)
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.CAUSA_POCO_FIABLE,
                detalle=(
                    f"la causa «{peor.etiqueta_cliente}» es una hipótesis del motor "
                    f"(confianza {round(peor.confianza * 100)} %), no un hecho confirmado "
                    "por una orden de servicio: contrastarla antes de afirmarla"
                ),
                impacto_cent=sum(causa.monto_cent for causa in dudosas),
                evidencia=[f"causa:{causa.causa or causa.etiqueta_cliente}" for causa in dudosas],
            )
        )

    no_ancladas = [cifra for cifra in ya_explicado.cifras if cifra.estado == "NO_ANCLADA"]
    if no_ancladas:
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.CIFRA_NO_ANCLADA,
                detalle=(
                    f"el verificador bloqueó {len(no_ancladas)} cifra(s) sin respaldo en "
                    "el recibo: no se entregaron al cliente y tampoco deben entregarse ahora"
                ),
                evidencia=["VERIFY"] + [cifra.token for cifra in no_ancladas],
            )
        )

    if not ya_explicado.hubo_explicacion:
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.SIN_EXPLICACION_ENTREGADA,
                detalle=(
                    "al cliente aún no se le entregó ninguna explicación: empieza usted "
                    "la conversación, no la retoma"
                ),
                evidencia=["RESPONSE"],
            )
        )

    if not cadena_valida:
        pendientes.append(
            Incertidumbre(
                codigo=MotivoIncertidumbre.CADENA_ROTA,
                detalle=(
                    "la cadena de hashes de la bitácora no valida: este paquete no puede "
                    "usarse como evidencia hasta que se revise"
                ),
                evidencia=["CHAIN"],
            )
        )
    return pendientes


# --------------------------------------------------------------------------- #
# Tokens permitidos del paquete y verificación del brief
# --------------------------------------------------------------------------- #
def _porcentaje_entero(confianza: float) -> int:
    """La confianza como porcentaje entero. Se calcula **una vez** y se usa dos veces.

    El brief la escribe y el conjunto permitido la ancla; si cada uno la redondeara por
    su cuenta, un 0.305 saldría escrito como ``31 %`` y anclado como ``30.50 %``, y el
    verificador bloquearía un brief correcto. La regla general del proyecto es que la
    cifra se calcula en un sitio y viaja, nunca se recalcula en el borde.
    """
    return round(confianza * 100)


def tokens_del_paquete(paquete: PaqueteAsesor) -> dict[str, str]:
    """``ALLOWED`` del brief: cada cifra que el paquete tiene derecho a escribir.

    Es el equivalente de :meth:`FactSet.tokens_permitidos` para este texto, con la misma
    lógica y la misma finalidad: si una cifra del brief no está aquí, es una invención.

    Se ancla en tres bloques:

    1. **Cifras del recibo** — totales, delta, deuda, residual, líneas, causas, sus
       participaciones y sus confianzas; periodos y fecha de vencimiento.
    2. **Cifras dentro de textos ya sellados** — los nombres comerciales
       (*"Paquete 5 GB"*), las etiquetas de causa y los detalles de incertidumbre llevan
       dígitos dentro. Se anclan pasando **el mismo extractor** por esos textos, no una
       expresión regular propia: así lo anclado es exactamente lo que el verificador va
       a leer, con el ``fact_id`` del campo del que salió. Es el mismo criterio que usa
       ``_anclar_enteros_de_textos`` para la respuesta al cliente.
    3. **Cifras que el brief dice de sí mismo** — cuántos eventos tiene la bitácora,
       cuántas cifras se le dieron ya al cliente, cuántas incertidumbres quedan. No
       vienen del recibo, así que se declaran una a una y con nombre propio.

    Returns:
        ``{token: fact_id}``, donde ``fact_id`` es la ruta legible de su respaldo.
    """
    permitidos: dict[str, str] = {}

    def anotar(token: str, fuente: str) -> None:
        permitidos.setdefault(token, fuente)

    def anotar_monto(centimos: int | None, fuente: str) -> None:
        if centimos is None:
            return
        anotar(token_monto(centimos), fuente)
        anotar(token_monto(abs(centimos)), fuente)

    def anotar_texto(texto: str | None, fuente: str) -> None:
        """Ancla lo que el extractor leería en un texto ya sellado en la bitácora."""
        for asercion in extraer_aserciones(texto or ""):
            anotar(asercion.token, fuente)

    # --- 1. Cifras del recibo ------------------------------------------- #
    anotar_monto(paquete.total_actual_cent, "factset:total_actual_cent")
    anotar_monto(paquete.total_previo_cent, "factset:total_previo_cent")
    anotar_monto(paquete.delta_total_cent, "factset:delta_total_cent")
    anotar_monto(paquete.deuda_anterior_cent, "factset:deuda_anterior_cent")
    anotar_monto(paquete.total_a_pagar_cent, "factset:total_a_pagar_cent")
    if paquete.residual_cent is not None:
        anotar_monto(paquete.residual_cent, "invariante:residual_cent")
        # El brief dice el residual en céntimos ("41 céntimos"), no en soles: el asesor
        # necesita el descuadre exacto, no su redondeo.
        anotar(token_entero(abs(paquete.residual_cent)), "invariante:residual_cent")

    for periodo in (paquete.periodo_actual, paquete.periodo_previo):
        if periodo:
            anotar(token_periodo(periodo), "factset:periodo")
            anotar(token_entero(int(periodo[:4])), "factset:periodo.anio")

    if paquete.fecha_vencimiento:
        # El brief escribe la fecha en formato peruano (dd/mm/aaaa); el extractor la
        # normaliza a ``fecha:aaaa-mm-dd``, que es el token que se ancla aquí.
        anotar(f"fecha:{paquete.fecha_vencimiento}", "factset:fecha_vencimiento")

    for linea in paquete.lineas:
        base = f"linea:{linea.concepto_id}"
        anotar_monto(linea.delta_cent, f"{base}.delta_cent")
        anotar_monto(linea.monto_actual_cent, f"{base}.monto_actual_cent")
        anotar_monto(linea.monto_previo_cent, f"{base}.monto_previo_cent")
        anotar(
            token_porcentaje(_porcentaje_entero(linea.confianza)), f"{base}.confianza"
        )
        anotar_texto(linea.nombre_comercial, f"texto:{base}.nombre_comercial")

    for causa in paquete.causas:
        ref = f"causa:{causa.causa or causa.etiqueta_cliente}"
        anotar_monto(causa.monto_cent, f"{ref}.monto_cent")
        anotar(
            token_porcentaje((Decimal(causa.participacion_bp) / 100).quantize(Decimal("0.01"))),
            f"{ref}.participacion",
        )
        anotar(token_porcentaje(_porcentaje_entero(causa.confianza)), f"{ref}.confianza")
        for movimiento in causa.movimientos:
            anotar(token_entero(movimiento), f"{ref}.movimiento")
        anotar_texto(causa.etiqueta_cliente, f"texto:{ref}.etiqueta")

    # --- 2. Cifras dentro de textos ya sellados -------------------------- #
    for indice, item in enumerate(paquete.incertidumbres):
        anotar_texto(item.detalle, f"texto:incertidumbre:{indice}.{item.codigo}")
    anotar_texto(paquete.accion_pendiente, "texto:paquete:accion_pendiente")
    anotar_texto(paquete.motivo_detalle, "texto:route:senal_disparadora")
    anotar_texto(paquete.motivo_codigo, "texto:route:motivo_codigo")
    anotar_texto(paquete.modalidad_renta, "texto:factset:modalidad_renta")
    anotar_texto(paquete.canal, "texto:request:canal")

    # --- 3. Cifras que el brief dice de sí mismo -------------------------- #
    if paquete.cuenta_id and paquete.cuenta_id.isdigit():
        # La cuenta es un dato de la bitácora (``cuenta_ref``), no una cifra calculada,
        # pero el extractor la lee como un entero: sin anclarla, el brief que la nombra
        # —y tiene que nombrarla— fallaría la verificación.
        anotar(token_entero(int(paquete.cuenta_id)), "bitacora:cuenta_ref")
    anotar(token_entero(paquete.evidencia.eventos), "bitacora:eventos")
    anotar(token_entero(len(paquete.ya_explicado.cifras)), "verify:aserciones_totales")
    anotar(token_entero(len(paquete.incertidumbres)), "paquete:incertidumbres")
    anotar(token_entero(max(0, len(paquete.incertidumbres) - 1)), "paquete:incertidumbres.resto")
    return permitidos


def verificar_brief(
    texto: str, permitidos: dict[str, str], *, cita_cliente: str = ""
) -> VerificacionBrief:
    """Pasa el brief por el verificador numérico: cada cifra, contra el paquete.

    Args:
        texto: el brief redactado.
        permitidos: salida de :func:`tokens_del_paquete`.
        cita_cliente: la pregunta del cliente tal y como aparece dentro del brief. Las
            cifras que caigan **dentro** de ese tramo no se juzgan: las escribió el
            cliente y el sistema no las afirma. Se listan aparte, que es la forma
            honesta de no bloquear un brief correcto ni avalar una cifra ajena.

    Returns:
        El veredicto, con la lista de tokens sin anclar (vacía si todo cuadra).
    """
    inicio_cita, fin_cita = -1, -1
    if cita_cliente:
        inicio_cita = texto.find(cita_cliente)
        if inicio_cita >= 0:
            fin_cita = inicio_cita + len(cita_cliente)

    ancladas = 0
    no_ancladas: list[str] = []
    citadas: list[str] = []
    aserciones = extraer_aserciones(texto)
    for asercion in aserciones:
        # Sin posición no se puede afirmar que la cifra esté dentro de la cita, y ante
        # la duda se juzga: es la regla de todo el verificador.
        situada = asercion.inicio is not None and asercion.fin is not None
        dentro_de_la_cita = (
            inicio_cita >= 0
            and situada
            and asercion.inicio >= inicio_cita
            and asercion.fin <= fin_cita
        )
        if dentro_de_la_cita:
            citadas.append(asercion.token)
            continue
        if asercion.token in permitidos:
            ancladas += 1
        else:
            no_ancladas.append(asercion.token)

    return VerificacionBrief(
        veredicto=(
            VeredictoVerificacion.PASS if not no_ancladas else VeredictoVerificacion.FAIL
        ),
        aserciones_totales=len(aserciones),
        ancladas=ancladas,
        no_ancladas=no_ancladas,
        citadas_del_cliente=citadas,
        tokens_permitidos=len(permitidos),
    )


# --------------------------------------------------------------------------- #
# Brief
# --------------------------------------------------------------------------- #
def _etiqueta(nombre: str, cuerpo: str) -> str:
    """Una línea del brief: etiqueta en mayúsculas y el dato siempre en la misma columna."""
    return f"{nombre.ljust(_ANCHO_ETIQUETA)}{cuerpo}"


def _fecha_peruana(iso: str | None) -> str:
    """``2026-07-12`` → ``12/07/2026``. Es como se lee un vencimiento en Perú."""
    if not iso:
        return "s/f"
    partes = iso.split("-")
    if len(partes) != 3:
        return iso
    return f"{partes[2]}/{partes[1]}/{partes[0]}"


def redactar_brief(paquete: PaqueteAsesor) -> tuple[str, str]:
    """Redacta la ficha del asesor **solo con cifras del paquete**.

    Mismo formato que el brief de ``POST /v1/derivacion`` —etiqueta en mayúsculas, dato
    en columna fija, una pregunta por línea— porque el asesor no debería aprender dos
    fichas distintas del mismo producto. La diferencia es de procedencia: aquella se
    redacta desde el ``FactSet`` en el momento de derivar; esta, desde la bitácora ya
    sellada, y por eso es la que se puede verificar después.

    Returns:
        ``(brief, cita_cliente)``. La cita se devuelve aparte porque
        :func:`verificar_brief` necesita saber qué tramo del texto son palabras del
        cliente y no afirmaciones del sistema.
    """
    consulta = " ".join((paquete.consulta_cliente or "").split())[:MAX_CONSULTA_EN_BRIEF]
    cita = f"«{consulta}»" if consulta else "«no registrada»"

    lineas = [
        _etiqueta(
            "CLIENTE",
            f"{paquete.cuenta_id or 'sin cuenta'} · recibo {paquete.periodo_actual or 's/p'} · "
            f"renta {paquete.modalidad_renta or 'n/d'} · vence "
            f"{_fecha_peruana(paquete.fecha_vencimiento)}",
        ),
        _etiqueta("CONSULTA", f"{cita} · canal {paquete.canal}"),
    ]

    if paquete.delta_total_cent is not None:
        lineas.append(
            _etiqueta(
                "VARIACIÓN",
                f"{formatear_soles(paquete.total_previo_cent or 0)} → "
                f"{formatear_soles(paquete.total_actual_cent or 0)} "
                f"({formatear_soles(paquete.delta_total_cent)})",
            )
        )
    else:
        lineas.append(_etiqueta("VARIACIÓN", "no se abrió el recibo en este turno"))

    if paquete.deuda_anterior_cent:
        lineas.append(
            _etiqueta(
                "DEUDA",
                f"arrastra {formatear_soles(paquete.deuda_anterior_cent)} · total a pagar "
                f"{formatear_soles(paquete.total_a_pagar_cent or 0)}",
            )
        )

    if not paquete.invariante_ok and paquete.residual_cent is not None:
        lineas.append(
            _etiqueta(
                "DESCUADRE",
                f"residual de {abs(paquete.residual_cent)} céntimos: no confirmar importes "
                "sin revisar",
            )
        )

    if paquete.causas:
        dominante = paquete.causas[0]
        participacion = (Decimal(dominante.participacion_bp) / 100).quantize(Decimal("0.01"))
        lineas.append(
            _etiqueta(
                "CAUSA",
                f"{dominante.etiqueta_cliente} · {formatear_soles(dominante.monto_cent)} "
                f"({participacion}%) · confianza {_porcentaje_entero(dominante.confianza)} %",
            )
        )
    else:
        lineas.append(_etiqueta("CAUSA", "sin causa atribuida por el motor"))

    if paquete.ya_explicado.hubo_explicacion:
        cifras = len(paquete.ya_explicado.cifras)
        lineas.append(
            _etiqueta(
                "YA EXPLICADO",
                f"explicación entregada · modo {paquete.ya_explicado.modo or 'PLANTILLA'} · "
                f"verificación {paquete.ya_explicado.veredicto or 'NO_APLICA'} · "
                f"{cifras} cifra(s) ya en manos del cliente",
            )
        )
    else:
        lineas.append(_etiqueta("YA EXPLICADO", "aún no se le entregó explicación"))

    if paquete.incertidumbres:
        primera = paquete.incertidumbres[0]
        resto = len(paquete.incertidumbres) - 1
        sufijo = f" (+{resto} más)" if resto > 0 else ""
        lineas.append(_etiqueta("NO CONFIRMADO", f"{primera.detalle}{sufijo}"))
    else:
        lineas.append(_etiqueta("NO CONFIRMADO", "nada: todas las cifras están respaldadas"))

    if paquete.motivo_codigo:
        detalle = paquete.motivo_detalle or paquete.motivo_codigo
        lineas.append(_etiqueta("DERIVA POR", detalle))
    else:
        # El paquete se puede pedir sobre un turno que **no** derivó: un asesor que
        # entra por iniciativa propia también necesita el contexto. Decir "sin motivo"
        # sonaría a dato perdido; esto dice lo que pasó.
        lineas.append(
            _etiqueta("DERIVA POR", "este turno no derivó: contexto pedido por el asesor")
        )
    lineas.append(_etiqueta("PENDIENTE", paquete.accion_pendiente))
    lineas.append(
        _etiqueta(
            "EVIDENCIA",
            f"bitácora de {paquete.evidencia.eventos} eventos · "
            + ("cadena íntegra" if paquete.evidencia.cadena_valida else "CADENA ROTA")
            + f" · {paquete.evidencia.consulta_auditoria}",
        )
    )
    return "\n".join(lineas), cita


# --------------------------------------------------------------------------- #
# Constructor
# --------------------------------------------------------------------------- #
def construir_paquete_asesor(
    eventos: Sequence[EventoAuditoria],
    *,
    trace_id: str,
    context_ref: str | None = None,
    cadena_valida: bool = True,
    indice_roto: int | None = None,
) -> PaqueteAsesor:
    """Arma el paquete del asesor con los eventos de un turno, y solo con ellos.

    Args:
        eventos: eventos de la bitácora **de un único turno**, en orden de escritura.
        trace_id: el turno; se usa tal cual en la referencia de evidencia.
        context_ref: referencia del expediente, si el turno derivó.
        cadena_valida: resultado de recorrer la cadena de hashes del fichero.
        indice_roto: primer índice que falla, si la cadena no valida.

    Returns:
        El paquete, con el brief ya redactado y **ya verificado**.

    Raises:
        ValueError: si no se pasa ningún evento. Un paquete sin bitácora sería
            exactamente la derivación a ciegas que este proyecto existe para eliminar.
    """
    del_turno = [evento for evento in eventos if evento.trace_id == trace_id]
    if not del_turno:
        raise ValueError(f"no hay eventos auditados para la traza {trace_id}")

    # El paquete describe un **caso**, no un turno suelto. Un cliente que pide asesor
    # después de que se le explicara el recibo genera dos trazas: la explicación y la
    # derivación. Si el paquete mirase solo la segunda diría "aún no se le entregó
    # explicación" —falso, y peligroso: el asesor volvería a explicar lo ya explicado—.
    # Se recorre por tanto toda la conversación hasta el turno ancla, y cada dato se
    # toma del evento **más reciente** que lo declara, que es el turno ancla cuando lo
    # trae y el anterior cuando no.
    del_caso = _eventos_del_caso(eventos, del_turno)

    peticion = _ultimo_con(del_caso, EtapaAuditoria.REQUEST, "utterance") or _ultimo(
        del_caso, EtapaAuditoria.REQUEST
    )
    hechos = _ultimo(del_caso, EtapaAuditoria.FACTS_BUILT)
    invariante = _ultimo(del_caso, EtapaAuditoria.INVARIANTE)
    # El motivo se busca donde consta: en la ruta que decidió derivar, sea de este turno
    # o del que abrió el expediente.
    ruta = _ultimo_con(del_caso, EtapaAuditoria.ROUTE, "motivo_codigo")
    respuesta = _ultimo_con(del_caso, EtapaAuditoria.RESPONSE, "context_ref")

    lineas = _lineas(hechos)
    causas = _causas(hechos)
    ya_explicado = _ya_explicado(del_caso)
    incertidumbres = _incertidumbres(
        del_caso,
        lineas=lineas,
        causas=causas,
        ya_explicado=ya_explicado,
        cadena_valida=cadena_valida,
    )

    codigo_bruto = ruta.get("motivo_codigo") or respuesta.get("motivo_codigo")
    motivo_codigo = str(codigo_bruto) if codigo_bruto else None
    try:
        motivo = MotivoDerivacion(motivo_codigo) if motivo_codigo else None
    except ValueError:  # un motivo que ya no existe en el enum no puede tumbar el paquete
        motivo = None

    total_actual = _entero(hechos.get("total_actual_cent"))
    deuda = _entero(hechos.get("deuda_anterior_cent"))
    total_a_pagar = _entero(hechos.get("total_a_pagar_cent"))
    if total_a_pagar is None and total_actual is not None:
        total_a_pagar = total_actual + (deuda or 0)

    cuenta = next((evento.cuenta_ref for evento in del_turno if evento.cuenta_ref), None)
    residual = _entero(invariante.get("residual_cent"))
    if residual is None:
        residual = _entero(hechos.get("residual_cent"))

    paquete = PaqueteAsesor(
        context_ref=context_ref or ruta.get("context_ref") or respuesta.get("context_ref"),
        conversation_id=peticion.get("conversation_id"),
        cuenta_id=cuenta,
        canal=str(peticion.get("canal") or "APP"),
        nivel=str(del_turno[0].nivel) if del_turno[0].nivel else peticion.get("nivel"),
        generado_en=datetime.now(UTC),
        motivo_codigo=motivo_codigo,
        motivo_detalle=ruta.get("senal_disparadora") or ruta.get("motivo"),
        accion_pendiente=ACCION_PENDIENTE.get(motivo, "atender la consulta del cliente")
        if motivo
        else "atender la consulta del cliente",
        consulta_cliente=str(peticion.get("utterance") or ""),
        periodo_actual=hechos.get("periodo_actual"),
        periodo_previo=hechos.get("periodo_previo"),
        total_previo_cent=_entero(hechos.get("total_previo_cent")),
        total_actual_cent=total_actual,
        delta_total_cent=_entero(hechos.get("delta_total_cent")),
        deuda_anterior_cent=deuda,
        total_a_pagar_cent=total_a_pagar,
        fecha_vencimiento=hechos.get("fecha_vencimiento"),
        modalidad_renta=hechos.get("modalidad_renta"),
        lineas=lineas,
        causas=causas,
        residual_cent=residual,
        invariante_ok=bool(invariante.get("ok", hechos.get("invariante_ok", True))),
        confianza_global=_decimal(hechos.get("confianza_global")),
        ya_explicado=ya_explicado,
        incertidumbres=incertidumbres,
        evidencia=EvidenciaAuditable(
            trace_id=trace_id,
            trazas=list(dict.fromkeys(evento.trace_id for evento in del_caso)),
            factset_sha256=hechos.get("factset_sha256") or hechos.get("sha256"),
            hash_ultimo_evento=del_turno[-1].hash,
            eventos=len(del_caso),
            etapas=[str(evento.etapa) for evento in del_turno],
            cadena_valida=cadena_valida,
            indice_roto=indice_roto,
            consulta_auditoria=f"GET /v1/auditoria?trace_id={trace_id}",
        ),
    )

    # El brief se redacta al final, cuando el paquete ya tiene todos sus datos, y se
    # verifica contra el propio paquete: es la misma regla que se le aplica al texto
    # que ve el cliente, aplicada al texto que ve el asesor.
    brief, cita = redactar_brief(paquete)
    verificacion = verificar_brief(brief, tokens_del_paquete(paquete), cita_cliente=cita)
    if verificacion.veredicto is VeredictoVerificacion.FAIL:
        _LOG.error(
            "brief del asesor con cifras sin anclar en la traza %s: %s",
            trace_id,
            verificacion.no_ancladas,
        )
    paquete.brief = brief
    paquete.verificacion_brief = verificacion
    return paquete
