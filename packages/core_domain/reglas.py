"""Carga y validación de ``rules.yaml``: las reglas de negocio versionadas.

El motor determinístico no tiene constantes de negocio en el código. Todo lo que un
analista de facturación podría discutir (si se cobra durante la suspensión, qué
convención de días se usa, qué movimientos pueden explicar cada concepto, dónde está
el umbral de derivación) vive en ``db/reglas/rules.yaml`` y viaja en cada respuesta
dentro de ``rules_version``: dos ejecuciones con reglas distintas son distinguibles.

Uso típico::

    from packages.core_domain.reglas import cargar_reglas

    reglas = cargar_reglas()
    if reglas.es_prorrateable("RENTA_PLAN_MOVIL"):
        ...

El objeto devuelto está cacheado y **se comparte entre peticiones**: no lo mute.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from packages.core_domain.dinero import Centimos
from packages.core_domain.enums import (
    CausaOficial,
    ClaseDelta,
    ConvencionProrrateo,
    FamiliaConcepto,
    MotivoDerivacion,
    TipoMovimiento,
    etiqueta_causa_oficial,
)
from packages.core_domain.esquemas.recibo import ConceptoCatalogo

__all__ = [
    "RUTA_RELATIVA_REGLAS",
    "ConfianzaAtribucion",
    "ConfiguracionCrossSelling",
    "ConfiguracionReglas",
    "EfectoEfervescente",
    "PesosIncomprension",
    "PoliticaCalculo",
    "ReglaCrossSelling",
    "UmbralesIncomprension",
    "cargar_reglas",
    "limpiar_cache_reglas",
    "raiz_proyecto",
    "ruta_reglas_por_defecto",
]

_LOG = logging.getLogger(__name__)

#: Ruta del fichero de reglas relativa a la raíz del proyecto.
RUTA_RELATIVA_REGLAS = Path("db") / "reglas" / "rules.yaml"

#: Variable de entorno para apuntar a otro fichero de reglas (tests, escenarios).
VAR_ENTORNO_RUTA = "RULES_PATH"


# --------------------------------------------------------------------------- #
# Secciones del fichero
# --------------------------------------------------------------------------- #
class PoliticaCalculo(BaseModel):
    """Parámetros que deciden **cómo se calcula** el recibo."""

    model_config = ConfigDict(extra="forbid")

    cobro_en_suspension: bool = Field(
        default=False,
        description="[POR VALIDAR] Si es False, los días suspendidos no se cobran",
    )
    convencion_prorrateo: ConvencionProrrateo = Field(
        default=ConvencionProrrateo.ACTUAL, description="[POR VALIDAR] actual | 30_360"
    )
    dias_base_30_360: int = Field(default=30, gt=0)
    tolerancia_residual_cent: int = Field(default=1, ge=0)
    igv_bp: int = Field(default=1800, ge=0, description="IGV en puntos básicos (1800 = 18 %)")
    metodo_redondeo: str = "mayor_resto"
    cargo_reconexion_cent: Centimos = 2500
    descuento_alta_solo_cargo_fijo: bool = Field(
        default=True,
        description=(
            "[CONFIRMADO-OFICIAL] El descuento por alta nueva o portabilidad aplica solo "
            "sobre el cargo fijo del plan, nunca sobre SVAs, paquetes ni financiamiento "
            "de equipos (vídeo oficial «Alta y porta», 04:06)"
        ),
    )
    ventana_descuento_alta_dias: int = Field(
        default=90,
        ge=0,
        description=(
            "[CONFIRMADO-OFICIAL] La promoción se agota en DÍAS, no en meses naturales, "
            "por lo que el recibo que la agota es mixto (vídeo «Alta y porta», 02:25)"
        ),
    )
    prorratear_financiamiento: bool = Field(
        default=False, description="Las cuotas de equipo NUNCA se prorratean"
    )
    dias_gracia_suspension: int = 15


class ConfianzaAtribucion(BaseModel):
    """Confianzas fijas de la atribución de causa (sección 4.7)."""

    model_config = ConfigDict(extra="forbid")

    causa_unica: float = Field(default=0.98, ge=0.0, le=1.0)
    sin_candidato: float = Field(default=0.30, ge=0.0, le=1.0)
    multiples_candidatos: float = Field(default=0.65, ge=0.0, le=1.0)
    tope_prorrateo_inconsistente: float = Field(default=0.50, ge=0.0, le=1.0)
    tolerancia_prorrateo_cent: int = Field(default=1, ge=0)
    minima_para_explicar: float = Field(default=0.35, ge=0.0, le=1.0)
    regla_concepto: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="Causa fijada por preferencia_causa sin movimiento del CRM que la respalde",
    )


class PesosIncomprension(BaseModel):
    """Pesos del score de incomprensión. Deben sumar 1."""

    model_config = ConfigDict(extra="forbid")

    w1: float = Field(default=0.40, ge=0.0, le=1.0, description="Cobertura del delta explicado")
    w2: float = Field(default=0.25, ge=0.0, le=1.0, description="Unicidad de causa")
    w3: float = Field(default=0.20, ge=0.0, le=1.0, description="Repregunta")
    w6: float = Field(default=0.15, ge=0.0, le=1.0, description="Turnos sin progreso")

    @model_validator(mode="after")
    def _validar_suma(self) -> Self:
        total = self.w1 + self.w2 + self.w3 + self.w6
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"los pesos del score deben sumar 1 y suman {total}")
        return self


class UmbralesIncomprension(BaseModel):
    """Umbrales de derivación a asesor humano (sección 4.8)."""

    model_config = ConfigDict(extra="forbid")

    tau_alto: float = Field(default=0.65, ge=0.0, le=1.0, description="U > tau_alto -> derivar")
    tau_bajo: float = Field(default=0.35, ge=0.0, le=1.0)
    pesos: PesosIncomprension = Field(default_factory=PesosIncomprension)
    similitud_repregunta: float = Field(default=0.80, ge=0.0, le=1.0)
    max_turnos_sin_progreso: int = Field(default=2, ge=1)
    histeresis: bool = Field(default=True, description="Una vez derivado, no se vuelve atrás")
    reglas_duras: list[MotivoDerivacion] = Field(default_factory=list)
    intenciones_regulatorias: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validar_orden(self) -> Self:
        if self.tau_bajo > self.tau_alto:
            raise ValueError(f"tau_bajo ({self.tau_bajo}) no puede superar a tau_alto ({self.tau_alto})")
        return self


class ReglaCrossSelling(BaseModel):
    """Regla de negocio explícita que habilita una oferta (nunca se ofrece sin una)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    descripcion: str = ""
    requiere_causas: list[TipoMovimiento] = Field(default_factory=list)
    requiere_conceptos: list[str] = Field(default_factory=list)


