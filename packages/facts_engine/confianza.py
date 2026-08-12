"""Umbral de incomprensión y decisión de derivar a un asesor (sección 4.8).

La ficha del desafío define la *Precisión del Hand-off* como *"exactitud lógica al
decidir cuándo derivar a un humano basándose en umbrales de incomprensión"*. Este módulo
es ese umbral, y es **código determinístico**: la decisión de pasar a un humano no se
delega en el modelo generativo.

Dos mecanismos, en este orden:

1. **Reglas duras** — derivan sin calcular nada: petición explícita de un humano,
   invariante roto, concepto fuera de catálogo e intención regulatoria (reclamo formal,
   baja, portabilidad). El falso negativo aquí es el daño grave, así que ante la duda
   se deriva.
2. **Score continuo** ``U``::

       s1 = cobertura del Δ DESGLOSADO = Σ|Δ_desglosados| / |Δ_total|
       s2 = unicidad de causa          = 1 − H(p_causas)/log(k)
       s3 = repregunta                 (similitud con los turnos PREVIOS > 0.80)
       s6 = turnos sin progreso        (normalizado por max_turnos_sin_progreso)

       U = 1 − (w1·s1 + w2·s2 + w3·(1−s3) + w6·(1−s6)),   Σw = 1
       DERIVAR si U > τ_alto (0.65)

   Con **histéresis**, y solo mientras un asesor esté realmente en la sala.

Desglosar no es confirmar
-------------------------
El score mide **si el sistema puede explicar**, y explicar un recibo es responder *cuánto
varió y en qué líneas*. Eso es lo que mide ``s1``: la parte del delta total que está
desglosada línea a línea, con su nombre comercial, su importe anterior, su importe actual
y evidencia citable. Es una magnitud **aritmética**, y en el dataset real vale 1.0.

Aparte, y **fuera de** ``U``, se calcula la :func:`cobertura causal
<_cobertura_causal>`: qué parte del delta tiene además una causa confirmada (una orden
del CRM o una causa oficial del catálogo). En el dataset del desafío no hay órdenes de
CRM, así que vale 0.0 casi siempre. Meter esa laguna dentro de ``U`` era el defecto que
convertía el hand-off en el caso normal: el sistema sabía perfectamente *cuánto* y *de
qué línea*, y aun así se declaraba incapaz de explicar. Son dos preguntas distintas:

* *"no sé cuánto varió ni en qué línea"* → el recibo no se puede explicar → **derivar**.
  Esa vía existe y es la regla dura ``INVARIANTE_ROTO``, no ``s1``.
* *"sé exactamente cuánto y en qué líneas, pero no puedo confirmar el porqué"* → el
  recibo **sí** se explica, con la laguna dicha en voz alta → **no derivar**. La
  cobertura causal gobierna la narrativa, la telemetría y la *oferta* de asesor, que es
  una acción que el cliente elige, no una puerta que se le cierra.

Los pesos y umbrales viven en ``rules.yaml``. Aquí no hay aritmética monetaria: los
importes solo se leen del FactSet, ya en céntimos enteros.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import MotivoDerivacion
from packages.core_domain.esquemas.factset import FactSet, LineaDelta
from packages.core_domain.esquemas.respuesta import Derivacion
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas
from packages.facts_engine.atribucion import esta_atribuida

# La regla dura «el cliente pide una persona» vive en su propio módulo porque la
# consultan tres sitios —este umbral, el clasificador de intención y el generador de la
# suite golden— y cuando cada uno tenía su copia, los tres discrepaban. Se reexporta
# desde aquí para no romper a quien ya la importaba de `confianza`.
from packages.facts_engine.peticion_humano import (
    PATRONES_PETICION_HUMANO,
    normalizar_texto,
    pide_humano,
)

__all__ = [
    "PATRONES_PETICION_HUMANO",
    "ResultadoIncomprension",
    "Turno",
    "entropia_normalizada",
    "evaluar_incomprension",
    "normalizar_texto",
    "pide_humano",
    "similitud_textos",
]

_TOKENIZADOR = re.compile(r"[a-z0-9]+")

#: Palabras vacías del español que no aportan a la similitud entre dos preguntas.
_VACIAS: frozenset[str] = frozenset(
    {
        "a", "al", "algo", "como", "con", "cual", "de", "del", "el", "en", "es", "esa",
        "ese", "esta", "este", "esto", "hay", "la", "las", "le", "lo", "los", "me",
        "mes", "mi", "no", "o", "para", "pero", "por", "porque", "que", "se", "si",
        "su", "sus", "un", "una", "y", "ya", "yo",
    }
)


def _tokens(texto: str) -> set[str]:
    """Tokens significativos de una frase, sin palabras vacías."""
    palabras = set(_TOKENIZADOR.findall(normalizar_texto(texto)))
    utiles = palabras - _VACIAS
    return utiles or palabras


def similitud_textos(izquierda: str, derecha: str) -> float:
    """Similitud de Jaccard entre los tokens significativos de dos frases (0..1).

    Se elige Jaccard y no una distancia de edición porque lo que interesa detectar es
    la **repregunta**: el cliente vuelve a preguntar lo mismo con otras palabras de
    enlace. Es determinista, explicable y no depende de ningún modelo.
    """
    if not izquierda.strip() or not derecha.strip():
        return 0.0
    uno, dos = _tokens(izquierda), _tokens(derecha)
    if not uno or not dos:
        return 0.0
    interseccion = len(uno & dos)
    if interseccion == 0:
        return 0.0
    return interseccion / len(uno | dos)


def entropia_normalizada(pesos: Sequence[int]) -> float:
    """Entropía de Shannon normalizada (0..1) de una distribución de impactos.

    ``H(p)/log(k)`` con ``p_i = |peso_i| / Σ|peso|``. Vale 0 cuando una sola causa
    concentra toda la variación (situación ideal: hay UNA explicación) y 1 cuando el
    delta se reparte por igual entre ``k`` causas (situación confusa de contar).
    """
    magnitudes = [abs(int(peso)) for peso in pesos if peso]
    total = sum(magnitudes)
    if len(magnitudes) <= 1 or total == 0:
        return 0.0
    entropia = 0.0
    for magnitud in magnitudes:
        proporcion = magnitud / total
        entropia -= proporcion * math.log(proporcion)
    return min(entropia / math.log(len(magnitudes)), 1.0)


class Turno(BaseModel):
    """Un turno de la conversación, lo mínimo que el umbral necesita saber."""

    model_config = ConfigDict(extra="forbid")

    utterance: str = ""
    # «asesor» entra cuando una persona real se suma a la conversación tras una
    # derivación. Sus turnos NO pasan por el verificador numérico: el asesor
    # responde de sus propias palabras, la máquina no puede responder por él.
    rol: Literal["cliente", "asistente", "asesor"] = "cliente"
    ts: datetime | None = None
    progreso: bool = Field(
        default=False, description="El turno resolvió algo (el cliente avanzó)"
    )
    derivado: bool = Field(default=False, description="En ese turno ya se derivó")


class ResultadoIncomprension(BaseModel):
    """Veredicto del umbral de incomprensión, con todo lo necesario para auditarlo."""

    model_config = ConfigDict(extra="forbid")

    derivar: bool
    motivo: MotivoDerivacion | None = None
    U: float = Field(ge=0.0, le=1.0, description="Score de incomprensión")
    senal_disparadora: str | None = Field(
        default=None, description="Qué disparó la decisión, en lenguaje humano"
    )
    reglas_disparadas: list[str] = Field(default_factory=list)
    s1_cobertura: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Parte del delta DESGLOSADA por línea. Entra en U con peso w1.",
    )
    s2_unicidad: float = Field(default=1.0, ge=0.0, le=1.0)
    s3_repregunta: float = Field(default=0.0, ge=0.0, le=1.0)
    s6_sin_progreso: float = Field(default=0.0, ge=0.0, le=1.0)
    cobertura_causal: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Parte del delta con causa CONFIRMADA (orden del CRM o causa oficial). "
            "NO entra en U: gobierna la narrativa, la oferta de asesor y la gobernanza."
        ),
    )
    tau_alto: float = 0.65
    tau_bajo: float = 0.35
    turnos_sin_progreso: int = 0
    histeresis_aplicada: bool = False

    @property
    def score(self) -> float:
        """Alias legible de ``U``."""
        return self.U

    @property
    def causa_confirmada(self) -> bool:
        """Si toda la variación tiene una causa documentada detrás.

        Cuando es ``False`` el recibo se explica igual —el desglose está completo— pero
        la explicación tiene que decir qué parte no se puede confirmar y por qué. Es la
        diferencia entre *"no puedo explicarlo"* y *"puedo explicarlo, pero no puedo
        confirmar el motivo"*, y el cliente merece que se le diga cuál de las dos es.
        """
        return self.cobertura_causal >= 1.0

    @property
    def ofrecer_asesor(self) -> bool:
        """Si conviene **ofrecer** un asesor sin derivar todavía.

        Es el término medio que faltaba: la explicación sale, con su desglose completo y
        su laguna declarada, y además se le ofrece al cliente la puerta a una persona por
        si quiere el porqué documentado. Derivar es entonces una decisión suya, no una
        rendición del sistema, y el hand-off vuelve a ser el último recurso.
        """
        return not self.derivar and not self.causa_confirmada

    def a_derivacion(
        self, *, context_ref: str | None = None, resumen_asesor: str | None = None
    ) -> Derivacion:
        """Convierte el veredicto en el bloque ``Derivacion`` de la respuesta."""
        return Derivacion(
            requerida=self.derivar,
            motivo=self.senal_disparadora,
            motivo_codigo=self.motivo,
            context_ref=context_ref,
            resumen_asesor=resumen_asesor,
            senal_disparadora=self.senal_disparadora,
            score_incomprension=self.U,
        )


def _normalizar_historial(historial: Sequence[Turno | str] | None) -> list[Turno]:
    """Admite ``Turno`` o simples cadenas (que se toman como turnos del cliente)."""
    if not historial:
        return []
    return [
        turno if isinstance(turno, Turno) else Turno(utterance=turno) for turno in historial
    ]


def _regla_activa(reglas: ConfiguracionReglas, motivo: MotivoDerivacion) -> bool:
    """Una regla dura solo actúa si ``rules.yaml`` la declara activa."""
    activas = reglas.umbrales_incomprension.reglas_duras
    return motivo in activas if activas else True


def _intencion_regulatoria(utterance_normalizado: str, reglas: ConfiguracionReglas) -> str | None:
    """Detecta reclamo formal, baja, portabilidad y demás intenciones regulatorias."""
    for intencion in reglas.umbrales_incomprension.intenciones_regulatorias:
        if normalizar_texto(intencion) in utterance_normalizado:
            return intencion
    return None


def _esta_desglosada(linea: LineaDelta) -> bool:
    """Si de esta línea se puede decir **cuánto varió y de qué es**.

    Hacen falta tres cosas y ninguna de ellas es el CRM:

    1. un nombre en lenguaje de cliente (*"Movistar TV Estándar"*), que en el dataset
       real sale de ``v_concepto_real`` y es el nombre comercial del propio operador;
    2. los dos importes, el de antes y el de ahora —``delta_cent`` los valida el propio
       ``LineaDelta``, así que basta con que la línea exista—;
    3. evidencia citable, porque una cifra que no se puede citar no se puede entregar:
       el verificador la rechazaría de todas formas.
    """
    return bool(linea.nombre_comercial.strip()) and bool(linea.evidencia)


def _desglose(factset: FactSet) -> float:
    """``s1``: parte del delta total que el sistema sabe **desglosar** por línea.

    Es la magnitud que entra en ``U`` porque es la que responde a *"¿puedo explicar este
    recibo?"*. Un 1.0 significa que cada céntimo de la variación tiene una línea con
    nombre, importe anterior, importe actual y referencia citable. Lo que **no** afirma
    es por qué se movió esa línea: eso es :func:`_cobertura_causal`, y va aparte.
    """
    if factset.delta_total_cent == 0:
        return 1.0
    desglosado = sum(
        abs(linea.delta_cent)
        for linea in factset.lineas
        if linea.se_explica and _esta_desglosada(linea)
    )
    return min(desglosado / abs(factset.delta_total_cent), 1.0)


def _cobertura_causal(factset: FactSet, confianza_minima: float) -> float:
    """Parte del delta con una causa **confirmada** (orden del CRM o causa oficial).

    **No entra en el score**, y es deliberado. Sin órdenes del CRM esta magnitud vale 0
    para todas las cuentas del dataset del desafío, y meterla en ``U`` imponía un suelo
    de ``w1 = 0.40`` a conversaciones perfectamente sanas. Lo que gobierna es otra cosa:

    * la **narrativa** — con cobertura causal baja hay que decir con todas las letras qué
      no se puede confirmar y por qué, en vez de insinuar una causa;
    * la **oferta** de asesor como acción sugerida, que el cliente acepta o no;
    * la **telemetría y la gobernanza**, donde queda registrada la laguna del dato.

    Que el sistema no pueda confirmar el porqué es una limitación del dato disponible, no
    una incomprensión de la conversación. Confundirlas falseaba la métrica de hand-off.
    """
    if factset.delta_total_cent == 0:
        return 1.0
    confirmado = sum(
        abs(linea.delta_cent)
        for linea in factset.lineas
        if linea.se_explica and esta_atribuida(linea, confianza_minima)
    )
    return min(confirmado / abs(factset.delta_total_cent), 1.0)


def _unicidad(factset: FactSet) -> float:
    """``s2``: 1 cuando hay una sola causa dominante, 0 cuando el delta se dispersa."""
    montos = [causa.monto_cent for causa in factset.causas_agregadas if causa.monto_cent]
    if len(montos) <= 1:
        return 1.0
    return 1.0 - entropia_normalizada(montos)


def _repregunta(
    utterance: str, historial: Sequence[Turno], umbral: float
) -> tuple[float, str | None]:
    """``s3``: similitud con los turnos previos del cliente, con puerta en el umbral.

    Por debajo del umbral no penaliza (dos preguntas sobre el recibo se parecen siempre
    un poco); a partir de él, el valor entra tal cual en el score.

    ``historial`` tiene que traer **solo turnos anteriores**: el mensaje de este turno
    llega por ``utterance`` y aparte. Cuando el llamador incluía también el turno actual,
    la frase se comparaba consigo misma, ``Jaccard(x, x) = 1.0`` y toda primera pregunta
    de todo cliente nuevo entraba en el score como si fuera una repregunta.
    """
    previos = [turno for turno in historial if turno.rol == "cliente" and turno.utterance]
    mejor, frase = 0.0, None
    for turno in previos[-3:]:
        similitud = similitud_textos(utterance, turno.utterance)
        if similitud > mejor:
            mejor, frase = similitud, turno.utterance
    if mejor < umbral:
        return 0.0, None
    return mejor, frase


def _sin_progreso(historial: Sequence[Turno], maximo: int) -> tuple[float, int]:
    """``s6``: turnos del cliente desde la última vez que la conversación avanzó.

    Se cuentan turnos **de cliente**, pero el corte lo marca ``progreso`` en **cualquier
    rol**, y ahí estaba el fallo: quien sabe si el turno resolvió algo es el asistente
    —es él quien lo escribe al responder—, y el bucle saltaba los turnos que no eran del
    cliente *antes* de mirar la bandera. Los turnos de cliente nacen siempre con
    ``progreso=False``, así que la bandera no se leía nunca y ``s6`` acababa siendo un
    simple contador de mensajes: subía por hablar, por bien que se hubiera explicado.
    """
    cuenta = 0
    for turno in reversed(historial):
        if turno.progreso:
            break
        if turno.rol == "cliente":
            cuenta += 1
    return (min(cuenta / maximo, 1.0) if maximo > 0 else 0.0), cuenta


def evaluar_incomprension(
    factset: FactSet,
    historial_turnos: Sequence[Turno | str] | None = None,
    utterance: str = "",
    *,
    reglas: ConfiguracionReglas | None = None,
    derivado_previamente: bool = False,
    asesor_en_sala: bool = False,
    conceptos_fuera_catalogo: Sequence[str] | None = None,
) -> ResultadoIncomprension:
    """Decide si la conversación debe pasar a un asesor humano (sección 4.8).

    Primero se evalúan las **reglas duras**, que derivan sin score; si ninguna se
    dispara, se calcula::

        U = 1 − (w1·s1 + w2·s2 + w3·(1−s3) + w6·(1−s6))
        DERIVAR si U > τ_alto

    Un ``U`` alto significa "el sistema no está entendiendo o no está explicando": el
    delta sin desglosar, causas dispersas, el cliente repreguntando lo mismo y varios
    turnos sin avanzar. Fíjese en que **ninguna** de las cuatro señales es "el CRM no me
    dice por qué": esa laguna se mide aparte, en ``cobertura_causal``, y no deriva.

    Args:
        factset: hechos del recibo ya conciliados (aporta ``s1``, ``s2`` y la cobertura
            causal).
        historial_turnos: turnos **previos** de la conversación (``Turno`` o texto
            suelto). El mensaje de este turno **no** va aquí: va en ``utterance``.
        utterance: mensaje actual del cliente. Entra como **dato**, nunca como
            instrucción, y solo se usa para detección de patrones.
        reglas: configuración; por defecto ``cargar_reglas()``.
        derivado_previamente: si la conversación ya fue derivada alguna vez.
        asesor_en_sala: si una persona real está atendiendo ahora mismo. Es lo que activa
            la histéresis, no el hecho de haber derivado: mientras nadie haya recogido el
            expediente no hay conversación humana que proteger, y fijar la derivación por
            un pico transitorio del score condenaba el resto del diálogo.
        conceptos_fuera_catalogo: conceptos del recibo que el catálogo no conoce
            (los calcula ``diff.comparar_detallado``).

    Returns:
        El veredicto completo, con los cuatro componentes del score y la cobertura
        causal, para que la auditoría pueda reconstruir la decisión.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    umbrales = configuracion.umbrales_incomprension
    pesos = umbrales.pesos
    historial = _normalizar_historial(historial_turnos)
    normalizado = normalizar_texto(utterance)

    reglas_disparadas: list[str] = []
    motivo: MotivoDerivacion | None = None
    senal: str | None = None

    patron = pide_humano(utterance) if normalizado else None
    if patron and _regla_activa(configuracion, MotivoDerivacion.PETICION_HUMANO):
        reglas_disparadas.append(MotivoDerivacion.PETICION_HUMANO.value)
        motivo = motivo or MotivoDerivacion.PETICION_HUMANO
        senal = senal or f'el cliente pidió atención humana ("{patron}")'

    if not factset.invariante.ok and _regla_activa(
        configuracion, MotivoDerivacion.INVARIANTE_ROTO
    ):
        reglas_disparadas.append(MotivoDerivacion.INVARIANTE_ROTO.value)
        motivo = motivo or MotivoDerivacion.INVARIANTE_ROTO
        senal = senal or (
            f"el recibo no concilia: quedan {factset.invariante.residual_cent} céntimos "
            "sin explicar"
        )

    fuera = list(conceptos_fuera_catalogo or [])
    if not fuera:
        fuera = sorted(
            {
                linea.concepto_id
                for linea in factset.lineas
                if not configuracion.existe_concepto(linea.concepto_id)
            }
        )
    if fuera and _regla_activa(configuracion, MotivoDerivacion.CONCEPTO_FUERA_CATALOGO):
        reglas_disparadas.append(MotivoDerivacion.CONCEPTO_FUERA_CATALOGO.value)
        motivo = motivo or MotivoDerivacion.CONCEPTO_FUERA_CATALOGO
        senal = senal or f"hay conceptos fuera de catálogo: {', '.join(fuera)}"

    intencion = _intencion_regulatoria(normalizado, configuracion) if normalizado else None
    if intencion and _regla_activa(configuracion, MotivoDerivacion.INTENCION_REGULATORIA):
        reglas_disparadas.append(MotivoDerivacion.INTENCION_REGULATORIA.value)
        motivo = motivo or MotivoDerivacion.INTENCION_REGULATORIA
        senal = senal or f'intención regulatoria detectada ("{intencion}")'

    s1 = _desglose(factset)
    s2 = _unicidad(factset)
    s3, frase_repetida = _repregunta(utterance, historial, umbrales.similitud_repregunta)
    s6, turnos_sin_progreso = _sin_progreso(historial, umbrales.max_turnos_sin_progreso)
    # Fuera del score a propósito: mide la laguna del DATO, no la salud del diálogo.
    causal = _cobertura_causal(factset, configuracion.confianza.minima_para_explicar)

    comprension = pesos.w1 * s1 + pesos.w2 * s2 + pesos.w3 * (1.0 - s3) + pesos.w6 * (1.0 - s6)
    score = min(max(1.0 - comprension, 0.0), 1.0)

    derivar = bool(reglas_disparadas) or score > umbrales.tau_alto
    if not reglas_disparadas and derivar:
        motivo = MotivoDerivacion.UMBRAL_INCOMPRENSION
        reglas_disparadas.append(MotivoDerivacion.UMBRAL_INCOMPRENSION.value)
        detalles = []
        if s1 < 1.0:
            detalles.append(f"solo se desglosa el {round(s1 * 100)} % de la variación")
        if frase_repetida:
            detalles.append("el cliente repite la misma pregunta")
        if turnos_sin_progreso >= umbrales.max_turnos_sin_progreso:
            detalles.append(f"{turnos_sin_progreso} turnos sin avanzar")
        motivo_texto = "; ".join(detalles) if detalles else "la explicación no resulta suficiente"
        senal = f"umbral de incomprensión superado (U={round(score, 2)} > {umbrales.tau_alto}): "
        senal += motivo_texto

    # La histéresis protege una conversación humana ya empezada, no un número que subió
    # una vez. Mientras nadie haya recogido el expediente, cada turno se juzga por sus
    # propios méritos: si la causa de la derivación sigue ahí, la regla dura o el score
    # volverán a dispararla solos, y si no sigue, no hay nada que fijar. Con
    # `histeresis_requiere_asesor: false` se recupera el comportamiento antiguo.
    histeresis = derivado_previamente and umbrales.histeresis
    if histeresis and umbrales.histeresis_requiere_asesor and not asesor_en_sala:
        histeresis = False
    if histeresis:
        derivar = True
        if "HISTERESIS" not in reglas_disparadas:
            reglas_disparadas.append("HISTERESIS")
        motivo = motivo or MotivoDerivacion.UMBRAL_INCOMPRENSION
        senal = senal or "un asesor ya está atendiendo esta conversación"

    return ResultadoIncomprension(
        derivar=derivar,
        motivo=motivo,
        U=round(score, 4),
        senal_disparadora=senal,
        reglas_disparadas=reglas_disparadas,
        s1_cobertura=round(s1, 4),
        s2_unicidad=round(s2, 4),
        s3_repregunta=round(s3, 4),
        s6_sin_progreso=round(s6, 4),
        cobertura_causal=round(causal, 4),
        tau_alto=umbrales.tau_alto,
        tau_bajo=umbrales.tau_bajo,
        turnos_sin_progreso=turnos_sin_progreso,
        histeresis_aplicada=histeresis,
    )
