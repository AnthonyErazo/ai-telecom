"""Tasa de silencio post-explicación: la métrica de satisfacción que exige la ficha.

La ficha del Desafío 1 pide literalmente *"incorporar un mecanismo para clasificar el
nivel de satisfacción o «TASA DE SILENCIO POST-EXPLICACIÓN» (si el cliente entendió y
cerró la sesión)"*. Este módulo la implementa **sin encuestas**: como una sonda pasiva
sobre el comportamiento posterior del cliente.

Definición operativa
--------------------
1. Con cada explicación entregada se emite un ``silence_probe_id`` (determinista,
   derivado de ``conversation_id`` y ``trace_id``) que viaja en
   ``RespuestaCanalAgnostica.telemetria``.
2. Se observa si hay **turno posterior del cliente** dentro de una **ventana
   configurable** (:data:`VENTANA_SILENCIO_S_POR_DEFECTO`, 30 minutos).
3. La sonda se clasifica en tres resultados (:class:`ResultadoSonda`):

   ============================  =========================================================
   ``REPREGUNTA``                Hubo turno posterior y **es la misma consulta** (similitud
                                 de tokens ≥ umbral) o pide un asesor humano. También se
                                 clasifica así toda explicación que ya salió derivada.
                                 **No es éxito.**
   ``SILENCIO_COMPRENSION``      No hubo turno posterior **y hay una señal positiva de
                                 cierre** (ejecutó una acción sugerida, cerró la sesión,
                                 valoró bien), o el turno posterior trataba de otro
                                 asunto. **Es el único éxito.**
   ``ABANDONO_AMBIGUO``          Silencio **sin ninguna señal de cierre**. No sabemos si
                                 entendió o si se rindió y llamó al 104. **No es éxito.**
   ============================  =========================================================

Sesgo de la métrica — se declara, no se esconde
-----------------------------------------------
El silencio no prueba comprensión. Un cliente que no vuelve a escribir puede haber
entendido perfectamente **o** haber abandonado el canal digital y llamado al call
center, que es exactamente el fracaso que el proyecto quiere evitar. Por eso:

* ``ABANDONO_AMBIGUO`` **no cuenta como éxito**: se reporta aparte.
* :attr:`MetricasSilencio.tasa_silencio` es por tanto una **cota inferior** de la
  comprensión real.
* :attr:`MetricasSilencio.cota_superior_comprension` muestra qué saldría si el ambiguo
  se contase como éxito. La distancia entre ambas cifras **es** la incertidumbre de la
  métrica y debe presentarse junto a ella.
* La única forma de estrechar esa banda es cruzar con datos que aquí no existen
  (llamadas al 104 en las 24 h siguientes, reclamos posteriores). Queda anotado como
  **[POR VALIDAR]** con el equipo de Atención Digital.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import Canal, VeredictoVerificacion

__all__ = [
    "ADVERTENCIA_SESGO",
    "NAMESPACE_SONDA",
    "RUTA_RELATIVA_TELEMETRIA",
    "UMBRAL_REPREGUNTA_POR_DEFECTO",
    "VAR_ENTORNO_RUTA",
    "VAR_ENTORNO_VENTANA",
    "VENTANA_SILENCIO_S_POR_DEFECTO",
    "MetricasSilencio",
    "RegistroTelemetria",
    "ResultadoSonda",
    "SenalCierre",
    "SondaSilencio",
    "registro_telemetria_por_defecto",
    "similitud",
]

_LOG = logging.getLogger(__name__)

#: Ventana por defecto para considerar "silencio" tras la explicación (30 minutos).
VENTANA_SILENCIO_S_POR_DEFECTO = 1800

#: Umbral de similitud de tokens a partir del cual un turno posterior es una repregunta.
UMBRAL_REPREGUNTA_POR_DEFECTO = 0.80

#: Variables de entorno de configuración.
VAR_ENTORNO_VENTANA = "VENTANA_SILENCIO_S"
VAR_ENTORNO_RUTA = "TELEMETRIA_PATH"

#: Fichero JSONL donde se persisten las sondas (append-only, se reconstruye al leer).
RUTA_RELATIVA_TELEMETRIA = Path("data") / "telemetria" / "sondas.jsonl"

#: Espacio de nombres para derivar ``silence_probe_id`` de forma reproducible.
NAMESPACE_SONDA = uuid5(NAMESPACE_URL, "https://recibo-claro.movistar.pe/silencio")

#: Texto que acompaña obligatoriamente a la métrica cuando se publica.
ADVERTENCIA_SESGO = (
    "El silencio no prueba comprensión: quien no vuelve a escribir puede haber entendido "
    "o haber llamado al 104. Por eso ABANDONO_AMBIGUO (silencio sin ninguna señal de "
    "cierre) NO cuenta como éxito y la tasa publicada es una COTA INFERIOR de la "
    "comprensión real; cota_superior_comprension indica el resultado si el ambiguo se "
    "contara a favor. La banda entre ambas es la incertidumbre de la métrica."
)


# --------------------------------------------------------------------------- #
# Similitud (repregunta)
# --------------------------------------------------------------------------- #
def similitud(izquierda: str, derecha: str) -> float:
    """Similitud 0..1 entre dos mensajes del cliente, para detectar la repregunta.

    Reutiliza ``facts_engine.confianza.similitud_textos`` (Jaccard sobre tokens
    significativos) para que el umbral de repregunta sea **el mismo** que usa el score
    de incomprensión. Si ese módulo no estuviera disponible, cae a una implementación
    local equivalente: la telemetría nunca debe tumbar el proceso.
    """
    try:
        from packages.facts_engine.confianza import similitud_textos
    except Exception:  # fallback autocontenido
        return _similitud_local(izquierda, derecha)
    return similitud_textos(izquierda, derecha)


def _similitud_local(izquierda: str, derecha: str) -> float:
    """Jaccard sobre palabras en minúsculas; respaldo mínimo de :func:`similitud`."""
    uno = {palabra for palabra in izquierda.lower().split() if len(palabra) > 2}
    dos = {palabra for palabra in derecha.lower().split() if len(palabra) > 2}
    if not uno or not dos:
        return 0.0
    return len(uno & dos) / len(uno | dos)


def _ahora() -> datetime:
    """Instante actual en UTC (todas las marcas de la telemetría son conscientes de zona)."""
    return datetime.now(UTC)


def _en_utc(momento: datetime | None) -> datetime:
    """Normaliza a UTC; asume UTC si llega una marca ingenua."""
    if momento is None:
        return _ahora()
    if momento.tzinfo is None:
        return momento.replace(tzinfo=UTC)
    return momento.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Enumeraciones locales de la telemetría
# --------------------------------------------------------------------------- #
class ResultadoSonda(StrEnum):
    """Clasificación de una sonda de silencio. Solo el primero es éxito."""

    SILENCIO_COMPRENSION = "SILENCIO_COMPRENSION"
    REPREGUNTA = "REPREGUNTA"
    ABANDONO_AMBIGUO = "ABANDONO_AMBIGUO"
    PENDIENTE = "PENDIENTE"


class SenalCierre(StrEnum):
    """Evidencia de que la consulta quedó cerrada. Sin una de estas, el silencio es ambiguo."""

    ACCION_EJECUTADA = "ACCION_EJECUTADA"
    SESION_CERRADA = "SESION_CERRADA"
    VALORACION_POSITIVA = "VALORACION_POSITIVA"
    OTRO_ASUNTO = "OTRO_ASUNTO"


# --------------------------------------------------------------------------- #
# Modelos
# --------------------------------------------------------------------------- #
class SondaSilencio(BaseModel):
    """Una explicación entregada y lo que el cliente hizo (o no hizo) después."""

    model_config = ConfigDict(extra="forbid")

    silence_probe_id: UUID
    conversation_id: UUID
    trace_id: str
    emitida_en: datetime
    ventana_s: int = Field(gt=0)
    cuenta_ref: str | None = Field(default=None, description="Tokenizada; jamás DNI ni teléfono")
    periodo: str | None = None
    canal: Canal = Canal.APP
    utterance_origen: str = Field(default="", description="Pregunta que se explicó")
    causa_dominante: str | None = None
    derivada: bool = Field(default=False, description="La respuesta ya salió con hand-off")
    verificacion: VeredictoVerificacion = VeredictoVerificacion.PASS
    resultado: ResultadoSonda = ResultadoSonda.PENDIENTE
    resuelta_en: datetime | None = None
    senal_cierre: SenalCierre | None = None
    similitud_repregunta: float | None = Field(default=None, ge=0.0, le=1.0)
    segundos_hasta_turno: int | None = None
    detalle: str = ""
    revision: int = Field(default=0, ge=0, description="Nº de escritura en el JSONL")

    @property
    def vence_en(self) -> datetime:
        """Instante en que se cierra la ventana de observación."""
        return self.emitida_en + timedelta(seconds=self.ventana_s)

    @property
    def pendiente(self) -> bool:
        """Verdadero mientras la sonda no se haya clasificado."""
        return self.resultado is ResultadoSonda.PENDIENTE

    @property
    def es_exito(self) -> bool:
        """Solo ``SILENCIO_COMPRENSION`` cuenta como éxito. El ambiguo, no."""
        return self.resultado is ResultadoSonda.SILENCIO_COMPRENSION

    def vencida(self, ahora: datetime | None = None) -> bool:
        """Si la ventana ya se cerró para esta sonda."""
        return _en_utc(ahora) >= self.vence_en

    def a_telemetria(self) -> dict[str, Any]:
        """Bloque que se adjunta a ``RespuestaCanalAgnostica.telemetria``."""
        return {
            "silence_probe_id": str(self.silence_probe_id),
            "ventana_silencio_s": self.ventana_s,
            "vence_en": self.vence_en.isoformat(),
        }


class MetricasSilencio(BaseModel):
    """Agregado publicable de la tasa de silencio, con su sesgo declarado."""

    model_config = ConfigDict(extra="forbid")

    ventana_s: int
    total_sondas: int = 0
    resueltas: int = 0
    pendientes: int = 0
    silencio_comprension: int = 0
    repregunta: int = 0
    abandono_ambiguo: int = 0
    tasa_silencio: float = Field(default=0.0, ge=0.0, le=1.0)
    tasa_repregunta: float = Field(default=0.0, ge=0.0, le=1.0)
    tasa_abandono_ambiguo: float = Field(default=0.0, ge=0.0, le=1.0)
    cota_superior_comprension: float = Field(default=0.0, ge=0.0, le=1.0)
    advertencia_sesgo: str = ADVERTENCIA_SESGO

    def a_texto(self) -> str:
        """Render de una línea por métrica, con la advertencia de sesgo al final."""
        return "\n".join(
            [
                f"Tasa de silencio post-explicación (ventana {self.ventana_s} s)",
                f"  sondas resueltas ......... {self.resueltas} de {self.total_sondas}"
                f" ({self.pendientes} pendientes)",
                f"  SILENCIO_COMPRENSION ..... {self.silencio_comprension}"
                f"  ({self.tasa_silencio:.1%})  <- único éxito",
                f"  REPREGUNTA ............... {self.repregunta}"
                f"  ({self.tasa_repregunta:.1%})",
                f"  ABANDONO_AMBIGUO ......... {self.abandono_ambiguo}"
                f"  ({self.tasa_abandono_ambiguo:.1%})  <- NO cuenta como éxito",
                f"  banda de comprensión ..... {self.tasa_silencio:.1%}"
                f" – {self.cota_superior_comprension:.1%}",
                f"  {self.advertencia_sesgo}",
            ]
        )


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #
def _raiz_proyecto() -> Path:
    """Sube desde este fichero hasta el directorio que contiene ``pyproject.toml``."""
    actual = Path(__file__).resolve()
    for candidato in actual.parents:
        if (candidato / "pyproject.toml").is_file():
            return candidato
    return actual.parents[2]


def ruta_telemetria_por_defecto() -> Path:
    """Ruta del JSONL de sondas: ``$TELEMETRIA_PATH`` o ``data/telemetria/sondas.jsonl``."""
    desde_entorno = os.getenv(VAR_ENTORNO_RUTA)
    if desde_entorno:
        return Path(desde_entorno)
    return _raiz_proyecto() / RUTA_RELATIVA_TELEMETRIA


def _ventana_por_defecto() -> int:
    """Lee la ventana de ``$VENTANA_SILENCIO_S`` con validación y valor de reserva."""
    bruto = os.getenv(VAR_ENTORNO_VENTANA)
    if not bruto:
        return VENTANA_SILENCIO_S_POR_DEFECTO
    try:
        valor = int(bruto)
    except ValueError:
        _LOG.warning("%s=%r no es un entero; se usa %s", VAR_ENTORNO_VENTANA, bruto,
                     VENTANA_SILENCIO_S_POR_DEFECTO)
        return VENTANA_SILENCIO_S_POR_DEFECTO
    if valor <= 0:
        _LOG.warning("%s debe ser positivo; se usa %s", VAR_ENTORNO_VENTANA,
                     VENTANA_SILENCIO_S_POR_DEFECTO)
        return VENTANA_SILENCIO_S_POR_DEFECTO
    return valor


class RegistroTelemetria:
    """Sondas de silencio: apertura, resolución y agregación.

    El estado vive en memoria y se persiste en un JSONL **append-only**: cada cambio de
    una sonda escribe una revisión nueva, y al cargar se conserva la última de cada
    ``silence_probe_id``. Así el fichero es reproducible y auditable con las mismas
    reglas que el registro de auditoría, sin necesidad de reescribir líneas.
    """

    def __init__(
        self,
        ruta: str | Path | None = None,
        *,
        ventana_s: int | None = None,
        umbral_repregunta: float = UMBRAL_REPREGUNTA_POR_DEFECTO,
        persistir: bool = True,
        otro_asunto_es_exito: bool = True,
    ) -> None:
        """Prepara el registro y recarga las sondas ya escritas.

        Args:
            ruta: fichero JSONL de sondas.
            ventana_s: ventana de observación en segundos. Por defecto,
                ``$VENTANA_SILENCIO_S`` o 30 minutos.
            umbral_repregunta: similitud a partir de la cual un turno posterior cuenta
                como repregunta (misma escala que ``rules.yaml``).
            persistir: si es ``False``, todo queda en memoria (pruebas).
            otro_asunto_es_exito: si un turno posterior trata de **otro** asunto, la
                consulta original se considera cerrada. Es un supuesto explícito y
                configurable: apagarlo endurece la métrica.
        """
        self.ruta = Path(ruta) if ruta is not None else ruta_telemetria_por_defecto()
        self.ventana_s = ventana_s if ventana_s is not None else _ventana_por_defecto()
        self.umbral_repregunta = umbral_repregunta
        self.persistir = persistir
        self.otro_asunto_es_exito = otro_asunto_es_exito
        self._cerrojo = threading.RLock()
        self._sondas: dict[UUID, SondaSilencio] = {}
        if self.persistir:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            self._cargar()

    # ------------------------------------------------------------------ #
    # Persistencia
    # ------------------------------------------------------------------ #
    def _cargar(self) -> None:
        """Reconstruye el estado quedándose con la última revisión de cada sonda."""
        if not self.ruta.is_file():
            return
        with self.ruta.open("r", encoding="utf-8") as fichero:
            for numero, bruta in enumerate(fichero):
                linea = bruta.strip()
                if not linea:
                    continue
                try:
                    sonda = SondaSilencio.model_validate_json(linea)
                except Exception as error:  # línea corrupta: se avisa
                    _LOG.warning("sonda ilegible en %s línea %d: %s", self.ruta, numero, error)
                    continue
                previa = self._sondas.get(sonda.silence_probe_id)
                if previa is None or sonda.revision >= previa.revision:
                    self._sondas[sonda.silence_probe_id] = sonda

    def _persistir_sonda(self, sonda: SondaSilencio) -> None:
        """Añade una revisión de la sonda al JSONL."""
        if not self.persistir:
            return
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        linea = json.dumps(
            sonda.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.ruta.open("a", encoding="utf-8", newline="\n") as fichero:
            fichero.write(linea + "\n")

    def _guardar(self, sonda: SondaSilencio) -> SondaSilencio:
        """Sube la revisión, actualiza el índice en memoria y persiste."""
        sonda.revision += 1
        self._sondas[sonda.silence_probe_id] = sonda
        self._persistir_sonda(sonda)
        return sonda

    # ------------------------------------------------------------------ #
    # Ciclo de vida de la sonda
    # ------------------------------------------------------------------ #
    @staticmethod
    def id_sonda(conversation_id: UUID | str, trace_id: str) -> UUID:
        """``silence_probe_id`` determinista para un turno (la demo es reproducible)."""
        return uuid5(NAMESPACE_SONDA, f"{conversation_id}|{trace_id}")

    def abrir_sonda(
        self,
        conversation_id: UUID | str,
        trace_id: str,
        *,
        cuenta_ref: str | None = None,
        periodo: str | None = None,
        canal: Canal = Canal.APP,
        utterance_origen: str = "",
        causa_dominante: str | None = None,
        derivada: bool = False,
        verificacion: VeredictoVerificacion = VeredictoVerificacion.PASS,
        ventana_s: int | None = None,
        ts: datetime | None = None,
    ) -> SondaSilencio:
        """Abre la sonda que acompaña a una explicación entregada.

        Una respuesta que ya sale **derivada** a un asesor se resuelve en el acto como
        ``REPREGUNTA``: el asistente no cerró la consulta, y contarla como silencio
        inflaría la métrica.

        Returns:
            La sonda; su ``a_telemetria()`` es lo que se adjunta a la respuesta.
        """
        identificador = self.id_sonda(conversation_id, trace_id)
        momento = _en_utc(ts)
        with self._cerrojo:
            existente = self._sondas.get(identificador)
            if existente is not None:
                return existente
            sonda = SondaSilencio(
                silence_probe_id=identificador,
                conversation_id=UUID(str(conversation_id)),
                trace_id=trace_id,
                emitida_en=momento,
                ventana_s=ventana_s or self.ventana_s,
                cuenta_ref=cuenta_ref,
                periodo=periodo,
                canal=canal,
                utterance_origen=utterance_origen,
                causa_dominante=causa_dominante,
                derivada=derivada,
                verificacion=verificacion,
            )
            if derivada:
                sonda.resultado = ResultadoSonda.REPREGUNTA
                sonda.resuelta_en = momento
                sonda.detalle = "la respuesta salió con derivación a asesor"
            return self._guardar(sonda)

    def registrar_turno_usuario(
        self,
        conversation_id: UUID | str,
        utterance: str = "",
        *,
        ts: datetime | None = None,
        pide_humano: bool = False,
    ) -> list[SondaSilencio]:
        """Registra un mensaje posterior del cliente y resuelve las sondas afectadas.

        Dentro de la ventana, el turno es ``REPREGUNTA`` si repite la consulta (o pide
        un humano) y ``SILENCIO_COMPRENSION`` con señal ``OTRO_ASUNTO`` si habla de otra
        cosa. Fuera de la ventana no reabre nada: la sonda ya se habrá cerrado por
        vencimiento.

        Returns:
            Las sondas que este turno acaba de resolver.
        """
        momento = _en_utc(ts)
        resueltas: list[SondaSilencio] = []
        with self._cerrojo:
            for sonda in self._pendientes_de(conversation_id):
                if momento >= sonda.vence_en:
                    continue
                parecido = similitud(sonda.utterance_origen, utterance)
                sonda.similitud_repregunta = round(parecido, 4)
                sonda.segundos_hasta_turno = max(
                    0, int((momento - sonda.emitida_en).total_seconds())
                )
                sonda.resuelta_en = momento
                if pide_humano or parecido >= self.umbral_repregunta:
                    sonda.resultado = ResultadoSonda.REPREGUNTA
                    sonda.detalle = (
                        "pide asesor humano"
                        if pide_humano
                        else f"repregunta (similitud {parecido:.2f} ≥ {self.umbral_repregunta:.2f})"
                    )
                elif self.otro_asunto_es_exito:
                    sonda.resultado = ResultadoSonda.SILENCIO_COMPRENSION
                    sonda.senal_cierre = SenalCierre.OTRO_ASUNTO
                    sonda.detalle = "el cliente pasó a otro asunto: la consulta quedó cerrada"
                else:
                    sonda.resultado = ResultadoSonda.ABANDONO_AMBIGUO
                    sonda.detalle = "turno posterior sobre otro asunto, sin señal de cierre"
                resueltas.append(self._guardar(sonda))
        return resueltas

    def registrar_senal_cierre(
        self,
        referencia: UUID | str,
        senal: SenalCierre,
        *,
        ts: datetime | None = None,
    ) -> list[SondaSilencio]:
        """Anota una señal positiva de cierre sobre una conversación o una sonda concreta.

        Es lo que convierte un silencio en :class:`ResultadoSonda.SILENCIO_COMPRENSION`
        en vez de en abandono ambiguo: el cliente pagó, abrió el detalle, cerró la sesión
        o valoró bien la explicación.

        Args:
            referencia: ``silence_probe_id`` o ``conversation_id``.
            senal: tipo de señal observada.
            ts: instante de la señal.
        """
        momento = _en_utc(ts)
        afectadas: list[SondaSilencio] = []
        with self._cerrojo:
            directa = self._sondas.get(_uuid_o_none(referencia))
            objetivo = [directa] if directa is not None else self._pendientes_de(referencia)
            for sonda in objetivo:
                if sonda is None or not sonda.pendiente:
                    continue
                sonda.senal_cierre = senal
                sonda.resultado = ResultadoSonda.SILENCIO_COMPRENSION
                sonda.resuelta_en = momento
                sonda.detalle = f"señal de cierre: {senal}"
                afectadas.append(self._guardar(sonda))
        return afectadas

    def cerrar_vencidas(self, ahora: datetime | None = None) -> list[SondaSilencio]:
        """Clasifica las sondas cuya ventana ya expiró sin turno posterior.

        Sin señal de cierre el resultado es ``ABANDONO_AMBIGUO``, **no** silencio de
        comprensión: es el punto exacto donde esta métrica se niega a ser optimista.
        """
        momento = _en_utc(ahora)
        cerradas: list[SondaSilencio] = []
        with self._cerrojo:
            for sonda in list(self._sondas.values()):
                if not sonda.pendiente or momento < sonda.vence_en:
                    continue
                sonda.resuelta_en = momento
                if sonda.senal_cierre is not None:
                    sonda.resultado = ResultadoSonda.SILENCIO_COMPRENSION
                    sonda.detalle = f"silencio con señal de cierre: {sonda.senal_cierre}"
                else:
                    sonda.resultado = ResultadoSonda.ABANDONO_AMBIGUO
                    sonda.detalle = "ventana cerrada sin turno posterior ni señal de cierre"
                cerradas.append(self._guardar(sonda))
        return cerradas

    # ------------------------------------------------------------------ #
    # Consultas
    # ------------------------------------------------------------------ #
    def _pendientes_de(self, conversation_id: UUID | str) -> list[SondaSilencio]:
        """Sondas sin resolver de una conversación, de la más antigua a la más nueva."""
        clave = str(conversation_id)
        return sorted(
            (
                sonda
                for sonda in self._sondas.values()
                if str(sonda.conversation_id) == clave and sonda.pendiente
            ),
            key=lambda sonda: sonda.emitida_en,
        )

    def sonda(self, silence_probe_id: UUID | str) -> SondaSilencio | None:
        """Devuelve una sonda por su identificador."""
        return self._sondas.get(_uuid_o_none(silence_probe_id))

    def sondas(
        self,
        *,
        conversation_id: UUID | str | None = None,
        canal: Canal | None = None,
    ) -> list[SondaSilencio]:
        """Todas las sondas conocidas, ordenadas por emisión, con filtros opcionales."""
        seleccion: Iterable[SondaSilencio] = self._sondas.values()
        if conversation_id is not None:
            clave = str(conversation_id)
            seleccion = (s for s in seleccion if str(s.conversation_id) == clave)
        if canal is not None:
            seleccion = (s for s in seleccion if s.canal is canal)
        return sorted(seleccion, key=lambda sonda: (sonda.emitida_en, str(sonda.silence_probe_id)))

    def metricas(
        self,
        *,
        ahora: datetime | None = None,
        canal: Canal | None = None,
        cerrar_vencidas: bool = True,
    ) -> MetricasSilencio:
        """Agrega las sondas en la métrica publicable.

        Args:
            ahora: instante de corte (permite evaluación determinista en los tests).
            canal: restringe a un canal (App, Bot, WhatsApp).
            cerrar_vencidas: clasifica antes las sondas cuya ventana ya expiró.

        Returns:
            Las tasas sobre las sondas **resueltas**; las pendientes quedan fuera del
            denominador para no diluir la cifra con ventanas todavía abiertas.
        """
        if cerrar_vencidas:
            self.cerrar_vencidas(ahora)
        universo = self.sondas(canal=canal)
        return agregar_metricas(universo, ventana_s=self.ventana_s)


def _uuid_o_none(valor: UUID | str) -> UUID:
    """Convierte a ``UUID``; devuelve un UUID nulo si el texto no lo es."""
    if isinstance(valor, UUID):
        return valor
    try:
        return UUID(str(valor))
    except (ValueError, AttributeError, TypeError):
        return UUID(int=0)


def agregar_metricas(
    sondas: Sequence[SondaSilencio], *, ventana_s: int = VENTANA_SILENCIO_S_POR_DEFECTO
) -> MetricasSilencio:
    """Calcula :class:`MetricasSilencio` sobre una colección de sondas ya clasificadas."""
    total = len(sondas)
    pendientes = sum(1 for sonda in sondas if sonda.pendiente)
    silencio = sum(1 for sonda in sondas if sonda.resultado is ResultadoSonda.SILENCIO_COMPRENSION)
    repregunta = sum(1 for sonda in sondas if sonda.resultado is ResultadoSonda.REPREGUNTA)
    ambiguo = sum(1 for sonda in sondas if sonda.resultado is ResultadoSonda.ABANDONO_AMBIGUO)
    resueltas = silencio + repregunta + ambiguo
    divisor = resueltas or 1
    return MetricasSilencio(
        ventana_s=ventana_s,
        total_sondas=total,
        resueltas=resueltas,
        pendientes=pendientes,
        silencio_comprension=silencio,
        repregunta=repregunta,
        abandono_ambiguo=ambiguo,
        tasa_silencio=silencio / divisor if resueltas else 0.0,
        tasa_repregunta=repregunta / divisor if resueltas else 0.0,
        tasa_abandono_ambiguo=ambiguo / divisor if resueltas else 0.0,
        cota_superior_comprension=(silencio + ambiguo) / divisor if resueltas else 0.0,
    )


_REGISTRO_POR_DEFECTO: RegistroTelemetria | None = None
_CERROJO_SINGLETON = threading.Lock()


def registro_telemetria_por_defecto(ruta: str | Path | None = None) -> RegistroTelemetria:
    """Devuelve el registro de telemetría compartido del proceso.

    Pasar ``ruta`` fuerza su recreación apuntando a otro fichero (pruebas, evaluación).
    """
    global _REGISTRO_POR_DEFECTO
    with _CERROJO_SINGLETON:
        if _REGISTRO_POR_DEFECTO is None or ruta is not None:
            _REGISTRO_POR_DEFECTO = RegistroTelemetria(ruta)
        return _REGISTRO_POR_DEFECTO
