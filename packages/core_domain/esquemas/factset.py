"""``FactSet``: el único lugar del que pueden salir cifras.

Regla innegociable nº 2: el LLM no calcula. Recibe este objeto ya validado y solo
puede redactar con lo que hay dentro. Regla nº 4: el conjunto ``ALLOWED`` del
verificador se construye **exclusivamente** desde aquí — ninguna cifra de un
documento recuperado puede sobrevivir al texto final.

Todo el módulo gira alrededor de dos métodos:

* :meth:`FactSet.calcular_sha256` — integridad de lo que vio el modelo.
* :meth:`FactSet.tokens_permitidos` — base del verificador numérico.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.core_domain.dinero import Centimos
from packages.core_domain.enums import (
    CausaOficial,
    ClaseDelta,
    EstadoServicio,
    FamiliaConcepto,
    ModalidadRenta,
    TipoMovimiento,
)
from packages.core_domain.esquemas.movimiento import PlanFinanciamiento
from packages.core_domain.esquemas.recibo import Periodo, Tramo

__all__ = [
    "CAMPOS_EXCLUIDOS_DEL_HASH",
    "NAMESPACE_FACTSET",
    "TOLERANCIA_RESIDUAL_CENT",
    "CausaAgregada",
    "FactSet",
    "Invariante",
    "LineaDelta",
    "token_entero",
    "token_fecha",
    "token_monto",
    "token_periodo",
    "token_porcentaje",
]

#: Tolerancia del invariante de conciliación: por encima de esto NO se explica, se deriva.
TOLERANCIA_RESIDUAL_CENT = 1

#: Espacio de nombres para derivar ``factset_id`` de forma determinista (demo reproducible).
NAMESPACE_FACTSET = uuid5(NAMESPACE_URL, "https://recibo-claro.movistar.pe/factset")

#: Campos que NO entran en el hash: el propio hash y la marca de tiempo de generación
#: (si entrara, la demo dejaría de ser byte-reproducible).
CAMPOS_EXCLUIDOS_DEL_HASH = frozenset({"sha256", "generado_en"})


# --------------------------------------------------------------------------- #
# Tokens numéricos — vocabulario común entre el FactSet y el verificador
# --------------------------------------------------------------------------- #
def token_monto(centimos: int) -> str:
    """Token canónico de un importe: ``12490 -> "cent:12490"``.

    El prefijo evita colisiones entre magnitudes distintas: los 12 días de un
    prorrateo (``num:12``) no anclan un importe de S/ 0.12 (``cent:12``).
    """
    return f"cent:{int(centimos)}"


def token_entero(valor: int) -> str:
    """Token canónico de un entero adimensional: días, cuotas, cantidades, años."""
    return f"num:{int(valor)}"


def token_porcentaje(valor: float | int | Decimal | str) -> str:
    """Token canónico de un porcentaje, normalizado a dos decimales.

    ``18``, ``18.0`` y ``"18,00"`` producen el mismo token ``pct:18.00``.
    """
    texto = str(valor).replace(",", ".").replace("%", "").strip()
    normalizado = Decimal(texto).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"pct:{normalizado}"


def token_fecha(valor: date | datetime | str) -> str:
    """Token canónico de una fecha en ISO: ``fecha:2026-07-12``."""
    if isinstance(valor, datetime):
        valor = valor.date()
    if isinstance(valor, date):
        return f"fecha:{valor.isoformat()}"
    return f"fecha:{valor}"


def token_periodo(periodo: str) -> str:
    """Token canónico de un periodo de facturación: ``periodo:2026-07``."""
    return f"periodo:{periodo}"


# --------------------------------------------------------------------------- #
# Piezas del FactSet
# --------------------------------------------------------------------------- #
class LineaDelta(BaseModel):
    """Diferencia de un concepto entre el recibo actual y el previo.

    ``delta_cent`` siempre es ``monto_actual_cent - monto_previo_cent`` y ``clase`` se
    deduce de ambos montos: las dos cosas se validan, no se confía en el llamador.
    Las líneas ``IGUAL`` existen en el FactSet pero **no se explican**.
    """

    model_config = ConfigDict(extra="forbid")

    concepto_id: str
    nombre_comercial: str
    clase: ClaseDelta
    monto_actual_cent: Centimos
    monto_previo_cent: Centimos
    delta_cent: Centimos
    causa: TipoMovimiento | None = None
    movimiento_id: int | None = None
    dias_prorrateo: int | None = None
    tramos: list[Tramo] | None = Field(
        default=None, description="Explicación auditable del prorrateo"
    )
    confianza: float = Field(ge=0.0, le=1.0)
    evidencia: list[str] = Field(
        default_factory=list,
        description='Referencias citables: ["linea:441", "mov:77", "cat:PRORRATEO_PLAN"]',
    )
    # --- campos opcionales de apoyo narrativo (no alteran el contrato de 3.3) ---
    familia: FamiliaConcepto | None = None
    causa_oficial: CausaOficial | None = None
    cuota_numero: int | None = None
    cuotas_totales: int | None = None

    @staticmethod
    def clasificar(monto_actual_cent: int, monto_previo_cent: int) -> ClaseDelta:
        """Clasifica un par de montos según la sección 4.6 de la especificación."""
        delta = monto_actual_cent - monto_previo_cent
        if delta == 0:
            return ClaseDelta.IGUAL
        if monto_previo_cent == 0:
            return ClaseDelta.NUEVO
        if monto_actual_cent == 0:
            return ClaseDelta.DESAPARECIDO
        return ClaseDelta.SUBIO if delta > 0 else ClaseDelta.BAJO

    @model_validator(mode="after")
    def _validar_aritmetica(self) -> Self:
        esperado = self.monto_actual_cent - self.monto_previo_cent
        if self.delta_cent != esperado:
            raise ValueError(
                f"{self.concepto_id}: delta_cent={self.delta_cent} pero actual - previo = {esperado}"
            )
        clase_esperada = self.clasificar(self.monto_actual_cent, self.monto_previo_cent)
        if self.clase != clase_esperada:
            raise ValueError(
                f"{self.concepto_id}: clase={self.clase} pero corresponde {clase_esperada}"
            )
        return self

    @property
    def se_explica(self) -> bool:
        """Las líneas sin variación no se narran."""
        return self.clase is not ClaseDelta.IGUAL

    @property
    def causa_confirmada(self) -> bool:
        """Si se sabe **por qué** se movió esta línea, y no solo cuánto.

        Son dos preguntas distintas y el sistema debe poder responder la primera aunque
        no pueda responder la segunda. Cuánto y de qué línea sale del propio recibo y
        está siempre; el porqué necesita una orden del CRM (``causa``) o una causa
        oficial del catálogo (``causa_oficial``), y el dataset del desafío no trae
        órdenes. Cuando esto vale ``False`` la explicación sigue saliendo, pero tiene
        que decir con todas las letras qué no se puede confirmar y por qué.
        """
        return self.causa is not None or self.causa_oficial is not None


class Invariante(BaseModel):
    """Conciliación entre el delta total y la suma de deltas por línea.

    Si ``|residual_cent| > 1`` el sistema **no explica**: responde 409
    ``INVARIANTE_FALLIDO`` y deriva a un asesor. Nunca hay "explicación aproximada".
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    residual_cent: Centimos = Field(description="Debe ser 0 (tolerancia ±1 céntimo)")
    suma_deltas_cent: Centimos
    delta_total_cent: Centimos

    @classmethod
    def evaluar(
        cls,
        delta_total_cent: int,
        deltas: list[int],
        tolerancia_cent: int = TOLERANCIA_RESIDUAL_CENT,
    ) -> Invariante:
        """Construye el invariante a partir del delta total y los deltas por línea."""
        suma = sum(deltas)
        residual = delta_total_cent - suma
        return cls(
            ok=abs(residual) <= tolerancia_cent,
            residual_cent=residual,
            suma_deltas_cent=suma,
            delta_total_cent=delta_total_cent,
        )


