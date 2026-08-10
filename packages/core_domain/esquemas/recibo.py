"""Modelos del recibo: catálogo de conceptos, líneas, tramos y documento completo.

El recibo es un objeto **estructurado**: nunca se vectoriza ni se recupera por
similitud. Se consulta por clave (``cuenta_id`` + ``periodo``) y se compara línea a
línea. Lo que sí va a índice vectorial es el catálogo, las FAQs y las casuísticas.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from packages.core_domain.dinero import Centimos, prorratear
from packages.core_domain.enums import (
    CausaOficial,
    EstadoServicio,
    FamiliaConcepto,
    ModalidadRenta,
    TipoMovimiento,
)

__all__ = [
    "MESES_ES",
    "ConceptoCatalogo",
    "LineaRecibo",
    "Periodo",
    "Recibo",
    "Tramo",
    "etiqueta_rango_fechas",
]

#: Periodo de facturación en formato ``YYYY-MM``.
Periodo = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

MESES_ES: dict[int, str] = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "setiembre",  # grafía habitual en Perú
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}


def etiqueta_rango_fechas(inicio: date, fin_exclusivo: date) -> str:
    """Etiqueta legible de un rango ``[inicio, fin)`` en español de Perú.

    Ejemplos: ``"del 1 al 12 de julio"``, ``"del 28 de junio al 3 de julio"``,
    ``"el 5 de julio"``, ``"del 20 de diciembre de 2025 al 4 de enero de 2026"``.

    El extremo derecho del intervalo es **exclusivo** (es la convención de tramos de
    la especificación), pero la etiqueta habla del último día realmente incluido.
    """
    dias = (fin_exclusivo - inicio).days
    if dias <= 0:
        raise ValueError(f"rango vacío o invertido: [{inicio}, {fin_exclusivo})")
    ultimo = fin_exclusivo - timedelta(days=1)
    if dias == 1:
        return f"el {inicio.day} de {MESES_ES[inicio.month]}"
    if inicio.year != ultimo.year:
        return (
            f"del {inicio.day} de {MESES_ES[inicio.month]} de {inicio.year} "
            f"al {ultimo.day} de {MESES_ES[ultimo.month]} de {ultimo.year}"
        )
    if inicio.month != ultimo.month:
        return (
            f"del {inicio.day} de {MESES_ES[inicio.month]} "
            f"al {ultimo.day} de {MESES_ES[ultimo.month]}"
        )
    return f"del {inicio.day} al {ultimo.day} de {MESES_ES[inicio.month]}"


class ConceptoCatalogo(BaseModel):
    """Definición de un concepto facturable en lenguaje de cliente y en lenguaje técnico.

    Es el único corpus del recibo que se recupera: por clave (``concepto_id`` viene del
    FactSet) y, para preguntas sueltas, por similitud vectorial.
    """

    model_config = ConfigDict(extra="forbid")

    concepto_id: str = Field(description="Clave estable, en MAYUSCULAS_CON_GUION_BAJO")
    nombre_comercial: str = Field(description="Como aparece en el recibo del cliente")
    nombre_tecnico: str = Field(default="", description="Nombre interno de facturación")
    familia: FamiliaConcepto
    definicion_cliente: str = Field(description="Explicación simple, sin tecnicismos, de usted")
    definicion_tecnica: str = Field(default="", description="Definición para el asesor")
    prorrateable: bool = Field(default=False, description="Si admite cálculo por días")
    afecto_igv: bool = True
    causas_permitidas: list[TipoMovimiento] = Field(
        default_factory=list,
        description="Se rellena desde regla_concepto_causa al cargar rules.yaml",
    )
    causa_oficial: CausaOficial | None = Field(
        default=None, description="Causa de la ficha con la que se narra por defecto"
    )
    sinonimos: list[str] = Field(default_factory=list, description="Términos del cliente")
    ejemplo_variacion: str | None = Field(
        default=None, description="Ejemplo de por qué este concepto varía entre recibos"
    )
    visible_cliente: bool = True

    @property
    def es_credito(self) -> bool:
        """Verdadero si el concepto resta en el recibo (crédito o descuento)."""
        return self.familia in (FamiliaConcepto.CREDITO,)


class Tramo(BaseModel):
    """Fracción homogénea del ciclo: misma tarifa y mismo estado de servicio.

    La tabla de tramos **es** la explicación del prorrateo: "del 1 al 12 de julio el
    Plan A, del 13 al 30 el Plan B". Todo el modelo de cálculo de la sección 4.1 se
    reduce a partir el ciclo en tramos disjuntos y sumar ``P_j · len_j / D``.

    El intervalo es ``[inicio, fin)``: ``fin`` es **exclusivo**, de modo que los tramos
    encadenados cumplen ``tramo[i].fin == tramo[i+1].inicio`` y ``Σ dias == dias_ciclo``.
    """

    model_config = ConfigDict(extra="forbid")

    inicio: date
    fin: date = Field(description="Extremo derecho EXCLUSIVO del tramo")
    dias: int = Field(ge=0, description="(fin - inicio).days")
    tarifa_mensual_cent: Centimos = Field(description="Tarifa mensual vigente, con descuento ya aplicado")
    estado: EstadoServicio = EstadoServicio.ACTIVO
    facturable: bool = Field(default=True, description="Falso si no se cobra (p. ej. suspensión)")
    monto_prorrateado_cent: Centimos = Field(default=0, description="tarifa · dias / dias_ciclo, o 0")
    etiqueta: str = Field(description='Texto legible, p. ej. "del 1 al 12 de julio"')
    concepto_id: str | None = None
    plan: str | None = Field(default=None, description="Nombre comercial del plan vigente")
    descuento_cent: Centimos = Field(default=0, description="Descuento mensual ya incluido en la tarifa")

    @model_validator(mode="after")
    def _validar_coherencia(self) -> Self:
        esperados = (self.fin - self.inicio).days
        if esperados != self.dias:
            raise ValueError(
                f"tramo incoherente: dias={self.dias} pero (fin - inicio)={esperados}"
            )
        if self.dias < 0:
            raise ValueError("tramo con días negativos")
        return self

    @property
    def fin_inclusivo(self) -> date:
        """Último día realmente incluido en el tramo (``fin - 1 día``)."""
        return self.fin - timedelta(days=1)

    @classmethod
    def crear(
        cls,
        *,
        inicio: date,
        fin: date,
        tarifa_mensual_cent: Centimos,
        dias_ciclo: int,
        estado: EstadoServicio = EstadoServicio.ACTIVO,
        facturable: bool | None = None,
        concepto_id: str | None = None,
        plan: str | None = None,
        descuento_cent: Centimos = 0,
        etiqueta: str | None = None,
    ) -> Tramo:
        """Construye un tramo calculando días, monto prorrateado y etiqueta.

        Si ``facturable`` es ``None`` se deduce del estado (SUSPENDIDO -> no facturable);
        la política real la fija ``rules.yaml`` (``politica.cobro_en_suspension``) y el
        llamador debe pasarla explícitamente cuando difiera del valor por defecto.
        """
        dias = (fin - inicio).days
        se_factura = facturable if facturable is not None else estado is EstadoServicio.ACTIVO
        monto = prorratear(tarifa_mensual_cent, dias, dias_ciclo) if se_factura else 0
        return cls(
            inicio=inicio,
            fin=fin,
            dias=dias,
            tarifa_mensual_cent=tarifa_mensual_cent,
            estado=estado,
            facturable=se_factura,
            monto_prorrateado_cent=monto,
            etiqueta=etiqueta or etiqueta_rango_fechas(inicio, fin),
            concepto_id=concepto_id,
            plan=plan,
            descuento_cent=descuento_cent,
        )


class LineaRecibo(BaseModel):
    """Una línea del detalle del recibo. Todos los importes en céntimos enteros."""

    model_config = ConfigDict(extra="forbid")

    linea_id: int
    concepto_id: str
    nombre_comercial: str
    familia: FamiliaConcepto
    monto_cent: Centimos
    periodo: Periodo
    servicio_id: str | None = Field(default=None, description="Línea/servicio al que aplica")
    descripcion: str | None = None
    cantidad: int = 1
    afecto_igv: bool = True
    dias_prorrateo: int | None = Field(default=None, description="Días efectivamente cobrados")
    fecha_inicio: date | None = None
    fecha_fin: date | None = Field(default=None, description="Exclusiva, como en Tramo")
    cuota_numero: int | None = Field(default=None, description='Para "cuota 3 de 18"')
    cuotas_totales: int | None = None
    movimiento_id: int | None = Field(default=None, description="Orden de Amdocs que la originó")
    tramos: list[Tramo] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validar_cuotas(self) -> Self:
        if self.cuota_numero is not None and self.cuotas_totales is not None:
            if not 1 <= self.cuota_numero <= self.cuotas_totales:
                raise ValueError(
                    f"cuota fuera de rango: {self.cuota_numero} de {self.cuotas_totales}"
                )
        return self


class Recibo(BaseModel):
    """Recibo completo de un periodo, tal como lo expone BrainyBill.

    Invariante estructural: ``total_cent`` es exactamente la suma de las líneas del
    periodo (IGV incluido como línea de familia IMPUESTO). La deuda anterior **no**
    forma parte de ``total_cent``; se suma aparte en ``total_a_pagar_cent`` porque no
    es un cargo del periodo y no debe contaminar el delta entre recibos.
    """

    model_config = ConfigDict(extra="forbid")

    recibo_id: str
    cuenta_id: str = Field(description="Identificador ficticio tokenizado; jamás DNI ni teléfono")
    periodo: Periodo
    modalidad_renta: ModalidadRenta
    ciclo_inicio: date
    ciclo_fin: date = Field(description="Extremo derecho EXCLUSIVO del ciclo")
    dias_ciclo: int = Field(gt=0)
    fecha_emision: date
    fecha_vencimiento: date
    lineas: list[LineaRecibo]
    total_cent: Centimos
    deuda_anterior_cent: Centimos = 0
    moneda: Literal["PEN"] = "PEN"
    estado_servicio: EstadoServicio = EstadoServicio.ACTIVO
    plan_vigente: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validar_conciliacion(self) -> Self:
        esperados = (self.ciclo_fin - self.ciclo_inicio).days
        if esperados != self.dias_ciclo:
            raise ValueError(
                f"ciclo incoherente: dias_ciclo={self.dias_ciclo} pero el rango tiene {esperados}"
            )
        suma = sum(linea.monto_cent for linea in self.lineas)
        if suma != self.total_cent:
            raise ValueError(
                f"recibo {self.recibo_id}: la suma de líneas ({suma}) no coincide con "
                f"total_cent ({self.total_cent}); descuadre de {suma - self.total_cent} céntimos"
            )
        return self

    @property
    def total_a_pagar_cent(self) -> Centimos:
        """Total del periodo más la deuda anterior arrastrada."""
        return self.total_cent + self.deuda_anterior_cent

    def suma_lineas_cent(self) -> Centimos:
        """Suma de todas las líneas del recibo (igual a ``total_cent`` por invariante)."""
        return sum(linea.monto_cent for linea in self.lineas)

    def agrupar_por_concepto(self) -> dict[str, Centimos]:
        """Agrupa sumando por ``concepto_id``. Es la entrada del diff (sección 4.6)."""
        agregado: defaultdict[str, int] = defaultdict(int)
        for linea in self.lineas:
            agregado[linea.concepto_id] += linea.monto_cent
        return dict(agregado)

    def lineas_de(self, concepto_id: str) -> list[LineaRecibo]:
        """Todas las líneas de un concepto (puede haber varias por servicio)."""
        return [linea for linea in self.lineas if linea.concepto_id == concepto_id]
