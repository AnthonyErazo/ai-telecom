"""Las tres métricas oficiales de la ficha, con su protocolo exacto (sección 10).

La ficha del Desafío 1 enuncia tres métricas técnicas, literalmente::

    "Precisión de Recuperación (Retrieval Accuracy): capacidad del modelo para extraer
     el dato exacto de la base proporcionada."
    "Tasa de Alucinación: cero invenciones financieras COMPROBABLES MEDIANTE LOGS DE LA
     TERMINAL."
    "Precisión del Hand-off: exactitud lógica al decidir cuándo derivar a un humano
     basándose en UMBRALES DE INCOMPRENSIÓN."

Este módulo las convierte en números reproducibles. Tres decisiones de protocolo que
conviene tener a la vista antes de leer una sola cifra:

1. **La precisión se mide en tres capas, no en una.** Un porcentaje de campos correctos
   se puede inflar contestando muchos campos fáciles. Por eso la cifra de titular es la
   capa (C), *strict answer accuracy*: el porcentaje de respuestas en las que **todos**
   los campos son exactos al céntimo. Una respuesta con un campo mal es una respuesta
   mal, porque el cliente no puede saber cuál es el campo malo.

2. **La tasa de alucinación se mide por respuesta, no solo por aserción.** ``TA_asercion``
   diluye: una respuesta con cuarenta cifras y una inventada da 2,5 %. Lo que se
   compromete es ``TA_respuesta = 0``, que es lo que le importa a un cliente al que se
   le está explicando su dinero.

3. **El hand-off tiene errores asimétricos.** No derivar cuando había que derivar (falso
   negativo) deja al cliente atrapado con una explicación que no entiende; derivar de
   más (falso positivo) solo cuesta minutos de asesor. Por eso la métrica primaria es
   ``Recall_handoff`` y se reporta además ``F2``, que pesa el recall el doble que la
   precisión.

.. warning::
   **ADVERTENCIA DE CIRCULARIDAD.** El ground truth y el sistema evaluado comparten
   autor. Estas cifras validan la **mecánica del motor** (que el prorrateo cierra, que
   el diff concilia, que ninguna cifra escapa del FactSet); **no predicen el desempeño
   sobre datos reales de Movistar**. Ver :data:`ADVERTENCIA_CIRCULARIDAD`.
"""

from __future__ import annotations

import statistics
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Any