class ConfiguracionCrossSelling(BaseModel):
    """Cross-selling **restrictivo**: doble condición, literal de la ficha.

    Se activa *"única y exclusivamente si el modelo clasifica la consulta original como
    resuelta positivamente y existe una regla de negocio explícita que lo habilite"*.
    """

    model_config = ConfigDict(extra="forbid")

    habilitado: bool = True
    requiere_consulta_resuelta: bool = True
    requiere_regla_explicita: bool = True
    confianza_minima: float = Field(default=0.90, ge=0.0, le=1.0)
    prohibido_si_derivacion: bool = True
    prohibido_si_delta_negativo: bool = False
    reglas_explicitas: list[ReglaCrossSelling] = Field(default_factory=list)


class EfectoEfervescente(BaseModel):
    """Cierre recordando beneficios que el cliente YA tiene, sin presentarlos como nuevos."""

    model_config = ConfigDict(extra="forbid")

    habilitado: bool = True
    maximo_beneficios: int = Field(default=2, ge=0)
    frase_apertura: str = "Recuerde que su plan ya incluye"


# --------------------------------------------------------------------------- #
# Configuración completa
# --------------------------------------------------------------------------- #
class ConfiguracionCiclos(BaseModel):
    """Calendario de facturación: cuándo cierra un ciclo y cuándo vence su recibo.

    El ciclo **nombra el día de cierre** y la facturación reanuda al día siguiente:
    el ciclo 5 cierra el 5 y arranca el 6 (vídeo oficial «Planta», 01:33).

    ``vencimiento_por_ciclo`` no se dedujo de esa regla, porque no hay ninguna
    fórmula que la genere: se extrajo observando el dataset del desafío, donde cada
    ciclo presenta un único día de vencimiento. El ciclo 5 → día 21 coincide con el
    vídeo, y esa coincidencia es lo que da crédito a las otras ocho filas.
    """

    model_config = ConfigDict(extra="forbid")

    vencimiento_por_ciclo: dict[int, int] = Field(
        default_factory=dict,
        description="día de cierre del ciclo -> día del mes en que vence el recibo",
    )
    dias_aviso_antes_de_vencer: int = Field(
        default=10,
        ge=0,
        description="El recibo se envía unos 10 días antes de vencer («Planta», 01:39)",
    )

    def vence_el_dia(self, ciclo: int) -> int | None:
        """Día del mes en que vence el recibo de ``ciclo``, o ``None`` si no se conoce.

        Devuelve ``None`` en vez de inventar un valor: un ciclo no observado es un
        dato que falta, y adivinarlo pondría una fecha falsa delante del cliente.
        """
        return self.vencimiento_por_ciclo.get(ciclo)


