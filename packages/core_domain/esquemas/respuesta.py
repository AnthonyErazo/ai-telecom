"""Respuesta canal-agnóstica: la misma explicación sirve a App, Bot Lucía y WhatsApp.

El backend nunca devuelve HTML ni markdown de un canal concreto: devuelve **bloques**
tipados que cada canal renderiza a su manera (la App puede pintar el bloque ``puente``
como un gráfico de cascada; WhatsApp lo degrada a texto).

La propiedad :attr:`RespuestaCanalAgnostica.texto` concatena todo lo legible de la
respuesta y es exactamente lo que audita el verificador numérico: si una cifra aparece
en cualquier bloque, aparece en ``texto``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import Centimos, formatear_soles
from packages.core_domain.enums import (
    AccionSiguiente,
    Canal,
    EstadoAsercion,
    ModoGeneracion,
    MotivoDerivacion,
    NivelAseguramiento,
    Verbosidad,
)

__all__ = [
    "Accion",
    "Asercion",
    "BarraPuente",
    "Bloque",
    "BloqueAviso",
    "BloqueKV",
    "BloquePuente",
    "BloqueTabla",
    "BloqueTexto",
    "Cita",
    "Derivacion",
    "Gobernanza",
    "ItemEvidencia",
    "ItemKV",
    "PeticionDerivacion",
    "PeticionExplicacion",
    "ResumenRecibo",
    "RespuestaCanalAgnostica",
    "RespuestaError",
]


# --------------------------------------------------------------------------- #
# Bloques (unión discriminada por `tipo`)
# --------------------------------------------------------------------------- #
class _BloqueBase(BaseModel):
    """Base común de los bloques. No se instancia directamente."""

    model_config = ConfigDict(extra="forbid")

    titulo: str | None = None
    fact_ids: list[str] = Field(
        default_factory=list, description="Campos del FactSet que respaldan el bloque"
    )

    def a_texto(self) -> str:  # pragma: no cover - lo implementa cada subclase
        """Render en texto plano del bloque (lo que audita el verificador)."""
        raise NotImplementedError


class BloqueTexto(_BloqueBase):
    """Párrafo en lenguaje natural."""

    tipo: Literal["texto"] = "texto"
    texto: str
    enfasis: bool = False

    def a_texto(self) -> str:
        """Devuelve el título (si lo hay) y el párrafo."""
        return f"{self.titulo}\n{self.texto}" if self.titulo else self.texto


class ItemKV(BaseModel):
    """Par clave/valor de un bloque ``kv``."""

    model_config = ConfigDict(extra="forbid")

    clave: str
    valor: str
    monto_cent: Centimos | None = Field(
        default=None, description="Si el valor es monetario, su importe exacto en céntimos"
    )
    fact_id: str | None = None


class BloqueKV(_BloqueBase):
    """Lista de pares clave/valor: el desglose corto que cabe en un chat."""

    tipo: Literal["kv"] = "kv"
    items: list[ItemKV] = Field(default_factory=list)

    def a_texto(self) -> str:
        """Una línea por par, con el valor tal como lo verá el cliente."""
        lineas = [f"{item.clave}: {item.valor}" for item in self.items]
        return "\n".join(([self.titulo] if self.titulo else []) + lineas)


class BarraPuente(BaseModel):
    """Una barra del gráfico puente previo -> actual."""

    model_config = ConfigDict(extra="forbid")

    etiqueta: str
    monto_cent: Centimos
    tipo: Literal["entrada", "incremento", "decremento", "total", "proyeccion"]
    fact_id: str | None = None


class BloquePuente(_BloqueBase):
    """Gráfico de cascada que reconstruye el recibo previo hasta el actual.

    Es la pieza visual que responde "¿por qué me vino más caro?": barra de entrada
    (recibo previo), una barra por causa (incremento/decremento) y barra de total.
    """

    tipo: Literal["puente"] = "puente"
    barras: list[BarraPuente] = Field(default_factory=list)

    def a_texto(self) -> str:
        """Enumera las barras con su importe formateado."""
        lineas = [f"{barra.etiqueta}: {formatear_soles(barra.monto_cent)}" for barra in self.barras]
        return "\n".join(([self.titulo] if self.titulo else []) + lineas)


class BloqueTabla(_BloqueBase):
    """Tabla ya renderizada como texto: tramos, cuotas, comparativa de conceptos."""

    tipo: Literal["tabla"] = "tabla"
    columnas: list[str] = Field(default_factory=list)
    filas: list[list[str]] = Field(default_factory=list)
    nota: str | None = None

    def a_texto(self) -> str:
        """Cabecera y filas separadas por ``|``."""
        partes = [self.titulo] if self.titulo else []
        if self.columnas:
            partes.append(" | ".join(self.columnas))
        partes.extend(" | ".join(fila) for fila in self.filas)
        if self.nota:
            partes.append(self.nota)
        return "\n".join(partes)


class BloqueAviso(_BloqueBase):
    """Aviso: derivación en curso, información no disponible por nivel, etc."""

    tipo: Literal["aviso"] = "aviso"
    severidad: Literal["info", "advertencia", "critico"] = "info"
    texto: str

    def a_texto(self) -> str:
        """Devuelve el texto del aviso, con su título si existe."""
        return f"{self.titulo}\n{self.texto}" if self.titulo else self.texto


#: Unión discriminada por el campo ``tipo``. Pydantic elige la clase automáticamente.
Bloque = Annotated[
    BloqueTexto | BloqueKV | BloquePuente | BloqueTabla | BloqueAviso,
    Field(discriminator="tipo"),
]


# --------------------------------------------------------------------------- #
# Acciones, gobernanza y derivación
# --------------------------------------------------------------------------- #
class Accion(BaseModel):
    """Siguiente acción sugerida al cliente. Ninguna es irreversible en el MVP."""

    model_config = ConfigDict(extra="forbid")

    id: AccionSiguiente
    etiqueta: str
    riesgo: Literal["INFORMATIVA", "REVERSIBLE"] = "INFORMATIVA"
    payload: dict[str, Any] = Field(default_factory=dict)


class Cita(BaseModel):
    """Ancla entre un fragmento del texto y el campo del FactSet que lo respalda."""

    model_config = ConfigDict(extra="forbid")

    inicio: int = Field(ge=0, description="Índice inicial del span dentro de `texto`")
    fin: int = Field(ge=0, description="Índice final (exclusivo) del span")
    fact_id: str = Field(description='Campo del FactSet, p. ej. "linea:RENTA_PLAN.delta_cent"')
    bloque_indice: int | None = None
    token: str | None = Field(default=None, description="Token normalizado citado")

    @property
    def span(self) -> tuple[int, int]:
        """El span como tupla ``(inicio, fin)``."""
        return (self.inicio, self.fin)


class Asercion(BaseModel):
    """Una cifra encontrada en el texto generado y su veredicto de anclaje.

    La lista completa de aserciones va al evento ``VERIFY`` de la auditoría: es la
    prueba "comprobable mediante logs de la terminal" que exige la ficha.
    """

    model_config = ConfigDict(extra="forbid")

    texto_original: str = Field(description='Tal como apareció: "S/ 124,90"')
    token: str = Field(description="Token normalizado: cent:12490")
    estado: EstadoAsercion
    fuente: str | None = Field(default=None, description="fact_id que la ancla, si la hay")
    derivacion: str | None = Field(
        default=None, description="Álgebra aplicada si el estado es DERIVADA"
    )
    inicio: int | None = None
    fin: int | None = None


class Gobernanza(BaseModel):
    """Trazabilidad de la respuesta: qué se verificó, con qué versión y con qué resultado."""

    model_config = ConfigDict(extra="forbid")

    anclado: bool
    verificacion_numerica: Literal["PASS", "FAIL", "NO_APLICA"]
    aserciones_totales: int = 0
    aserciones_ancladas: int = 0
    aserciones_no_ancladas: int = 0
    confianza: float = Field(default=1.0, ge=0.0, le=1.0)
    modo: ModoGeneracion
    rules_version: str
    model_version: str
    factset_sha256: str
    citas: list[Cita] = Field(default_factory=list)
    aserciones: list[Asercion] = Field(
        default_factory=list, description="Detalle completo para la auditoría"
    )
    latencia_ms: int | None = None


class Derivacion(BaseModel):
    """Hand-off a un asesor humano con el contexto de la interacción cargado."""

    model_config = ConfigDict(extra="forbid")

    requerida: bool = False
    motivo: str | None = None
    motivo_codigo: MotivoDerivacion | None = None
    context_ref: str | None = Field(default=None, description="Referencia del contexto guardado")
    resumen_asesor: str | None = None
    senal_disparadora: str | None = Field(
        default=None, description="Regla dura o score que la disparó"
    )
    score_incomprension: float | None = Field(default=None, ge=0.0, le=1.0)


class RespuestaCanalAgnostica(BaseModel):
    """Lo que devuelve ``POST /v1/explicar``: bloques, acciones, derivación y gobernanza."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    trace_id: str
    bloques: list[Bloque] = Field(default_factory=list)
    acciones: list[Accion] = Field(default_factory=list)
    derivacion: Derivacion = Field(default_factory=Derivacion)
    gobernanza: Gobernanza
    telemetria: dict[str, Any] = Field(
        default_factory=dict,
        description="Incluye silence_probe_id para la tasa de silencio post-explicación",
    )

    @property
    def texto(self) -> str:
        """Todo el contenido legible concatenado, en el orden de los bloques.

        Es la superficie que revisa el verificador numérico: cualquier cifra que llegue
        al cliente, por el bloque que sea, aparece aquí.
        """
        return "\n".join(bloque.a_texto() for bloque in self.bloques)


