"""Movimientos del historial de órdenes (Amdocs CRM) y financiamiento de equipos.

Un movimiento es lo único que puede *causar* una variación entre recibos. La
atribución (sección 4.7) empareja líneas con movimientos del ciclo; sin movimiento
candidato la causa queda en ``None`` con confianza baja, nunca se inventa.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.core_domain.dinero import Centimos
from packages.core_domain.enums import EstadoServicio, TipoMovimiento

__all__ = [
    "CuotaFinanciamiento",
    "DetalleAltaEquipoFinanciado",
    "DetalleAltaPaquete",
    "DetalleCambioPlan",
    "DetalleFinDescuento",
    "DetalleNota",
    "DetalleReconexion",
    "DetalleSuspension",
    "MovementEvent",
    "PlanFinanciamiento",
    "MODELOS_DETALLE",
]


# --------------------------------------------------------------------------- #
# Cargas útiles tipadas de `MovementEvent.detalle`
# --------------------------------------------------------------------------- #
class DetalleCambioPlan(BaseModel):
    """Detalle de ``CAMBIO_PLAN``."""

    model_config = ConfigDict(extra="allow")

    plan_anterior: str
    plan_nuevo: str
    tarifa_anterior_cent: Centimos
    tarifa_nueva_cent: Centimos
    servicio_id: str | None = None


class DetalleSuspension(BaseModel):
    """Detalle de ``SUSPENSION`` (corte por deuda u otra causa)."""

    model_config = ConfigDict(extra="allow")

    motivo: str = "MOROSIDAD"
    estado: EstadoServicio = EstadoServicio.SUSPENDIDO
    fecha_fin_prevista: date | None = None
    servicio_id: str | None = None


class DetalleReconexion(BaseModel):
    """Detalle de ``RECONEXION``. El cargo es fijo y se cobra una sola vez."""

    model_config = ConfigDict(extra="allow")

    cargo_cent: Centimos
    dias_suspendido: int = 0
    servicio_id: str | None = None


class DetalleAltaEquipoFinanciado(BaseModel):
    """Detalle de ``ALTA_EQUIPO_FINANCIADO`` (sistema francés, nunca se prorratea)."""

    model_config = ConfigDict(extra="allow")

    equipo: str
    principal_cent: Centimos = Field(description="Capital financiado K")
    cuotas_totales: int = Field(gt=0, description="n")
    tasa_mensual_bp: int = Field(default=0, ge=0, description="i en puntos básicos (0 = sin interés)")
    cuota_cent: Centimos | None = Field(default=None, description="A, si ya está calculada")


class DetalleAltaPaquete(BaseModel):
    """Detalle de ``ALTA_PAQUETE`` (datos, roaming, TV premium…)."""

    model_config = ConfigDict(extra="allow")

    paquete_id: str
    nombre: str
    monto_cent: Centimos
    recurrente: bool = False


class DetalleFinDescuento(BaseModel):
    """Detalle de ``FIN_DESCUENTO`` (promoción vencida)."""

    model_config = ConfigDict(extra="allow")

    promocion_id: str
    nombre: str
    descuento_cent: Centimos = Field(description="Importe mensual que deja de aplicarse (positivo)")
    meses_vigencia: int | None = None


class DetalleNota(BaseModel):
    """Detalle de ``NOTA_CREDITO`` o ``NOTA_DEBITO``."""

    model_config = ConfigDict(extra="allow")

    documento: str
    monto_cent: Centimos = Field(description="Positivo en débito, negativo en crédito")
    motivo: str


#: Modelo de detalle esperado por tipo de movimiento. Los tipos ausentes usan el dict crudo.
MODELOS_DETALLE: dict[TipoMovimiento, type[BaseModel]] = {
    TipoMovimiento.CAMBIO_PLAN: DetalleCambioPlan,
    TipoMovimiento.SUSPENSION: DetalleSuspension,
    TipoMovimiento.RECONEXION: DetalleReconexion,
    TipoMovimiento.ALTA_EQUIPO_FINANCIADO: DetalleAltaEquipoFinanciado,
    TipoMovimiento.ALTA_PAQUETE: DetalleAltaPaquete,
    TipoMovimiento.FIN_DESCUENTO: DetalleFinDescuento,
    TipoMovimiento.NOTA_CREDITO: DetalleNota,
    TipoMovimiento.NOTA_DEBITO: DetalleNota,
}


class MovementEvent(BaseModel):
    """Evento del historial de órdenes tal como lo entrega Amdocs.

    ``detalle`` se mantiene como ``dict`` para no acoplarse al esquema real del CRM;
    ``detalle_tipado()`` lo valida contra el modelo que corresponde al tipo.
    """

    model_config = ConfigDict(extra="forbid")

    movimiento_id: int
    cuenta_id: str
    tipo: TipoMovimiento
    ocurrido_en: datetime
    detalle: dict[str, Any] = Field(default_factory=dict)
    canal: str | None = None
    servicio_id: str | None = None

    @property
    def fecha(self) -> date:
        """Fecha (sin hora) del movimiento; es la que se usa para cortar tramos."""
        return self.ocurrido_en.date()

    def detalle_tipado(self) -> BaseModel | dict[str, Any]:
        """Valida ``detalle`` contra el modelo del tipo y lo devuelve.

        Si el tipo no tiene modelo asociado devuelve el ``dict`` tal cual, de modo que
        el llamador nunca se queda sin datos.
        """
        modelo = MODELOS_DETALLE.get(self.tipo)
        if modelo is None:
            return self.detalle
        return modelo.model_validate(self.detalle)


# --------------------------------------------------------------------------- #
# Financiamiento de equipos (sistema francés)
# --------------------------------------------------------------------------- #
class CuotaFinanciamiento(BaseModel):
    """Una cuota del cronograma francés. Clave explicativa: "cuota 3 de 18"."""

    model_config = ConfigDict(extra="forbid")

    numero: int = Field(ge=1)
    de_total: int = Field(ge=1)
    monto_cent: Centimos
    interes_cent: Centimos = 0
    amortizacion_cent: Centimos = 0
    saldo_inicial_cent: Centimos = 0
    saldo_final_cent: Centimos = 0

    @model_validator(mode="after")
    def _validar_numero(self) -> Self:
        if self.numero > self.de_total:
            raise ValueError(f"cuota {self.numero} de {self.de_total} es imposible")
        return self

    @property
    def etiqueta(self) -> str:
        """Texto listo para la explicación: ``"cuota 3 de 18"``."""
        return f"cuota {self.numero} de {self.de_total}"

    @property
    def es_ultima(self) -> bool:
        """La última cuota absorbe el céntimo residual y dispara el efecto efervescente."""
        return self.numero == self.de_total


class PlanFinanciamiento(BaseModel):
    """Cronograma completo de un equipo financiado.

    Invariante: el saldo final de la última cuota es exactamente 0 y la suma de
    amortizaciones es igual al principal.
    """

    model_config = ConfigDict(extra="forbid")

    equipo: str
    principal_cent: Centimos
    cuotas_totales: int = Field(gt=0)
    tasa_mensual_bp: int = Field(default=0, ge=0)
    cronograma: list[CuotaFinanciamiento] = Field(default_factory=list)
    movimiento_id: int | None = None

    @model_validator(mode="after")
    def _validar_cronograma(self) -> Self:
        if not self.cronograma:
            return self
        if len(self.cronograma) != self.cuotas_totales:
            raise ValueError(
                f"cronograma con {len(self.cronograma)} cuotas pero cuotas_totales="
                f"{self.cuotas_totales}"
            )
        if self.cronograma[-1].saldo_final_cent != 0:
            raise ValueError(
                f"el saldo final debe ser 0 y es {self.cronograma[-1].saldo_final_cent}"
            )
        amortizado = sum(cuota.amortizacion_cent for cuota in self.cronograma)
        if amortizado != self.principal_cent:
            raise ValueError(
                f"la amortización total ({amortizado}) no cierra con el principal "
                f"({self.principal_cent})"
            )
        return self

    def cuota(self, numero: int) -> CuotaFinanciamiento | None:
        """Devuelve la cuota número ``numero`` (1-indexada) o ``None``."""
        for cuota in self.cronograma:
            if cuota.numero == numero:
                return cuota
        return None