class CausaAgregada(BaseModel):
    """Agrupación de deltas por causa, en el vocabulario del cliente.

    Es lo que se narra: "S/ 45.00 por el cambio de plan y S/ 12.90 por el paquete
    de datos". ``participacion_bp`` va en puntos básicos (10000 = 100 %) para no
    introducir ``float`` en nada que se parezca a un reparto.
    """

    model_config = ConfigDict(extra="forbid")

    causa: TipoMovimiento | None = None
    causa_oficial: CausaOficial | None = None
    etiqueta_cliente: str
    monto_cent: Centimos
    participacion_bp: int = Field(default=0, description="Peso sobre |delta total|, en bp")
    conceptos: list[str] = Field(default_factory=list)
    movimientos: list[int] = Field(default_factory=list)
    confianza: float = Field(default=1.0, ge=0.0, le=1.0)
    evidencia: list[str] = Field(default_factory=list)

    @property
    def participacion_pct(self) -> Decimal:
        """Participación en porcentaje con dos decimales, derivada de ``participacion_bp``."""
        return (Decimal(self.participacion_bp) / 100).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
# FactSet
# --------------------------------------------------------------------------- #
class FactSet(BaseModel):
    """Fotografía verificada de la variación de un recibo. Contrato con el LLM.

    Se construye en ``packages.facts_engine.motor`` y viaja intacto hasta el prompt,
    el verificador y la auditoría. Su ``sha256`` demuestra que el texto entregado se
    generó sobre estos y no otros hechos.
    """

    model_config = ConfigDict(extra="forbid")

    factset_id: UUID
    cuenta_id: str = Field(description="Ficticio tokenizado, jamás DNI ni teléfono")
    modalidad_renta: ModalidadRenta
    periodo_actual: Periodo
    periodo_previo: Periodo
    dias_ciclo: int = Field(gt=0)
    total_actual_cent: Centimos
    total_previo_cent: Centimos
    delta_total_cent: Centimos
    lineas: list[LineaDelta] = Field(default_factory=list)
    causas_agregadas: list[CausaAgregada] = Field(default_factory=list)
    invariante: Invariante
    deuda_anterior_cent: Centimos = 0
    confianza_global: float = Field(ge=0.0, le=1.0)
    rules_version: str
    sha256: str = Field(default="", description="Integridad de lo que vio el LLM")

    # --- contexto opcional, útil para narrar y para anclar fechas ---
    generado_en: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ciclo_inicio: date | None = None
    ciclo_fin: date | None = Field(default=None, description="Exclusivo")
    fecha_vencimiento: date | None = None
    estado_servicio: EstadoServicio = EstadoServicio.ACTIVO
    plan_vigente: str | None = None
    financiamientos: list[PlanFinanciamiento] = Field(default_factory=list)
    beneficios_vigentes: list[str] = Field(
        default_factory=list,
        description="Beneficios que el cliente YA tiene (efecto efervescente); solo texto",
    )
    movimientos_ciclo: list[int] = Field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Validación
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _validar_totales(self) -> Self:
        esperado = self.total_actual_cent - self.total_previo_cent
        if self.delta_total_cent != esperado:
            raise ValueError(
                f"delta_total_cent={self.delta_total_cent} pero actual - previo = {esperado}"
            )
        if self.invariante.delta_total_cent != self.delta_total_cent:
            raise ValueError("el invariante no habla del mismo delta total que el FactSet")
        if self.periodo_previo >= self.periodo_actual:
            raise ValueError(
                f"periodo_previo ({self.periodo_previo}) debe ser anterior a "
                f"periodo_actual ({self.periodo_actual})"
            )
        return self

    # ------------------------------------------------------------------ #
    # Identidad e integridad
    # ------------------------------------------------------------------ #
    @staticmethod
    def id_determinista(cuenta_id: str, periodo: str, rules_version: str) -> UUID:
        """UUID reproducible para un (cuenta, periodo, versión de reglas).

        La demo debe ser byte-reproducible: un identificador aleatorio rompería el
        hash y, con él, la comparación de ejecuciones.
        """
        return uuid5(NAMESPACE_FACTSET, f"{cuenta_id}|{periodo}|{rules_version}")

    def json_canonico(self) -> str:
        """JSON determinista del FactSet: claves ordenadas, sin espacios, UTF-8.

        Excluye ``sha256`` (no puede firmarse a sí mismo) y ``generado_en`` (marca de
        tiempo volátil). Es exactamente el texto sobre el que se calcula el hash.
        """
        datos = self.model_dump(mode="json", exclude=set(CAMPOS_EXCLUIDOS_DEL_HASH))
        return json.dumps(datos, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def calcular_sha256(self) -> str:
        """SHA-256 hexadecimal del JSON canónico (sin modificar el objeto)."""
        return hashlib.sha256(self.json_canonico().encode("utf-8")).hexdigest()

    def sellar(self) -> FactSet:
        """Escribe ``sha256`` con el hash recién calculado y devuelve el propio FactSet."""
        self.sha256 = self.calcular_sha256()
        return self

    def verificar_sha256(self) -> bool:
        """Comprueba que el ``sha256`` almacenado corresponde al contenido actual."""
        return bool(self.sha256) and self.sha256 == self.calcular_sha256()

    # ------------------------------------------------------------------ #
    # Consultas de conveniencia
    # ------------------------------------------------------------------ #
    def linea(self, concepto_id: str) -> LineaDelta | None:
        """Devuelve la línea de un concepto, si existe."""
        for linea in self.lineas:
            if linea.concepto_id == concepto_id:
                return linea
        return None

    def lineas_explicables(self) -> list[LineaDelta]:
        """Líneas con variación, ordenadas por impacto absoluto descendente."""
        return sorted(
            (linea for linea in self.lineas if linea.se_explica),
            key=lambda linea: (-abs(linea.delta_cent), linea.concepto_id),
        )

    def causa_dominante(self) -> CausaAgregada | None:
        """Causa con mayor impacto absoluto; guía la elección de plantilla y narrativa."""
        if not self.causas_agregadas:
            return None
        return max(
            self.causas_agregadas,
            key=lambda causa: (abs(causa.monto_cent), causa.etiqueta_cliente),
        )

    def firma_causal(self) -> str:
        """Firma para recuperar casuísticas: ``causas ordenadas + modalidad + signo(Δ)``."""
        causas = sorted({str(causa.causa) for causa in self.causas_agregadas if causa.causa})
        signo = "+" if self.delta_total_cent > 0 else ("-" if self.delta_total_cent < 0 else "0")
        return f"{'|'.join(causas) or 'SIN_CAUSA'}#{self.modalidad_renta}#{signo}"

    @property
    def total_a_pagar_cent(self) -> Centimos:
        """Total del periodo más la deuda anterior arrastrada."""
        return self.total_actual_cent + self.deuda_anterior_cent

    # ------------------------------------------------------------------ #
    # Anclaje numérico — base del verificador
    # ------------------------------------------------------------------ #
    def mapa_tokens(self) -> dict[str, list[str]]:
        """Todos los valores numéricos anclables, con la evidencia que los respalda.

        Devuelve ``{token: [fact_id, ...]}``. El ``fact_id`` es una ruta legible dentro
        del FactSet (``"linea:RENTA_PLAN_MOVIL.delta_cent"``), y es lo que se guarda en
        ``Gobernanza.citas`` y en el evento ``VERIFY`` de la auditoría: cada cifra del
        texto queda trazada a un campo concreto de este objeto.

        **Qué se ancla** (todo lo que el modelo puede llegar a escribir sin calcular):

        1. Totales del recibo: actual, previo, delta, deuda anterior, total a pagar.
        2. Cada línea: monto actual, monto previo y delta.
        3. Cada causa agregada: importe y participación en porcentaje.
        4. El invariante: residual, suma de deltas y delta total.
        5. Días: los del ciclo y los de prorrateo de cada línea.
        6. Cada tramo: días, tarifa mensual, monto prorrateado, fechas de inicio y de
           fin (la exclusiva y la inclusiva) y los números de día que aparecen en la
           etiqueta ("del **1** al **12** de julio").
        7. Cuotas de equipo financiado: número, total y monto ("cuota 3 de 18").
        8. Periodos, años y fechas de ciclo y de vencimiento.
        9. Confianzas, expresadas como porcentaje.

        De cada importe se ancla el valor **con signo** y su **valor absoluto**, porque
        el texto dice "bajó S/ 30.00" para un delta de −3000.

        **Qué NO se ancla aquí:** los resultados de la *álgebra permitida* (sumas,
        restas, diferencias de fechas, cocientes días/D, porcentajes y redondeo a
        céntimo). Esos los deriva el verificador a partir de este conjunto y los
        registra uno a uno en el log. Tampoco se anclan las cifras de documentos
        recuperados: el saneador las sustituye por marcadores antes del prompt.
        """
        mapa: defaultdict[str, list[str]] = defaultdict(list)

        def anotar(token: str, fact_id: str) -> None:
            if fact_id not in mapa[token]:
                mapa[token].append(fact_id)

        def anotar_monto(centimos: int | None, fact_id: str) -> None:
            if centimos is None:
                return
            anotar(token_monto(centimos), fact_id)
            anotar(token_monto(abs(centimos)), fact_id)

        def anotar_entero(valor: int | None, fact_id: str) -> None:
            if valor is None:
                return
            anotar(token_entero(valor), fact_id)

        def anotar_fecha(valor: date | None, fact_id: str) -> None:
            if valor is None:
                return
            anotar(token_fecha(valor), fact_id)
            anotar_entero(valor.day, f"{fact_id}.dia")
            anotar_entero(valor.year, f"{fact_id}.anio")

        # 1. Totales
        anotar_monto(self.total_actual_cent, "factset:total_actual_cent")
        anotar_monto(self.total_previo_cent, "factset:total_previo_cent")
        anotar_monto(self.delta_total_cent, "factset:delta_total_cent")
        anotar_monto(self.deuda_anterior_cent, "factset:deuda_anterior_cent")
        anotar_monto(self.total_a_pagar_cent, "factset:total_a_pagar_cent")

        # 4. Invariante
        anotar_monto(self.invariante.residual_cent, "invariante:residual_cent")
        anotar_monto(self.invariante.suma_deltas_cent, "invariante:suma_deltas_cent")
        anotar_monto(self.invariante.delta_total_cent, "invariante:delta_total_cent")

        # 5. Días de ciclo
        anotar_entero(self.dias_ciclo, "factset:dias_ciclo")

        # 8. Periodos y fechas del ciclo
        for nombre, periodo in (
            ("periodo_actual", self.periodo_actual),
            ("periodo_previo", self.periodo_previo),
        ):
            anotar(token_periodo(periodo), f"factset:{nombre}")
            anotar_entero(int(periodo[:4]), f"factset:{nombre}.anio")
        anotar_fecha(self.ciclo_inicio, "factset:ciclo_inicio")
        anotar_fecha(self.ciclo_fin, "factset:ciclo_fin")
        anotar_fecha(self.fecha_vencimiento, "factset:fecha_vencimiento")

        # 9. Confianza global
        anotar(token_porcentaje(round(self.confianza_global * 100, 2)), "factset:confianza_global")

        # 2, 5, 6, 7. Líneas, sus días, sus tramos y sus cuotas
        for linea in self.lineas:
            base = f"linea:{linea.concepto_id}"
            anotar_monto(linea.monto_actual_cent, f"{base}.monto_actual_cent")
            anotar_monto(linea.monto_previo_cent, f"{base}.monto_previo_cent")
            anotar_monto(linea.delta_cent, f"{base}.delta_cent")
            anotar_entero(linea.dias_prorrateo, f"{base}.dias_prorrateo")
            anotar_entero(linea.cuota_numero, f"{base}.cuota_numero")
            anotar_entero(linea.cuotas_totales, f"{base}.cuotas_totales")
            anotar(token_porcentaje(round(linea.confianza * 100, 2)), f"{base}.confianza")
            for indice, tramo in enumerate(linea.tramos or []):
                ref = f"tramo:{linea.concepto_id}#{indice}"
                anotar_entero(tramo.dias, f"{ref}.dias")
                anotar_monto(tramo.tarifa_mensual_cent, f"{ref}.tarifa_mensual_cent")
                anotar_monto(tramo.monto_prorrateado_cent, f"{ref}.monto_prorrateado_cent")
                anotar_monto(tramo.descuento_cent, f"{ref}.descuento_cent")
                anotar_fecha(tramo.inicio, f"{ref}.inicio")
                anotar_fecha(tramo.fin, f"{ref}.fin")
                anotar_fecha(tramo.fin_inclusivo, f"{ref}.fin_inclusivo")

        # 3. Causas agregadas
        for causa in self.causas_agregadas:
            ref = f"causa:{causa.causa or causa.causa_oficial or 'SIN_CAUSA'}"
            anotar_monto(causa.monto_cent, f"{ref}.monto_cent")
            anotar(token_porcentaje(causa.participacion_pct), f"{ref}.participacion")
            anotar(token_porcentaje(round(causa.confianza * 100, 2)), f"{ref}.confianza")

        # 7. Financiamientos
        for plan in self.financiamientos:
            ref = f"financiamiento:{plan.equipo}"
            anotar_monto(plan.principal_cent, f"{ref}.principal_cent")
            anotar_entero(plan.cuotas_totales, f"{ref}.cuotas_totales")
            for cuota in plan.cronograma:
                sub = f"{ref}.cuota{cuota.numero}"
                anotar_entero(cuota.numero, f"{sub}.numero")
                anotar_entero(cuota.de_total, f"{sub}.de_total")
                anotar_monto(cuota.monto_cent, f"{sub}.monto_cent")
                anotar_monto(cuota.saldo_final_cent, f"{sub}.saldo_final_cent")

        return {token: sorted(fuentes) for token, fuentes in sorted(mapa.items())}

    def tokens_permitidos(self) -> set[str]:
        """Conjunto de tokens numéricos anclables (``ALLOWED`` base del verificador).

        Es ``set(self.mapa_tokens())``. Cualquier cifra del texto generado que, tras
        normalizarse con ``token_monto`` / ``token_entero`` / ``token_porcentaje`` /
        ``token_fecha``, no pertenezca a este conjunto ni sea derivable por álgebra
        permitida, es una **alucinación numérica** y bloquea la respuesta.
        """
        return set(self.mapa_tokens())

    def fuentes_de(self, token: str) -> list[str]:
        """Devuelve los ``fact_id`` que respaldan un token (vacío si no está anclado)."""
        return self.mapa_tokens().get(token, [])

    def resumen_para_prompt(self) -> dict[str, Any]:
        """Proyección compacta del FactSet para inyectar en el prompt.

        Se omite todo lo que el modelo no necesita ver (identificadores internos,
        evidencia, hashes) y se conservan las cifras tal cual, en céntimos enteros.
        """
        return {
            "periodo_actual": self.periodo_actual,
            "periodo_previo": self.periodo_previo,
            "modalidad_renta": str(self.modalidad_renta),
            "dias_ciclo": self.dias_ciclo,
            "total_actual_cent": self.total_actual_cent,
            "total_previo_cent": self.total_previo_cent,
            "delta_total_cent": self.delta_total_cent,
            "deuda_anterior_cent": self.deuda_anterior_cent,
            "lineas": [
                {
                    "concepto_id": linea.concepto_id,
                    "nombre_comercial": linea.nombre_comercial,
                    "clase": str(linea.clase),
                    "delta_cent": linea.delta_cent,
                    "monto_actual_cent": linea.monto_actual_cent,
                    "monto_previo_cent": linea.monto_previo_cent,
                    "causa": str(linea.causa) if linea.causa else None,
                    # Va al prompt a propósito: decirle al modelo que el porqué NO está
                    # confirmado es la mejor defensa contra que se lo invente.
                    "causa_confirmada": linea.causa_confirmada,
                    "dias_prorrateo": linea.dias_prorrateo,
                    "cuota": (
                        f"{linea.cuota_numero} de {linea.cuotas_totales}"
                        if linea.cuota_numero and linea.cuotas_totales
                        else None
                    ),
                    "tramos": [
                        {
                            "etiqueta": tramo.etiqueta,
                            "dias": tramo.dias,
                            "tarifa_mensual_cent": tramo.tarifa_mensual_cent,
                            "monto_prorrateado_cent": tramo.monto_prorrateado_cent,
                            "estado": str(tramo.estado),
                        }
                        for tramo in (linea.tramos or [])
                    ],
                }
                for linea in self.lineas_explicables()
            ],
            "causas_agregadas": [
                {
                    "etiqueta_cliente": causa.etiqueta_cliente,
                    "causa": str(causa.causa) if causa.causa else None,
                    "monto_cent": causa.monto_cent,
                }
                for causa in self.causas_agregadas
            ],
            "beneficios_vigentes": list(self.beneficios_vigentes),
        }