# --------------------------------------------------------------------------- #
# Contratos de entrada/salida de la API
# --------------------------------------------------------------------------- #
class PeticionExplicacion(BaseModel):
    """Cuerpo de ``POST /v1/explicar``.

    ``cuenta_id`` se ignora si el token ya lo determina: ``account_ref`` se deriva
    SIEMPRE del token, jamás del texto del usuario. ``utterance`` entra al prompt
    como dato delimitado, nunca como instrucción.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID | None = None
    cuenta_id: str | None = None
    periodo: str | None = Field(default=None, description="YYYY-MM; por defecto, el último")
    verbosidad: Verbosidad = Verbosidad.CORTO
    utterance: str = Field(default="", max_length=2000)
    canal: Canal = Canal.APP


class PeticionDerivacion(BaseModel):
    """Cuerpo de ``POST /v1/derivacion``."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    cuenta_id: str | None = None
    periodo: str | None = None
    motivo_codigo: MotivoDerivacion = MotivoDerivacion.PETICION_HUMANO
    motivo: str | None = None
    utterance: str = Field(default="", max_length=2000)


class ResumenRecibo(BaseModel):
    """Una fila de ``GET /v1/historial``: lo justo para listar un periodo, sin el
    detalle línea a línea que sí trae el ``FactSet`` de ``GET /v1/hechos``.
    """

    model_config = ConfigDict(extra="forbid")

    periodo: str
    total_cent: Centimos
    fecha_emision: str
    fecha_vencimiento: str
    modalidad_renta: str
    deuda_anterior_cent: Centimos
    estado_servicio: str
    es_actual: bool = Field(description="Si es el periodo que hoy ve el cliente en Mi Recibo")


class ItemEvidencia(BaseModel):
    """Item de ``GET /v1/evidencia/{explicacion_id}``."""

    model_config = ConfigDict(extra="forbid")

    tipo: str = Field(description='"linea" | "mov" | "cat" | "tramo" | "faq" | "casuistica"')
    ref_id: str
    snippet: str
    fact_id: str | None = None


class RespuestaError(BaseModel):
    """Error de negocio con código estable (p. ej. ``INVARIANTE_FALLIDO`` en 409)."""

    model_config = ConfigDict(extra="forbid")

    codigo: str
    detalle: str
    trace_id: str | None = None
    nivel_requerido: NivelAseguramiento | None = None
    datos: dict[str, Any] = Field(default_factory=dict)
