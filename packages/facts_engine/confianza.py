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

       s1 = cobertura del Δ explicado = Σ|Δ_atribuidos| / |Δ_total|
       s2 = unicidad de causa         = 1 − H(p_causas)/log(k)
       s3 = repregunta                (similitud entre turnos consecutivos > 0.80)
       s6 = turnos sin progreso       (normalizado por max_turnos_sin_progreso)

       U = 1 − (w1·s1 + w2·s2 + w3·(1−s3) + w6·(1−s6)),   Σw = 1
       DERIVAR si U > τ_alto (0.65)

   Con **histéresis**: una vez derivada, la conversación no vuelve al asistente.

Los pesos y umbrales viven en ``rules.yaml``. Aquí no hay aritmética monetaria: los
importes solo se leen del FactSet, ya en céntimos enteros.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import MotivoDerivacion
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import Derivacion
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas
from packages.facts_engine.atribucion import esta_atribuida

__all__ = [
    "PATRONES_PETICION_HUMANO",
    "ResultadoIncomprension",
    "Turno",
    "entropia_normalizada",
    "evaluar_incomprension",
    "normalizar_texto",
    "similitud_textos",
]

#: Formas en que un cliente peruano pide un humano. Es una regla dura: se deriva sin más.
PATRONES_PETICION_HUMANO: tuple[str, ...] = (
    "asesor",
    "humano",
    "persona real",
    "una persona",
    "operador",
    "ejecutivo",
    "representante",
    "agente",
    "hablar con alguien",
    "quiero hablar con",
    "comunicarme con",
    "atencion al cliente",
    "call center",
    "telefono de atencion",
)

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