from eval.datos import (
    CuentaSintetica,
    cargar_cuenta,
    factset_de_cuenta,
    ground_truth_de,
)
from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import (
    Canal,
    ModalidadRenta,
    ModoGeneracion,
    TipoMovimiento,
    Verbosidad,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.evaluacion import CasoGolden, GroundTruthCausaDelta
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import Derivacion, RespuestaCanalAgnostica
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas
from packages.facts_engine.atribucion import CONCEPTOS_DERIVADOS
from packages.facts_engine.confianza import ResultadoIncomprension, Turno, evaluar_incomprension
from packages.facts_engine.motor import resumen_de_conciliacion
from packages.llm_layer import explicar

__all__ = [
    "ADVERTENCIA_CIRCULARIDAD",
    "CAMPOS_HANDOFF",
    "CONCEPTO_DEUDA_ANTERIOR",
    "MAX_TURNOS_SONDEO",
    "NAMESPACE_EVAL",
    "CampoRecuperacion",
    "InformeEvaluacion",
    "MetricasAlucinacion",
    "MetricasApoyo",
    "MetricasHandoff",
    "MetricasRecuperacion",
    "ObservacionCaso",
    "agregar",
    "campos_de_recuperacion",
    "ejecutar_caso",
    "ejecutar_suite",
    "esperado_por_concepto",
    "resumen_para_asesor",
    "turnos_hasta_derivar",
]

#: Texto que ``run_eval.py`` imprime de forma destacada. Es un requisito de la
#: especificación (sección 10) y una exigencia de honestidad intelectual: sin esta
#: advertencia, las cifras de abajo se leerían como una promesa de desempeño en
#: producción, y no lo son.
ADVERTENCIA_CIRCULARIDAD = (
    "ADVERTENCIA DE CIRCULARIDAD — LÉASE ANTES QUE CUALQUIER CIFRA\n"
    "El ground truth de esta evaluación y el sistema evaluado COMPARTEN AUTOR: ambos\n"
    "salen del mismo modelo de negocio y del mismo generador sintético. Por tanto:\n"
    "  · Lo que estas cifras SÍ demuestran: que la mecánica del motor es correcta y\n"
    "    reproducible (el prorrateo cierra, el diff concilia al céntimo, ninguna cifra\n"
    "    de la respuesta escapa del FactSet y el hand-off se dispara donde debe).\n"
    "  · Lo que estas cifras NO demuestran: el desempeño sobre datos reales de\n"
    "    Movistar. Un dataset real traerá conceptos fuera de catálogo, descuadres de\n"
    "    origen y casuísticas que este generador no imagina.\n"
    "  · La única cifra que se traslada tal cual a producción es TA_respuesta = 0,\n"
    "    porque el verificador no compara contra el ground truth sino contra el FactSet\n"
    "    del propio cliente: es una garantía estructural, no un resultado estadístico.\n"
    "Cierre del círculo pendiente: re-ejecutar esta suite con el dataset de Movistar y\n"
    "con casos golden redactados por el equipo de facturación, no por el equipo autor."
)

#: Los siete campos del payload de hand-off (``Derivacion``). La completitud mide
#: cuántos llegan rellenos al asesor: un hand-off sin ``resumen_asesor`` obliga al
#: cliente a contar su problema otra vez, que es justo lo que se quería evitar.
CAMPOS_HANDOFF: tuple[str, ...] = (
    "requerida",
    "motivo",
    "motivo_codigo",
    "context_ref",
    "resumen_asesor",
    "senal_disparadora",
    "score_incomprension",
)

#: Concepto que vive FUERA del total del periodo (igual que en ``Recibo``): su ground
#: truth se contrasta contra ``deuda_anterior_cent``, no contra el delta de líneas.
CONCEPTO_DEUDA_ANTERIOR = "DEUDA_ANTERIOR"

#: Tope de turnos que simula :func:`turnos_hasta_derivar`. Más allá de cuatro turnos
#: repitiendo la misma pregunta, la conversación ya está perdida se derive o no.
MAX_TURNOS_SONDEO = 4

#: Espacio de nombres para los identificadores deterministas de la evaluación.
NAMESPACE_EVAL = uuid.UUID("7b7a5a2e-0d3f-5c9a-9a41-2f1d6b4c8e00")


# --------------------------------------------------------------------------- #
# Ground truth proyectado a campos comparables
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CampoRecuperacion:
    """Un campo del protocolo de *field-level exact match*, siempre en céntimos enteros.

    ``origen`` documenta de dónde sale el valor esperado, para que cualquiera pueda
    auditar la comparación sin leer el código.
    """

    nombre: str
    esperado_cent: int
    obtenido_cent: int
    origen: str

    @property
    def correcto(self) -> bool:
        """Coincidencia **exacta**: en céntimos enteros no existe "casi igual"."""
        return self.esperado_cent == self.obtenido_cent

    @property
    def desvio_cent(self) -> int:
        """Diferencia con signo entre lo obtenido y lo esperado."""
        return self.obtenido_cent - self.esperado_cent


def esperado_por_concepto(
    filas: Iterable[GroundTruthCausaDelta],
) -> tuple[dict[str, int], dict[str, set[TipoMovimiento | None]], int]:
    """Proyecta el ground truth a ``(deltas por concepto, causas por concepto, deuda)``.

    Un mismo concepto puede tener varias filas de ground truth con causas distintas
    (el IGV de un recibo con suspensión y reconexión, por ejemplo): los importes se
    **suman** y las causas se acumulan en un conjunto, porque el sistema atribuye una
    sola causa por línea y acertar cualquiera de las que la produjeron es acertar.

    ``DEUDA_ANTERIOR`` se separa: no es una línea del periodo, va fuera del total.
    """
    deltas: dict[str, int] = {}
    causas: dict[str, set[TipoMovimiento | None]] = {}
    deuda = 0
    for fila in filas:
        if fila.concepto_id == CONCEPTO_DEUDA_ANTERIOR:
            deuda += fila.delta_cent
            continue
        deltas[fila.concepto_id] = deltas.get(fila.concepto_id, 0) + fila.delta_cent
        causas.setdefault(fila.concepto_id, set()).add(fila.causa)
    return deltas, causas, deuda


def campos_de_recuperacion(
    caso: CasoGolden,
    factset: FactSet,
    filas_gt: Sequence[GroundTruthCausaDelta],
) -> list[CampoRecuperacion]:
    """Construye la lista de campos que la capa (A) compara al céntimo.

    Se comparan cuatro grupos:

    1. Los tres totales del caso golden (``total_actual``, ``total_previo``, ``delta``),
       escritos a mano en el YAML y revisados por una persona.
    2. ``deuda_anterior_cent``, solo si el ground truth la declara.
    3. El delta de **cada concepto** que el ground truth dice que varió.
    4. Los conceptos que el sistema declara variados y el ground truth **no** conoce:
       entran como campos con esperado 0. Sin este cuarto grupo, un sistema que
       reportara variaciones de más sacaría la misma nota que uno exacto.
    """
    esperados, _causas, deuda_gt = esperado_por_concepto(filas_gt)
    obtenidos = {linea.concepto_id: linea.delta_cent for linea in factset.lineas}

    campos = [
        CampoRecuperacion(
            "total_actual_cent",
            caso.total_esperado_cent,
            factset.total_actual_cent,
            "golden.total_esperado_cent",
        ),
        CampoRecuperacion(
            "total_previo_cent",
            caso.total_esperado_cent - caso.delta_esperado_cent,
            factset.total_previo_cent,
            "golden.total_esperado_cent - golden.delta_esperado_cent",
        ),
        CampoRecuperacion(
            "delta_total_cent",
            caso.delta_esperado_cent,
            factset.delta_total_cent,
            "golden.delta_esperado_cent",
        ),
    ]
    if deuda_gt:
        campos.append(
            CampoRecuperacion(
                "deuda_anterior_cent",
                deuda_gt,
                factset.deuda_anterior_cent,
                "gt_causa_delta[DEUDA_ANTERIOR]",
            )
        )
    for concepto in sorted(set(esperados) | set(obtenidos)):
        campos.append(
            CampoRecuperacion(
                f"delta_cent:{concepto}",
                esperados.get(concepto, 0),
                obtenidos.get(concepto, 0),
                "gt_causa_delta" if concepto in esperados else "concepto no previsto por el GT",
            )
        )
    return campos


# --------------------------------------------------------------------------- #
# Hand-off: payload y simulación de turnos
# --------------------------------------------------------------------------- #
def resumen_para_asesor(factset: FactSet, incomprension: ResultadoIncomprension) -> str:
    """Resumen que viaja con el hand-off para que el asesor no empiece de cero.

    Es texto **para el asesor**, no para el cliente: lleva las cifras en soles, el
    residual de conciliación y la señal que disparó la derivación. La ficha pide
    literalmente *"transferir el contexto de la interacción"*; esto es ese contexto.
    """
    conciliacion = resumen_de_conciliacion(factset)
    causas = ", ".join(
        f"{causa.etiqueta_cliente} ({formatear_soles(causa.monto_cent)})"
        for causa in factset.causas_agregadas
    )
    partes = [
        f"Cuenta {factset.cuenta_id}, periodo {factset.periodo_actual} "
        f"({factset.modalidad_renta}).",
        f"Recibo {formatear_soles(factset.total_actual_cent)} frente a "
        f"{formatear_soles(factset.total_previo_cent)} del periodo anterior: "
        f"variación de {formatear_soles(factset.delta_total_cent)}.",
        f"Causas detectadas: {causas}." if causas else "No se detectó ninguna variación.",
        f"Conciliación: residual {conciliacion['residual_cent']} céntimos, "
        f"invariante {'OK' if factset.invariante.ok else 'ROTO'}, "
        f"confianza {factset.confianza_global}.",
        f"Motivo de la derivación: {incomprension.senal_disparadora or 'no informado'} "
        f"(U={incomprension.U}).",
    ]
    if factset.deuda_anterior_cent:
        partes.append(
            f"Arrastra deuda anterior de {formatear_soles(factset.deuda_anterior_cent)}; "
            f"total a pagar {formatear_soles(factset.total_a_pagar_cent)}."
        )
    return " ".join(partes)


def construir_derivacion(
    factset: FactSet,
    incomprension: ResultadoIncomprension,
    respuesta: RespuestaCanalAgnostica,
) -> Derivacion:
    """Arma el payload de hand-off **solo con lo que produce el sistema**.

    Nada se rellena a mano para inflar la completitud: ``context_ref`` es el
    ``trace_id`` real del turno (con el que se recupera la conversación entera de la
    bitácora de auditoría) y ``resumen_asesor`` se deriva del FactSet ya conciliado.
    """
    if not incomprension.derivar:
        return respuesta.derivacion
    return incomprension.a_derivacion(
        context_ref=f"trace:{respuesta.trace_id}",
        resumen_asesor=resumen_para_asesor(factset, incomprension),
    )


def completitud_handoff(derivacion: Derivacion) -> tuple[int, int]:
    """Cuenta cuántos de los siete campos del hand-off llegan informados."""
    presentes = 0
    for campo in CAMPOS_HANDOFF:
        valor = getattr(derivacion, campo, None)
        if campo == "requerida":
            presentes += int(bool(valor))
        elif isinstance(valor, str):
            presentes += int(bool(valor.strip()))
        elif valor is not None:
            presentes += 1
    return presentes, len(CAMPOS_HANDOFF)


def turnos_hasta_derivar(
    factset: FactSet,
    utterance: str,
    *,
    reglas: ConfiguracionReglas | None = None,
    maximo: int = MAX_TURNOS_SONDEO,
) -> int | None:
    """Turno en el que el sistema decide derivar, repitiendo el cliente su pregunta.

    Simula al cliente que **no se da por satisfecho**: repite la misma consulta turno
    tras turno sin que la conversación avance. Es el peor caso realista y el que mide de
    verdad la latencia del hand-off, porque las reglas duras derivan en el turno 1 pero
    el umbral de incomprensión necesita acumular señal (``s3`` repregunta y ``s6`` sin
    progreso).

    Returns:
        El número de turno (1..``maximo``) en el que ``derivar`` se vuelve cierto, o
        ``None`` si el sistema aguanta los ``maximo`` turnos sin derivar.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    historial: list[Turno] = []
    for turno in range(1, maximo + 1):
        veredicto = evaluar_incomprension(
            factset, historial, utterance, reglas=configuracion, derivado_previamente=False
        )
        if veredicto.derivar:
            return turno
        historial.append(Turno(utterance=utterance, rol="cliente", progreso=False))
        historial.append(Turno(utterance="", rol="asistente"))
    return None


# --------------------------------------------------------------------------- #
# Observación de un caso
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ObservacionCaso:
    """Todo lo medido en una ejecución del sistema sobre un caso golden.

    Es el registro crudo: las métricas agregadas se calculan a partir de una lista de
    estas observaciones, de modo que se pueden reagrupar (por escenario, por modalidad,
    por canal) sin volver a ejecutar nada.
    """

    caso_id: str
    cuenta_id: str
    periodo: str
    escenarios: tuple[str, ...]
    modalidad_renta: ModalidadRenta
    verbosidad: Verbosidad
    canal: Canal

    # -- capa A y B de Precisión de Recuperación
    campos: tuple[CampoRecuperacion, ...]
    recall_at_1: bool | None

    # -- Tasa de Alucinación
    aserciones_totales: int
    aserciones_no_ancladas: int
    veredicto: VeredictoVerificacion
    fragmentos_prohibidos: tuple[str, ...]

    # -- Precisión del Hand-off
    debe_derivar: bool
    derivo: bool
    motivo_derivacion: str | None
    turnos_hasta_derivar: int | None
    handoff_campos_presentes: int
    handoff_campos_totales: int

    # -- métricas de apoyo
    residual_cent: int
    causas_acertadas: int
    causas_evaluadas: int
    modo_generacion: ModoGeneracion
    latencia_ms: int
    texto: str = ""
    error: str | None = None

    @property
    def campos_correctos(self) -> int:
        """Cuántos campos coinciden exactamente."""
        return sum(1 for campo in self.campos if campo.correcto)

    @property
    def ra_field(self) -> float:
        """Precisión field-level del caso (1.0 si no hubiera campos que comparar)."""
        return self.campos_correctos / len(self.campos) if self.campos else 1.0

    @property
    def exacta(self) -> bool:
        """``True`` si **todos** los campos son exactos: cuenta para la capa (C)."""
        return bool(self.campos) and self.campos_correctos == len(self.campos)

    @property
    def alucinada(self) -> bool:
        """``True`` si la respuesta contiene al menos una cifra sin anclar."""
        return self.aserciones_no_ancladas > 0 or self.veredicto is VeredictoVerificacion.FAIL

    @property
    def campos_fallidos(self) -> list[CampoRecuperacion]:
        """Campos que no coincidieron, para el detalle del informe."""
        return [campo for campo in self.campos if not campo.correcto]


def ejecutar_caso(
    caso: CasoGolden,
    *,
    modo: str | None = None,
    reglas: ConfiguracionReglas | None = None,
    usar_retriever: bool = True,
    ruta_dataset: str | None = None,
    cuenta: CuentaSintetica | None = None,
) -> ObservacionCaso:
    """Ejecuta el pipeline completo sobre un caso golden y mide todo lo medible.

    El recorrido es exactamente el de ``POST /v1/explicar``: motor determinístico →
    retriever → capa generativa → verificador → umbral de incomprensión. No hay atajos
    ni versiones "de laboratorio": si la API se rompiera, esta evaluación se rompería
    con ella.

    Args:
        caso: caso golden con sus expectativas.
        modo: ``"mock"`` o ``"gemini"``; por defecto, ``LLM_MODE``.
        reglas: configuración de negocio; por defecto ``cargar_reglas()``.
        usar_retriever: si es ``False`` se omite el RAG (la capa B queda sin medir).
            Sirve para aislar el motor cuando se depura.
        ruta_dataset: dataset alternativo.
        cuenta: cuenta ya cargada, para no releerla en suites grandes.

    Returns:
        La :class:`ObservacionCaso` con las mediciones de las tres métricas.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    arranque = time.perf_counter()
    datos_cuenta = cuenta if cuenta is not None else cargar_cuenta(caso.cuenta_id, ruta_dataset)
    factset = factset_de_cuenta(datos_cuenta, caso.periodo, configuracion)
    filas_gt = ground_truth_de(caso.cuenta_id, caso.periodo, ruta_dataset)

    contexto = None
    conceptos_fuera: list[str] = []
    if usar_retriever:
        contexto = _recuperar_seguro(factset, caso.utterance)
        if contexto is not None:
            conceptos_fuera = list(contexto.conceptos_fuera_catalogo)

    respuesta = explicar(
        factset,
        modo=modo,
        verbosidad=caso.verbosidad,
        utterance=caso.utterance,
        contexto_recuperado=list(contexto.fragmentos) if contexto is not None else None,
        canal=caso.canal,
        conversation_id=uuid.uuid5(NAMESPACE_EVAL, caso.caso_id),
        trace_id=f"eval-{caso.caso_id}",
    )
    incomprension = evaluar_incomprension(
        factset,
        None,
        caso.utterance,
        reglas=configuracion,
        conceptos_fuera_catalogo=conceptos_fuera,
    )
    derivacion = construir_derivacion(factset, incomprension, respuesta)
    derivo = bool(incomprension.derivar or respuesta.derivacion.requerida)
    presentes, totales = completitud_handoff(derivacion) if derivo else (0, len(CAMPOS_HANDOFF))

    texto = respuesta.texto
    minuscula = texto.lower()
    prohibidos = tuple(
        fragmento for fragmento in caso.no_debe_contener if fragmento.lower() in minuscula
    )

    acertadas, evaluadas = _precision_causa_raiz(factset, filas_gt)

    return ObservacionCaso(
        caso_id=caso.caso_id,
        cuenta_id=caso.cuenta_id,
        periodo=caso.periodo,
        escenarios=tuple(caso.escenarios),
        modalidad_renta=factset.modalidad_renta,
        verbosidad=caso.verbosidad,
        canal=caso.canal,
        campos=tuple(campos_de_recuperacion(caso, factset, filas_gt)),
        recall_at_1=_recall_at_1(contexto, filas_gt),
        aserciones_totales=respuesta.gobernanza.aserciones_totales,
        aserciones_no_ancladas=respuesta.gobernanza.aserciones_no_ancladas,
        veredicto=VeredictoVerificacion(respuesta.gobernanza.verificacion_numerica),
        fragmentos_prohibidos=prohibidos,
        debe_derivar=caso.debe_derivar,
        derivo=derivo,
        motivo_derivacion=str(derivacion.motivo_codigo) if derivacion.motivo_codigo else None,
        turnos_hasta_derivar=turnos_hasta_derivar(factset, caso.utterance, reglas=configuracion),
        handoff_campos_presentes=presentes,
        handoff_campos_totales=totales,
        residual_cent=factset.invariante.residual_cent,
        causas_acertadas=acertadas,
        causas_evaluadas=evaluadas,
        modo_generacion=respuesta.gobernanza.modo,
        latencia_ms=int((time.perf_counter() - arranque) * 1000),
        texto=texto,
    )


def _recuperar_seguro(factset: FactSet, utterance: str) -> Any | None:
    """Recupera contexto sin dejar que un fallo del RAG tumbe la evaluación.

    El retriever degrada solo (sin pgvector, sin API de embeddings, sin ``rank-bm25``),
    pero si el corpus no estuviera generado hay que poder medir igualmente las capas A
    y C, que no dependen de él.
    """
    try:
        from packages.retriever import recuperar

        return recuperar(factset, utterance, k=5)
    except Exception:
        return None


def _recall_at_1(contexto: Any | None, filas_gt: Sequence[GroundTruthCausaDelta]) -> bool | None:
    """Capa (B): ¿el primer documento de catálogo recuperado es de un concepto que varió?

    Devuelve ``None`` —y queda fuera del denominador— cuando la pregunta no se puede
    hacer: sin retriever, o en un recibo sin variación (los controles ESTABLE), donde
    no hay ningún documento "correcto" que recuperar.
    """
    if contexto is None:
        return None
    esperados = {
        fila.concepto_id for fila in filas_gt if fila.concepto_id != CONCEPTO_DEUDA_ANTERIOR
    }
    if not esperados:
        return None
    if not contexto.conceptos:
        return False
    primero = contexto.conceptos[0]
    concepto = primero.metadatos.get("concepto_id") or primero.doc_id.removeprefix("cat:")
    return str(concepto) in esperados


def _precision_causa_raiz(
    factset: FactSet, filas_gt: Sequence[GroundTruthCausaDelta]
) -> tuple[int, int]:
    """Aciertos de causa raíz sobre los conceptos que el ground truth atribuye.

    Se excluyen los **conceptos derivados** (``IGV``, ``REDONDEO``): el motor les asigna
    ``causa = None`` a propósito, porque su variación no tiene causa propia sino que es
    consecuencia aritmética del resto del recibo. Contarlos como fallos castigaría una
    decisión de diseño correcta; ocultar la exclusión sería maquillar el número, así que
    el informe la declara y cuenta cuántos conceptos se excluyeron.
    """
    _deltas, causas_gt, _deuda = esperado_por_concepto(filas_gt)
    obtenidas = {linea.concepto_id: linea.causa for linea in factset.lineas}
    acertadas = evaluadas = 0
    for concepto, esperadas in causas_gt.items():
        if concepto in CONCEPTOS_DERIVADOS:
            continue
        evaluadas += 1
        if obtenidas.get(concepto) in esperadas:
            acertadas += 1
    return acertadas, evaluadas


def ejecutar_suite(
    casos: Sequence[CasoGolden],
    *,
    modo: str | None = None,
    reglas: ConfiguracionReglas | None = None,
    usar_retriever: bool = True,
    ruta_dataset: str | None = None,
    al_terminar_caso: Any | None = None,
) -> list[ObservacionCaso]:
    """Ejecuta todos los casos, reutilizando la carga de cada cuenta.

    Args:
        al_terminar_caso: callback opcional ``(indice, total, observacion)`` para pintar
            progreso en la terminal sin acoplar este módulo a la salida.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    cache: dict[str, CuentaSintetica] = {}
    observaciones: list[ObservacionCaso] = []
    for indice, caso in enumerate(casos, start=1):
        if caso.cuenta_id not in cache:
            cache[caso.cuenta_id] = cargar_cuenta(caso.cuenta_id, ruta_dataset)
        observacion = ejecutar_caso(
            caso,
            modo=modo,
            reglas=configuracion,
            usar_retriever=usar_retriever,
            ruta_dataset=ruta_dataset,
            cuenta=cache[caso.cuenta_id],
        )
        observaciones.append(observacion)
        if al_terminar_caso is not None:
            al_terminar_caso(indice, len(casos), observacion)
    return observaciones


# --------------------------------------------------------------------------- #
# Métrica 1 — Precisión de Recuperación
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MetricasRecuperacion:
    """*Retrieval Accuracy* en tres capas. La cifra de titular es :attr:`strict`."""

    campos_totales: int
    campos_correctos: int
    ra_field_micro: float
    ra_field_macro: float
    ra_field_por_escenario: dict[str, float]
    casos_por_escenario: dict[str, int]
    recall_at_1: float | None
    recall_at_1_evaluados: int
    strict: float
    casos_exactos: int
    casos_totales: int
    campos_fallidos: tuple[tuple[str, str, int, int], ...] = ()

    @property
    def titular(self) -> float:
        """Strict answer accuracy: el número que se lleva al documento ejecutivo."""
        return self.strict


def metricas_recuperacion(observaciones: Sequence[ObservacionCaso]) -> MetricasRecuperacion:
    """Calcula las tres capas de la Precisión de Recuperación.

    * **(A) field-level exact match**, en céntimos enteros. ``micro`` pondera por campo
      (los casos con más conceptos pesan más); ``macro`` promedia primero dentro de cada
      escenario y luego entre escenarios, para que ``ESTABLE`` —con tres campos— pese lo
      mismo que un compuesto con nueve.
    * **(B) Recall@1 doc-level** sobre ``concepto_id``: si el primer documento de
      catálogo recuperado corresponde a un concepto que efectivamente varió.
    * **(C) strict answer accuracy**: porcentaje de respuestas con ``RA_field == 1.0``.
    """
    campos_totales = sum(len(observacion.campos) for observacion in observaciones)
    campos_correctos = sum(observacion.campos_correctos for observacion in observaciones)

    por_escenario: dict[str, list[float]] = {}
    for observacion in observaciones:
        for escenario in observacion.escenarios or ("SIN_ESCENARIO",):
            por_escenario.setdefault(escenario, []).append(observacion.ra_field)

    macro_escenario = {
        escenario: statistics.fmean(valores) for escenario, valores in sorted(por_escenario.items())
    }
    conteo_escenario = {
        escenario: len(valores) for escenario, valores in sorted(por_escenario.items())
    }

    evaluados = [
        observacion for observacion in observaciones if observacion.recall_at_1 is not None
    ]
    recall = (
        sum(1 for observacion in evaluados if observacion.recall_at_1) / len(evaluados)
        if evaluados
        else None
    )
    exactos = sum(1 for observacion in observaciones if observacion.exacta)

    fallidos = tuple(
        (observacion.caso_id, campo.nombre, campo.esperado_cent, campo.obtenido_cent)
        for observacion in observaciones
        for campo in observacion.campos_fallidos
    )

    return MetricasRecuperacion(
        campos_totales=campos_totales,
        campos_correctos=campos_correctos,
        ra_field_micro=_division(campos_correctos, campos_totales),
        ra_field_macro=statistics.fmean(macro_escenario.values()) if macro_escenario else 0.0,
        ra_field_por_escenario=macro_escenario,
        casos_por_escenario=conteo_escenario,
        recall_at_1=recall,
        recall_at_1_evaluados=len(evaluados),
        strict=_division(exactos, len(observaciones)),
        casos_exactos=exactos,
        casos_totales=len(observaciones),
        campos_fallidos=fallidos,
    )


# --------------------------------------------------------------------------- #
# Métrica 2 — Tasa de Alucinación
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MetricasAlucinacion:
    """Cero invenciones financieras. La comprometida es :attr:`ta_respuesta`."""

    aserciones_totales: int
    aserciones_no_ancladas: int
    ta_asercion: float
    respuestas_totales: int
    respuestas_con_alucinacion: int
    ta_respuesta: float
    veredictos: dict[str, int]
    respuestas_con_fragmento_prohibido: int
    fragmentos_detectados: tuple[tuple[str, str], ...] = ()

    @property
    def compromiso_cumplido(self) -> bool:
        """``TA_respuesta == 0`` **y** ningún fragmento prohibido en el texto."""
        return self.ta_respuesta == 0.0 and self.respuestas_con_fragmento_prohibido == 0


def metricas_alucinacion(observaciones: Sequence[ObservacionCaso]) -> MetricasAlucinacion:
    """Calcula ``TA_asercion`` y ``TA_respuesta``.

    ``TA_asercion`` es la proporción de cifras del texto que no se pudieron anclar en el
    FactSet ni derivar de él por álgebra permitida. ``TA_respuesta`` es la proporción de
    **respuestas** con al menos una de esas cifras, y es la que se compromete en cero:
    una respuesta con una sola cifra inventada es una respuesta inservible, por muchas
    cifras correctas que la acompañen.

    Los fragmentos prohibidos (``no_debe_contener`` de los casos adversariales) se
    cuentan aparte: no son alucinaciones numéricas sino fugas de contenido, y mezclarlos
    haría ilegible la métrica comprometida.
    """
    totales = sum(observacion.aserciones_totales for observacion in observaciones)
    no_ancladas = sum(observacion.aserciones_no_ancladas for observacion in observaciones)
    alucinadas = sum(1 for observacion in observaciones if observacion.alucinada)

    veredictos: dict[str, int] = {}
    for observacion in observaciones:
        clave = str(observacion.veredicto)
        veredictos[clave] = veredictos.get(clave, 0) + 1

    con_prohibidos = [
        observacion for observacion in observaciones if observacion.fragmentos_prohibidos
    ]
    detectados = tuple(
        (observacion.caso_id, fragmento)
        for observacion in con_prohibidos
        for fragmento in observacion.fragmentos_prohibidos
    )

    return MetricasAlucinacion(
        aserciones_totales=totales,
        aserciones_no_ancladas=no_ancladas,
        ta_asercion=_division(no_ancladas, totales),
        respuestas_totales=len(observaciones),
        respuestas_con_alucinacion=alucinadas,
        ta_respuesta=_division(alucinadas, len(observaciones)),
        veredictos=dict(sorted(veredictos.items())),
        respuestas_con_fragmento_prohibido=len(con_prohibidos),
        fragmentos_detectados=detectados,
    )


# --------------------------------------------------------------------------- #
# Métrica 3 — Precisión del Hand-off
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MetricasHandoff:
    """Exactitud de la decisión de derivar, con los errores tratados como asimétricos."""

    verdaderos_positivos: int
    falsos_positivos: int
    verdaderos_negativos: int
    falsos_negativos: int
    recall: float
    precision: float
    f2: float
    tasa_atrapamiento: float
    exactitud: float
    mediana_turnos_hasta_derivar: float | None
    turnos_observados: tuple[int, ...]
    completitud: float
    campos_presentes: int
    campos_esperados: int
    casos_no_derivados_que_debian: tuple[str, ...] = ()
    casos_derivados_de_mas: tuple[str, ...] = ()

    @property
    def primaria(self) -> float:
        """``Recall_handoff``: el falso negativo es el daño grave, así que manda él."""
        return self.recall


def metricas_handoff(observaciones: Sequence[ObservacionCaso]) -> MetricasHandoff:
    """Calcula la Precisión del Hand-off con la asimetría explícita.

    ``Recall_handoff = VP / (VP + FN)`` es la métrica primaria: mide cuántas de las
    conversaciones que debían escalar escalaron de verdad. Un falso negativo deja al
    cliente peleando con un asistente que no le sirve; un falso positivo solo gasta
    minutos de asesor. Por eso se reporta también ``F2`` (β = 2, el recall pesa el
    doble) y la ``tasa_atrapamiento = FP / (FP + VN)``, es decir, qué proporción de las
    conversaciones **sanas** acaba innecesariamente en manos de un humano: es el coste
    operativo de ser prudente, y hay que verlo al lado del recall para poder decidir el
    umbral con criterio.

    ``Handoff_completeness`` promedia, sobre las derivaciones efectivas, cuántos de los
    siete campos del payload llegan informados al asesor.
    """
    vp = sum(1 for obs in observaciones if obs.debe_derivar and obs.derivo)
    fn = sum(1 for obs in observaciones if obs.debe_derivar and not obs.derivo)
    fp = sum(1 for obs in observaciones if not obs.debe_derivar and obs.derivo)
    vn = sum(1 for obs in observaciones if not obs.debe_derivar and not obs.derivo)

    recall = _division(vp, vp + fn)
    precision = _division(vp, vp + fp)
    denominador_f2 = 4 * precision + recall
    f2 = (5 * precision * recall / denominador_f2) if denominador_f2 else 0.0

    turnos = tuple(
        sorted(
            obs.turnos_hasta_derivar
            for obs in observaciones
            if obs.debe_derivar and obs.turnos_hasta_derivar is not None
        )
    )
    derivadas = [obs for obs in observaciones if obs.derivo]
    presentes = sum(obs.handoff_campos_presentes for obs in derivadas)
    esperados = sum(obs.handoff_campos_totales for obs in derivadas)

    return MetricasHandoff(
        verdaderos_positivos=vp,
        falsos_positivos=fp,
        verdaderos_negativos=vn,
        falsos_negativos=fn,
        recall=recall,
        precision=precision,
        f2=f2,
        tasa_atrapamiento=_division(fp, fp + vn),
        exactitud=_division(vp + vn, len(observaciones)),
        mediana_turnos_hasta_derivar=statistics.median(turnos) if turnos else None,
        turnos_observados=turnos,
        completitud=_division(presentes, esperados),
        campos_presentes=presentes,
        campos_esperados=esperados,
        casos_no_derivados_que_debian=tuple(
            obs.caso_id for obs in observaciones if obs.debe_derivar and not obs.derivo
        ),
        casos_derivados_de_mas=tuple(
            obs.caso_id for obs in observaciones if not obs.debe_derivar and obs.derivo
        ),
    )


# --------------------------------------------------------------------------- #
# Métricas de apoyo
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MetricasApoyo:
    """Salud del motor: conciliación, atribución y cuánto se apoya en la plantilla."""

    residual_medio_cent: float
    residual_maximo_cent: int
    casos_con_invariante_roto: int
    precision_causa_raiz: float
    causas_acertadas: int
    causas_evaluadas: int
    tasa_fallback: float
    modos: dict[str, int]
    latencia_mediana_ms: float
    latencia_p95_ms: float


def metricas_apoyo(observaciones: Sequence[ObservacionCaso]) -> MetricasApoyo:
    """Calcula ``residual_medio_cent``, ``precision_causa_raiz`` y ``tasa_fallback``.

    * ``residual_medio_cent`` es la media del **valor absoluto** del residual de
      conciliación. Con la política de ±1 céntimo, cualquier valor distinto de 0,0
      merece una mirada: el motor debería cerrar exacto en datos sintéticos.
    * ``precision_causa_raiz`` compara la causa que el motor atribuye a cada concepto
      contra la que el generador escribió al inyectar el escenario, excluyendo los
      conceptos derivados (ver :func:`_precision_causa_raiz`).
    * ``tasa_fallback`` es la proporción de respuestas que acabaron en la plantilla
      determinística. En modo ``mock`` debe ser 0: si sube, el verificador está
      rechazando al proveedor y hay que mirar el log ``LLM_CALL``.
    """
    residuales = [abs(obs.residual_cent) for obs in observaciones]
    acertadas = sum(obs.causas_acertadas for obs in observaciones)
    evaluadas = sum(obs.causas_evaluadas for obs in observaciones)
    fallback = sum(1 for obs in observaciones if obs.modo_generacion is ModoGeneracion.PLANTILLA)
    modos: dict[str, int] = {}
    for obs in observaciones:
        clave = str(obs.modo_generacion)
        modos[clave] = modos.get(clave, 0) + 1
    latencias = sorted(obs.latencia_ms for obs in observaciones)

    return MetricasApoyo(
        residual_medio_cent=statistics.fmean(residuales) if residuales else 0.0,
        residual_maximo_cent=max(residuales) if residuales else 0,
        casos_con_invariante_roto=sum(1 for valor in residuales if valor > 1),
        precision_causa_raiz=_division(acertadas, evaluadas),
        causas_acertadas=acertadas,
        causas_evaluadas=evaluadas,
        tasa_fallback=_division(fallback, len(observaciones)),
        modos=dict(sorted(modos.items())),
        latencia_mediana_ms=statistics.median(latencias) if latencias else 0.0,
        latencia_p95_ms=_percentil(latencias, 95),
    )


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class InformeEvaluacion:
    """Resultado completo de una ejecución de ``make eval``."""

    recuperacion: MetricasRecuperacion
    alucinacion: MetricasAlucinacion
    handoff: MetricasHandoff
    apoyo: MetricasApoyo
    observaciones: tuple[ObservacionCaso, ...]
    modo: str
    rules_version: str
    duracion_ms: int
    advertencia: str = ADVERTENCIA_CIRCULARIDAD
    parametros: Mapping[str, Any] = field(default_factory=dict)

    #: Umbral de ``precision_causa_raiz`` por debajo del cual la evaluación NO aprueba.
    #: No es 1.0 a propósito: una causa mal atribuida en un caso ambiguo es un fallo de
    #: calidad, no una mentira financiera. Pero por debajo de este umbral el sistema
    #: está contando una historia equivocada a demasiados clientes.
    UMBRAL_CAUSA_RAIZ: ClassVar[float] = 0.98

    @property
    def aprobado(self) -> bool:
        """Criterio de aceptación del MVP.

        Se exige lo que el proyecto **promete**, no lo que sería bonito tener:
        cero alucinaciones, cero fugas de contenido, conciliación exacta, ningún falso
        negativo de hand-off y una atribución causal casi perfecta. La precisión
        field-level entra como umbral alto pero no absoluto: un campo discrepante es una
        discrepancia de datos, no una mentira.

        ``precision_causa_raiz`` se añadió el 9 de agosto de 2026 tras un incidente que
        conviene no olvidar: ``RENTA_MOVISTAR_TOTAL`` era el único concepto de renta sin
        ``SUSPENSION`` entre sus causas permitidas, y a nueve cuentas de trescientas el
        recibo les decía *«Cambio de plan»* cuando lo que había habido era un corte. La
        aritmética era exacta, la alucinación cero y el invariante cerraba, así que
        ``run_eval`` imprimía **EVALUACIÓN APROBADA** mientras ``pytest`` rompía la
        construcción. Un criterio de aceptación que aprueba un sistema que miente sobre
        la causa no es un criterio de aceptación: cubría la exactitud de las cifras y
        dejaba fuera la veracidad del relato, que es justo lo que el desafío pide.
        """
        return (
            self.alucinacion.compromiso_cumplido
            and self.handoff.falsos_negativos == 0
            and self.apoyo.casos_con_invariante_roto == 0
            and self.recuperacion.strict == 1.0
            and self.apoyo.precision_causa_raiz >= self.UMBRAL_CAUSA_RAIZ
        )

    def a_dict(self) -> dict[str, Any]:
        """Proyección serializable (``run_eval.py --json``)."""
        return {
            "modo": self.modo,
            "rules_version": self.rules_version,
            "duracion_ms": self.duracion_ms,
            "casos": len(self.observaciones),
            "aprobado": self.aprobado,
            "advertencia_circularidad": self.advertencia,
            "parametros": dict(self.parametros),
            "precision_recuperacion": {
                "ra_field_micro": round(self.recuperacion.ra_field_micro, 6),
                "ra_field_macro_por_escenario": {
                    escenario: round(valor, 6)
                    for escenario, valor in self.recuperacion.ra_field_por_escenario.items()
                },
                "ra_field_macro": round(self.recuperacion.ra_field_macro, 6),
                "recall_at_1": (
                    round(self.recuperacion.recall_at_1, 6)
                    if self.recuperacion.recall_at_1 is not None
                    else None
                ),
                "recall_at_1_evaluados": self.recuperacion.recall_at_1_evaluados,
                "strict_answer_accuracy": round(self.recuperacion.strict, 6),
                "campos_correctos": self.recuperacion.campos_correctos,
                "campos_totales": self.recuperacion.campos_totales,
                "campos_fallidos": [
                    {
                        "caso_id": caso,
                        "campo": campo,
                        "esperado_cent": esperado,
                        "obtenido_cent": obtenido,
                    }
                    for caso, campo, esperado, obtenido in self.recuperacion.campos_fallidos
                ],
            },
            "tasa_alucinacion": {
                "ta_asercion": round(self.alucinacion.ta_asercion, 6),
                "ta_respuesta": round(self.alucinacion.ta_respuesta, 6),
                "aserciones_totales": self.alucinacion.aserciones_totales,
                "aserciones_no_ancladas": self.alucinacion.aserciones_no_ancladas,
                "respuestas_con_alucinacion": self.alucinacion.respuestas_con_alucinacion,
                "veredictos": self.alucinacion.veredictos,
                "fragmentos_prohibidos": [
                    {"caso_id": caso, "fragmento": fragmento}
                    for caso, fragmento in self.alucinacion.fragmentos_detectados
                ],
                "compromiso_cumplido": self.alucinacion.compromiso_cumplido,
            },
            "precision_handoff": {
                "recall": round(self.handoff.recall, 6),
                "precision": round(self.handoff.precision, 6),
                "f2": round(self.handoff.f2, 6),
                "tasa_atrapamiento": round(self.handoff.tasa_atrapamiento, 6),
                "exactitud": round(self.handoff.exactitud, 6),
                "matriz": {
                    "vp": self.handoff.verdaderos_positivos,
                    "fp": self.handoff.falsos_positivos,
                    "vn": self.handoff.verdaderos_negativos,
                    "fn": self.handoff.falsos_negativos,
                },
                "mediana_turnos_hasta_derivar": self.handoff.mediana_turnos_hasta_derivar,
                "handoff_completeness": round(self.handoff.completitud, 6),
                "campos_del_payload": list(CAMPOS_HANDOFF),
                "falsos_negativos": list(self.handoff.casos_no_derivados_que_debian),
                "falsos_positivos": list(self.handoff.casos_derivados_de_mas),
            },
            "apoyo": {
                "residual_medio_cent": round(self.apoyo.residual_medio_cent, 4),
                "residual_maximo_cent": self.apoyo.residual_maximo_cent,
                "casos_con_invariante_roto": self.apoyo.casos_con_invariante_roto,
                "precision_causa_raiz": round(self.apoyo.precision_causa_raiz, 6),
                "causas_acertadas": self.apoyo.causas_acertadas,
                "causas_evaluadas": self.apoyo.causas_evaluadas,
                "tasa_fallback": round(self.apoyo.tasa_fallback, 6),
                "modos": self.apoyo.modos,
                "latencia_mediana_ms": self.apoyo.latencia_mediana_ms,
                "latencia_p95_ms": self.apoyo.latencia_p95_ms,
            },
            "detalle_por_caso": [
                {
                    "caso_id": obs.caso_id,
                    "cuenta_id": obs.cuenta_id,
                    "escenarios": list(obs.escenarios),
                    "modalidad_renta": str(obs.modalidad_renta),
                    "ra_field": round(obs.ra_field, 6),
                    "exacta": obs.exacta,
                    "recall_at_1": obs.recall_at_1,
                    "aserciones_totales": obs.aserciones_totales,
                    "aserciones_no_ancladas": obs.aserciones_no_ancladas,
                    "veredicto": str(obs.veredicto),
                    "debe_derivar": obs.debe_derivar,
                    "derivo": obs.derivo,
                    "motivo_derivacion": obs.motivo_derivacion,
                    "turnos_hasta_derivar": obs.turnos_hasta_derivar,
                    "residual_cent": obs.residual_cent,
                    "modo": str(obs.modo_generacion),
                    "latencia_ms": obs.latencia_ms,
                }
                for obs in self.observaciones
            ],
        }


def agregar(
    observaciones: Sequence[ObservacionCaso],
    *,
    modo: str = "mock",
    rules_version: str = "",
    duracion_ms: int = 0,
    parametros: Mapping[str, Any] | None = None,
) -> InformeEvaluacion:
    """Reduce las observaciones a las cuatro familias de métricas."""
    return InformeEvaluacion(
        recuperacion=metricas_recuperacion(observaciones),
        alucinacion=metricas_alucinacion(observaciones),
        handoff=metricas_handoff(observaciones),
        apoyo=metricas_apoyo(observaciones),
        observaciones=tuple(observaciones),
        modo=modo,
        rules_version=rules_version,
        duracion_ms=duracion_ms,
        parametros=dict(parametros or {}),
    )


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _division(numerador: int | float, denominador: int | float) -> float:
    """División que devuelve 0.0 en vez de reventar con denominador cero."""
    return float(numerador) / float(denominador) if denominador else 0.0


def _percentil(valores: Sequence[int], percentil: int) -> float:
    """Percentil por interpolación lineal sobre una lista ya ordenada."""
    if not valores:
        return 0.0
    if len(valores) == 1:
        return float(valores[0])
    posicion = (len(valores) - 1) * percentil / 100
    inferior = int(posicion)
    superior = min(inferior + 1, len(valores) - 1)
    peso = posicion - inferior
    return valores[inferior] * (1 - peso) + valores[superior] * peso