class ConfiguracionReglas(BaseModel):
    """Contenido validado de ``rules.yaml``.

    El catálogo hereda ``causas_permitidas`` de ``regla_concepto_causa``: esa tabla es
    la única fuente de verdad de qué movimiento puede explicar qué concepto.
    """

    model_config = ConfigDict(extra="forbid")

    rules_version: str
    descripcion: str = ""
    politica: PoliticaCalculo = Field(default_factory=PoliticaCalculo)
    ciclos: ConfiguracionCiclos = Field(default_factory=ConfiguracionCiclos)
    confianza: ConfianzaAtribucion = Field(default_factory=ConfianzaAtribucion)
    umbrales_incomprension: UmbralesIncomprension = Field(default_factory=UmbralesIncomprension)
    cross_selling: ConfiguracionCrossSelling = Field(default_factory=ConfiguracionCrossSelling)
    efecto_efervescente: EfectoEfervescente = Field(default_factory=EfectoEfervescente)
    regla_concepto_causa: dict[str, list[TipoMovimiento]] = Field(default_factory=dict)
    preferencia_causa: dict[str, dict[ClaseDelta, TipoMovimiento]] = Field(
        default_factory=dict,
        description="concepto_id -> {clase de variación: causa que gana por regla de concepto}",
    )
    catalogo: list[ConceptoCatalogo] = Field(default_factory=list)
    ruta_origen: str | None = Field(default=None, description="Fichero del que se cargó")

    _indice: dict[str, ConceptoCatalogo] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _indexar_y_propagar(self) -> Self:
        indice: dict[str, ConceptoCatalogo] = {}
        for concepto in self.catalogo:
            if concepto.concepto_id in indice:
                raise ValueError(f"concepto duplicado en el catálogo: {concepto.concepto_id}")
            indice[concepto.concepto_id] = concepto

        desconocidos = sorted(set(self.regla_concepto_causa) - set(indice))
        if desconocidos:
            raise ValueError(
                "regla_concepto_causa referencia conceptos que no están en el catálogo: "
                + ", ".join(desconocidos)
            )

        for concepto_id, causas in self.regla_concepto_causa.items():
            indice[concepto_id].causas_permitidas = list(causas)

        # `preferencia_causa` no puede inventar causas: solo prioriza entre las que
        # `regla_concepto_causa` ya autoriza. Si no, una preferencia mal escrita
        # atribuiría en silencio un movimiento imposible para ese concepto.
        for concepto_id, preferencias in self.preferencia_causa.items():
            if concepto_id not in indice:
                raise ValueError(
                    f"preferencia_causa referencia un concepto fuera del catálogo: {concepto_id}"
                )
            permitidas = set(self.regla_concepto_causa.get(concepto_id, []))
            for clase, causa in preferencias.items():
                if causa not in permitidas:
                    raise ValueError(
                        f"preferencia_causa[{concepto_id}][{clase}] = {causa} no está en "
                        f"regla_concepto_causa[{concepto_id}] ({sorted(str(c) for c in permitidas)})"
                    )

        self._indice = indice
        return self

    # ------------------------------------------------------------------ #
    # Consultas del catálogo
    # ------------------------------------------------------------------ #
    def concepto(self, concepto_id: str) -> ConceptoCatalogo | None:
        """Devuelve la ficha del concepto, o ``None`` si no está catalogado."""
        return self._indice.get(concepto_id)

    def existe_concepto(self, concepto_id: str) -> bool:
        """Un concepto fuera de catálogo es una **regla dura de derivación**."""
        return concepto_id in self._indice

    def concepto_ids(self) -> set[str]:
        """Todos los ``concepto_id`` del catálogo."""
        return set(self._indice)

    def conceptos_por_familia(self, familia: FamiliaConcepto) -> list[ConceptoCatalogo]:
        """Conceptos de una familia, en el orden del fichero."""
        return [concepto for concepto in self.catalogo if concepto.familia is familia]

    def causas_permitidas(self, concepto_id: str) -> list[TipoMovimiento]:
        """Movimientos que pueden explicar un concepto (vacío si no hay regla)."""
        return list(self.regla_concepto_causa.get(concepto_id, []))

    def permite_causa(self, concepto_id: str, causa: TipoMovimiento) -> bool:
        """Comprueba si un movimiento puede atribuirse a un concepto."""
        return causa in self.regla_concepto_causa.get(concepto_id, [])

    def indice_preferencia(self, concepto_id: str, causa: TipoMovimiento) -> int:
        """Posición de una causa en ``regla_concepto_causa``: **menor es más preferida**.

        El orden de la lista deja de ser decorativo y pasa a ser la prioridad de la
        atribución cuando hay varios movimientos candidatos en el mismo ciclo. Las
        causas no listadas van al final.
        """
        permitidas = self.regla_concepto_causa.get(concepto_id, [])
        try:
            return permitidas.index(causa)
        except ValueError:
            return len(permitidas)

    def causa_preferida(
        self, concepto_id: str, clase: ClaseDelta | str | None
    ) -> TipoMovimiento | None:
        """Causa que gana **por regla de concepto** para esa clase de variación.

        Es la tabla ``preferencia_causa`` de ``rules.yaml``. Devuelve ``None`` cuando el
        concepto no declara preferencia para esa clase, que es el caso normal: entonces
        la causa la deciden los movimientos de la ventana del ciclo.

        La diferencia con ``causas_permitidas`` es sustancial: aquí la causa **no
        necesita** un movimiento del CRM que la respalde. Un descuento promocional que
        desaparece es una promoción terminada aunque nadie haya emitido la orden.
        """
        if clase is None:
            return None
        preferencias = self.preferencia_causa.get(concepto_id)
        if not preferencias:
            return None
        try:
            clave = ClaseDelta(str(clase))
        except ValueError:  # pragma: no cover - ClaseDelta ya valida en el modelo
            return None
        return preferencias.get(clave)

    def es_prorrateable(self, concepto_id: str) -> bool:
        """Si el concepto admite cálculo por días. El financiamiento nunca lo admite."""
        concepto = self.concepto(concepto_id)
        if concepto is None:
            return False
        if concepto.familia is FamiliaConcepto.FINANCIAMIENTO:
            return self.politica.prorratear_financiamiento
        return concepto.prorrateable

    def es_afecto_igv(self, concepto_id: str) -> bool:
        """Si el concepto entra en la base afecta al IGV."""
        concepto = self.concepto(concepto_id)
        return True if concepto is None else concepto.afecto_igv

    def familia(self, concepto_id: str) -> FamiliaConcepto | None:
        """Familia contable del concepto."""
        concepto = self.concepto(concepto_id)
        return concepto.familia if concepto else None

    def causa_oficial(
        self, concepto_id: str, causa: TipoMovimiento | None = None
    ) -> CausaOficial | None:
        """Causa oficial de la ficha para un concepto y, opcionalmente, un movimiento.

        El movimiento manda cuando existe: es información del CRM. Si no hay
        movimiento se usa la causa por defecto del catálogo.
        """
        if causa is not None:
            from packages.core_domain.enums import causa_oficial_de

            oficial = causa_oficial_de(causa)
            if oficial is not None:
                return oficial
        concepto = self.concepto(concepto_id)
        return concepto.causa_oficial if concepto else None

    def etiqueta_cliente(
        self, concepto_id: str, causa: TipoMovimiento | None = None
    ) -> str:
        """Etiqueta en lenguaje de cliente de la causa asociada a un concepto."""
        return etiqueta_causa_oficial(self.causa_oficial(concepto_id, causa))

    # ------------------------------------------------------------------ #
    # Política de cálculo
    # ------------------------------------------------------------------ #
    def dias_ciclo_efectivos(self, dias_reales: int) -> int:
        """Días que usa el prorrateo según la convención configurada.

        Con ``actual`` son los días reales del ciclo; con ``30_360``, siempre 30.
        """
        if self.politica.convencion_prorrateo is ConvencionProrrateo.TREINTA_360:
            return self.politica.dias_base_30_360
        return dias_reales

    def tramo_es_facturable(self, suspendido: bool) -> bool:
        """Aplica ``politica.cobro_en_suspension`` a un tramo."""
        if not suspendido:
            return True
        return self.politica.cobro_en_suspension