def normalizar_texto(texto: str) -> str:
    """Minúsculas sin tildes ni signos: la forma canónica para comparar frases."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(caracter for caracter in descompuesto if unicodedata.category(caracter) != "Mn")


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
    s1_cobertura: float = Field(default=1.0, ge=0.0, le=1.0)
    s2_unicidad: float = Field(default=1.0, ge=0.0, le=1.0)
    s3_repregunta: float = Field(default=0.0, ge=0.0, le=1.0)
    s6_sin_progreso: float = Field(default=0.0, ge=0.0, le=1.0)
    tau_alto: float = 0.65
    tau_bajo: float = 0.35
    turnos_sin_progreso: int = 0
    histeresis_aplicada: bool = False

    @property
    def score(self) -> float:
        """Alias legible de ``U``."""
        return self.U

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


def _pide_humano(utterance_normalizado: str) -> str | None:
    """Devuelve el patrón que delata la petición explícita de un asesor."""
    for patron in PATRONES_PETICION_HUMANO:
        if patron in utterance_normalizado:
            return patron
    return None


def _intencion_regulatoria(utterance_normalizado: str, reglas: ConfiguracionReglas) -> str | None:
    """Detecta reclamo formal, baja, portabilidad y demás intenciones regulatorias."""
    for intencion in reglas.umbrales_incomprension.intenciones_regulatorias:
        if normalizar_texto(intencion) in utterance_normalizado:
            return intencion
    return None


def _cobertura(factset: FactSet, confianza_minima: float) -> float:
    """``s1``: parte del delta total que queda efectivamente explicada."""
    if factset.delta_total_cent == 0:
        return 1.0
    explicado = sum(
        abs(linea.delta_cent)
        for linea in factset.lineas
        if linea.se_explica and esta_atribuida(linea, confianza_minima)
    )
    return min(explicado / abs(factset.delta_total_cent), 1.0)


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
    """``s6``: turnos consecutivos del cliente sin que la conversación avance."""
    cuenta = 0
    for turno in reversed(historial):
        if turno.rol != "cliente":
            continue
        if turno.progreso:
            break
        cuenta += 1
    return (min(cuenta / maximo, 1.0) if maximo > 0 else 0.0), cuenta


def evaluar_incomprension(
    factset: FactSet,
    historial_turnos: Sequence[Turno | str] | None = None,
    utterance: str = "",
    *,
    reglas: ConfiguracionReglas | None = None,
    derivado_previamente: bool = False,
    conceptos_fuera_catalogo: Sequence[str] | None = None,
) -> ResultadoIncomprension:
    """Decide si la conversación debe pasar a un asesor humano (sección 4.8).

    Primero se evalúan las **reglas duras**, que derivan sin score; si ninguna se
    dispara, se calcula::

        U = 1 − (w1·s1 + w2·s2 + w3·(1−s3) + w6·(1−s6))
        DERIVAR si U > τ_alto

    Un ``U`` alto significa "el sistema no está entendiendo o no está explicando": poca
    cobertura del delta, causas dispersas, el cliente repreguntando lo mismo y varios
    turnos sin avanzar.

    Args:
        factset: hechos del recibo ya conciliados (aporta ``s1`` y ``s2``).
        historial_turnos: turnos previos de la conversación (``Turno`` o texto suelto).
        utterance: mensaje actual del cliente. Entra como **dato**, nunca como
            instrucción, y solo se usa para detección de patrones.
        reglas: configuración; por defecto ``cargar_reglas()``.
        derivado_previamente: si la conversación ya fue derivada. Con histéresis
            activada, no se vuelve atrás.
        conceptos_fuera_catalogo: conceptos del recibo que el catálogo no conoce
            (los calcula ``diff.comparar_detallado``).

    Returns:
        El veredicto completo, con los cuatro componentes del score, para que la
        auditoría pueda reconstruir la decisión.
    """
    configuracion = reglas if reglas is not None else cargar_reglas()
    umbrales = configuracion.umbrales_incomprension
    pesos = umbrales.pesos
    historial = _normalizar_historial(historial_turnos)
    normalizado = normalizar_texto(utterance)

    reglas_disparadas: list[str] = []
    motivo: MotivoDerivacion | None = None
    senal: str | None = None

    patron = _pide_humano(normalizado) if normalizado else None
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

    s1 = _cobertura(factset, configuracion.confianza.minima_para_explicar)
    s2 = _unicidad(factset)
    s3, frase_repetida = _repregunta(utterance, historial, umbrales.similitud_repregunta)
    s6, turnos_sin_progreso = _sin_progreso(historial, umbrales.max_turnos_sin_progreso)

    comprension = pesos.w1 * s1 + pesos.w2 * s2 + pesos.w3 * (1.0 - s3) + pesos.w6 * (1.0 - s6)
    score = min(max(1.0 - comprension, 0.0), 1.0)

    derivar = bool(reglas_disparadas) or score > umbrales.tau_alto
    if not reglas_disparadas and derivar:
        motivo = MotivoDerivacion.UMBRAL_INCOMPRENSION
        reglas_disparadas.append(MotivoDerivacion.UMBRAL_INCOMPRENSION.value)
        detalles = []
        if s1 < 1.0:
            detalles.append(f"solo se explica el {round(s1 * 100)} % de la variación")
        if frase_repetida:
            detalles.append("el cliente repite la misma pregunta")
        if turnos_sin_progreso >= umbrales.max_turnos_sin_progreso:
            detalles.append(f"{turnos_sin_progreso} turnos sin avanzar")
        motivo_texto = "; ".join(detalles) if detalles else "la explicación no resulta suficiente"
        senal = f"umbral de incomprensión superado (U={round(score, 2)} > {umbrales.tau_alto}): "
        senal += motivo_texto

    histeresis = derivado_previamente and umbrales.histeresis
    if histeresis:
        derivar = True
        if "HISTERESIS" not in reglas_disparadas:
            reglas_disparadas.append("HISTERESIS")
        motivo = motivo or MotivoDerivacion.UMBRAL_INCOMPRENSION
        senal = senal or "la conversación ya había sido derivada a un asesor"

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
        tau_alto=umbrales.tau_alto,
        tau_bajo=umbrales.tau_bajo,
        turnos_sin_progreso=turnos_sin_progreso,
        histeresis_aplicada=histeresis,
    )