# --------------------------------------------------------------------------- #
# Carga
# --------------------------------------------------------------------------- #
def raiz_proyecto() -> Path:
    """Localiza la raíz del repositorio subiendo hasta encontrar ``pyproject.toml``."""
    actual = Path(__file__).resolve()
    for candidato in actual.parents:
        if (candidato / "pyproject.toml").is_file():
            return candidato
    return actual.parents[2]


def ruta_reglas_por_defecto() -> Path:
    """Ruta de ``rules.yaml``: ``$RULES_PATH`` si está definida, si no ``db/reglas/rules.yaml``."""
    desde_entorno = os.getenv(VAR_ENTORNO_RUTA)
    if desde_entorno:
        return Path(desde_entorno)
    return raiz_proyecto() / RUTA_RELATIVA_REGLAS


def _leer_yaml(ruta: Path) -> dict[str, Any]:
    """Lee el YAML y garantiza que la raíz es un diccionario."""
    if not ruta.is_file():
        raise FileNotFoundError(f"no se encontró el fichero de reglas: {ruta}")
    with ruta.open("r", encoding="utf-8") as fichero:
        datos = yaml.safe_load(fichero)
    if not isinstance(datos, dict):
        raise ValueError(f"el fichero de reglas no contiene un mapa en la raíz: {ruta}")
    return datos


@lru_cache(maxsize=8)
def _cargar_cacheado(
    ruta: str, cobro_en_suspension: str | None, convencion: str | None
) -> ConfiguracionReglas:
    """Carga efectiva; la cachea la clave (ruta + overrides de entorno)."""
    camino = Path(ruta)
    datos = _leer_yaml(camino)
    datos["ruta_origen"] = str(camino)

    politica = datos.setdefault("politica", {})
    if cobro_en_suspension is not None:
        politica["cobro_en_suspension"] = cobro_en_suspension.strip().lower() in {
            "1",
            "true",
            "si",
            "sí",
            "yes",
        }
    if convencion is not None and convencion.strip():
        politica["convencion_prorrateo"] = convencion.strip()

    reglas = ConfiguracionReglas.model_validate(datos)

    version_entorno = os.getenv("RULES_VERSION")
    if version_entorno and version_entorno != reglas.rules_version:
        _LOG.warning(
            "RULES_VERSION del entorno (%s) no coincide con rules.yaml (%s); manda el fichero",
            version_entorno,
            reglas.rules_version,
        )
    return reglas


def cargar_reglas(
    ruta: str | Path | None = None, aplicar_entorno: bool = True
) -> ConfiguracionReglas:
    """Carga (y cachea) las reglas de negocio.

    Args:
        ruta: fichero YAML alternativo. Por defecto ``db/reglas/rules.yaml`` o
            ``$RULES_PATH``.
        aplicar_entorno: si es ``True``, las variables ``COBRO_EN_SUSPENSION`` y
            ``CONVENCION_PRORRATEO`` sobrescriben la política del fichero (son los
            dos parámetros marcados **[POR VALIDAR]** en la especificación).

    Returns:
        La configuración validada. **Es un objeto compartido: no lo mute.**
    """
    camino = Path(ruta) if ruta is not None else ruta_reglas_por_defecto()
    cobro = os.getenv("COBRO_EN_SUSPENSION") if aplicar_entorno else None
    convencion = os.getenv("CONVENCION_PRORRATEO") if aplicar_entorno else None
    return _cargar_cacheado(str(camino.resolve()), cobro, convencion)


def limpiar_cache_reglas() -> None:
    """Vacía la caché de reglas (necesario en tests que tocan el entorno o el fichero)."""
    _cargar_cacheado.cache_clear()
