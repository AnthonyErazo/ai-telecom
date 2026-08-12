"""``PaqueteAsesor``: lo que un asesor humano recibe cuando el sistema le pasa un caso.

Por qué existe
--------------
El proyecto ya sabía **derivar**: dejaba un expediente en una cola con el FactSet
sellado y un brief de siete líneas. Lo que faltaba era un **contrato único de traspaso**,
igual para los tres canales, porque los tres tienen el mismo problema y distinta
solución de transporte:

* **App Mi Movistar** — el asesor entra a la conversación abierta. Ve el historial, pero
  el historial no le dice qué se pudo confirmar y qué no.
* **WhatsApp** — el asesor toma el número desde otra herramienta. No ve **nada** de
  nuestro estado: Meta no guarda el hilo de forma consultable (retiene 30 días para
  reintentos de entrega, sin extremo de lectura) y su protocolo de cesión de hilo no
  existe para WhatsApp. Sin este paquete, el asesor empieza de cero y el cliente repite.
* **Voz (Gemini Live)** — el asesor recibe la llamada. Necesita en dos segundos lo que
  la conversación tardó tres minutos en construir.

Lo común es el **contenido**; lo que cambia es el **transporte**. Este módulo define el
contenido. Cómo lo consume cada canal está en ``docs/PAQUETE_ASESOR.md``.

Las tres reglas que ordenan el contrato
---------------------------------------
1. **Todo sale de la bitácora encadenada.** El paquete no se construye desde un estado
   paralelo, sino desde los eventos ya sellados por hash. Así lo que ve el asesor es
   **exactamente** lo que se auditó, y :class:`EvidenciaAuditable` permite comprobarlo.
2. **Hay un campo para lo que NO se sabe.** :class:`Incertidumbre` es tan obligatoria
   como las cifras: un asesor que no sabe qué es hipótesis y qué es hecho confirma cosas
   que el sistema nunca afirmó. Es la diferencia entre transferir contexto y transferir
   confianza injustificada.
3. **El brief pasa por el verificador.** El texto que lee el asesor se redacta solo con
   cifras del paquete y se comprueba token a token, igual que la respuesta al cliente.
   Un brief con una cifra inventada sería peor que no tener brief: el asesor la repetiría
   con la autoridad de una persona.

El módulo es **solo contrato**: no lee ficheros ni conoce la bitácora. Quien lo construye
es :mod:`packages.governance.paquete_asesor`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import Centimos
from packages.core_domain.enums import MotivoDerivacion, VeredictoVerificacion

__all__ = [
    "ACCION_PENDIENTE",
    "CausaPaquete",
    "CifraEntregada",
    "EvidenciaAuditable",
    "Incertidumbre",
    "LineaPaquete",
    "MotivoIncertidumbre",
    "PaqueteAsesor",
    "VerificacionBrief",
    "YaExplicado",
]


#: Qué tiene que hacer el asesor, por motivo de derivación. Es la línea PENDIENTE del
#: brief: sin ella el asesor recibe contexto pero no una tarea.
#:
#: Vive en el dominio y no en el router de derivación porque la usan tres sitios —el
#: ``POST /v1/derivacion``, el brief del paquete y la consola del asesor— y una tabla
#: duplicada en dos de ellos acabaría diciendo cosas distintas del mismo motivo.
ACCION_PENDIENTE: dict[MotivoDerivacion, str] = {
    MotivoDerivacion.PETICION_HUMANO: (
        "atender la duda concreta del cliente; la explicación del recibo ya se le dio"
    ),
    MotivoDerivacion.INVARIANTE_ROTO: (
        "el recibo no concilia: verificar con facturación ANTES de confirmar cifras"
    ),
    MotivoDerivacion.CONCEPTO_FUERA_CATALOGO: (
        "hay un concepto que el catálogo no reconoce: identificarlo y explicarlo"
    ),
    MotivoDerivacion.INTENCION_REGULATORIA: (
        "tramitar por el canal formal (reclamo, baja o portabilidad), no por facturación"
    ),
    MotivoDerivacion.UMBRAL_INCOMPRENSION: (
        "reexplicar con otras palabras: el cliente repreguntó sin quedar conforme"
    ),
    MotivoDerivacion.VERIFICACION_FALLIDA: (
        "no se pudo sustentar una cifra: recalcular el detalle antes de responder"
    ),
    MotivoDerivacion.NIVEL_INSUFICIENTE: (
        "validar la identidad del cliente para poder darle importes"
    ),
    # Faltaba: sin esta entrada, la frontera del sistema —el dato no está en el recibo—
    # llegaba al asesor como un genérico "atender la consulta", que no es una tarea.
    MotivoDerivacion.FUERA_DE_ALCANCE: (
        "el dato pedido no está en el recibo (consumo, saldo, minutos): consultarlo en "
        "el sistema que lo tenga"
    ),
}


class MotivoIncertidumbre:
    """Códigos de :class:`Incertidumbre`.

    No es un ``StrEnum`` del dominio porque no viaja en ninguna decisión de negocio: es
    vocabulario de presentación para el asesor. Se declara como constantes para que la
    lista sea cerrada y grepeable, sin engordar ``enums.py`` con algo que solo lee una
    persona.
    """

    INVARIANTE_ROTO = "INVARIANTE_ROTO"
    LINEA_SIN_ATRIBUIR = "LINEA_SIN_ATRIBUIR"
    CAUSA_POCO_FIABLE = "CAUSA_POCO_FIABLE"
    CIFRA_NO_ANCLADA = "CIFRA_NO_ANCLADA"
    SIN_EXPLICACION_ENTREGADA = "SIN_EXPLICACION_ENTREGADA"
    SIN_HECHOS = "SIN_HECHOS"
    CADENA_ROTA = "CADENA_ROTA"


class LineaPaquete(BaseModel):
    """Una línea del recibo que aporta variación, tal y como quedó en la bitácora."""

    model_config = ConfigDict(extra="forbid")

    concepto_id: str
    nombre_comercial: str
    clase: str = Field(description="SUBIO · BAJO · NUEVO · DESAPARECIDO")
    monto_previo_cent: Centimos
    monto_actual_cent: Centimos
    delta_cent: Centimos
    causa: str | None = None
    confianza: float = Field(default=0.0, ge=0.0, le=1.0)
    atribuida: bool = Field(
        default=False,
        description="Falso cuando el motor vio la variación pero no supo a qué se debe",
    )


class CausaPaquete(BaseModel):
    """Una causa agregada: el *porqué* en el vocabulario del cliente, con su peso."""

    model_config = ConfigDict(extra="forbid")

    etiqueta_cliente: str
    causa: str | None = None
    monto_cent: Centimos
    participacion_bp: int = Field(default=0, description="Peso sobre |delta total|, en bp")
    confianza: float = Field(default=0.0, ge=0.0, le=1.0)
    movimientos: list[int] = Field(default_factory=list)


class CifraEntregada(BaseModel):
    """Una cifra que **ya se le dijo al cliente**, con el estado que le dio el verificador.

    Es la lista literal del evento ``VERIFY``. Para el asesor vale más que la prosa: le
    dice qué números tiene el cliente en la cabeza y cuáles de ellos están respaldados.
    """

    model_config = ConfigDict(extra="forbid")

    texto: str = Field(description="Tal y como apareció en la respuesta al cliente")
    token: str = Field(description="Token normalizado: cent:12490, num:12, pct:18.00…")
    estado: str = Field(description="ANCLADA · DERIVADA · NO_ANCLADA")
    fuente: str | None = Field(default=None, description="fact_id que la respalda")


class YaExplicado(BaseModel):
    """Qué recibió el cliente antes de la derivación. Evita que el asesor se repita."""

    model_config = ConfigDict(extra="forbid")

    hubo_explicacion: bool = False
    texto: str | None = Field(default=None, description="Literal entregado, recortado")
    modo: str | None = Field(default=None, description="LLM · PLANTILLA · NO_APLICA")
    veredicto: str | None = Field(default=None, description="Veredicto del verificador")
    cifras: list[CifraEntregada] = Field(default_factory=list)
    citas: list[str] = Field(default_factory=list, description="fact_id citados")
    score_incomprension: float | None = None


class Incertidumbre(BaseModel):
    """Algo que el sistema **no pudo confirmar**, con su porqué y su impacto.

    Existe porque el fallo más caro de un traspaso no es la falta de datos: es que el
    asesor dé por confirmado lo que era una hipótesis del motor. Aquí se nombra.
    """

    model_config = ConfigDict(extra="forbid")

    codigo: str = Field(description="Uno de MotivoIncertidumbre")
    detalle: str = Field(description="Explicado en una frase, para leerlo en voz alta")
    impacto_cent: Centimos | None = Field(
        default=None, description="Importe afectado, si se puede acotar"
    )
    evidencia: list[str] = Field(
        default_factory=list, description="Etapas o fact_id donde consta"
    )


class EvidenciaAuditable(BaseModel):
    """De dónde salió el paquete y cómo comprobarlo.

    Sin esto el paquete sería una afirmación más. Con esto, el asesor (o una auditoría
    posterior) puede pedir la traza, recorrer la cadena de hashes y ver que ningún
    evento se tocó después de escribirse.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(description="Turno ancla: el que se pide auditar")
    trazas: list[str] = Field(
        default_factory=list,
        description="Todos los turnos del caso, en orden; el ancla es el último",
    )
    factset_sha256: str | None = None
    hash_ultimo_evento: str | None = None
    eventos: int = Field(default=0, description="Eventos auditados del caso completo")
    etapas: list[str] = Field(default_factory=list, description="Etapas del turno ancla")
    cadena_valida: bool = True
    indice_roto: int | None = None
    consulta_auditoria: str = Field(
        default="",
        description="Ruta exacta para releer la evidencia, p. ej. /v1/auditoria?trace_id=…",
    )


class VerificacionBrief(BaseModel):
    """Veredicto del verificador numérico sobre el brief que lee el asesor.

    El brief es texto generado por el sistema, así que se le aplica la misma regla que a
    la respuesta al cliente: cada cifra, contra los tokens del paquete. La única
    excepción son las cifras **citadas del cliente** —lo que él escribió va entre
    comillas—, que no son afirmaciones del sistema y se listan aparte en vez de
    bloquearse: censurar la pregunta del cliente sería absurdo, y darla por respaldada,
    deshonesto.
    """

    model_config = ConfigDict(extra="forbid")

    veredicto: VeredictoVerificacion
    aserciones_totales: int = 0
    ancladas: int = 0
    no_ancladas: list[str] = Field(default_factory=list)
    citadas_del_cliente: list[str] = Field(
        default_factory=list, description="Cifras que escribió el cliente, no el sistema"
    )
    tokens_permitidos: int = 0


class PaqueteAsesor(BaseModel):
    """Contrato único de traspaso a un asesor humano, común a los tres canales.

    Se construye con
    :func:`packages.governance.paquete_asesor.construir_paquete_asesor` a partir de los
    eventos de la bitácora de un turno.
    """

    model_config = ConfigDict(extra="forbid")

    # --- identificación ------------------------------------------------- #
    context_ref: str | None = None
    conversation_id: str | None = None
    cuenta_id: str | None = None
    canal: str = Field(default="APP", description="APP · WHATSAPP · VOZ · BOT · ASESOR")
    nivel: str | None = Field(default=None, description="Nivel con el que se atendió")
    generado_en: datetime

    # --- por qué llega a una persona ------------------------------------ #
    motivo_codigo: str | None = None
    motivo_detalle: str | None = Field(
        default=None, description="Señal concreta que disparó la derivación"
    )
    accion_pendiente: str = Field(default="", description="La tarea del asesor")
    consulta_cliente: str = Field(default="", description="Lo que escribió o dijo el cliente")

    # --- el caso --------------------------------------------------------- #
    periodo_actual: str | None = None
    periodo_previo: str | None = None
    total_previo_cent: Centimos | None = None
    total_actual_cent: Centimos | None = None
    delta_total_cent: Centimos | None = None
    deuda_anterior_cent: Centimos | None = None
    total_a_pagar_cent: Centimos | None = None
    fecha_vencimiento: str | None = None
    modalidad_renta: str | None = None
    lineas: list[LineaPaquete] = Field(default_factory=list)
    causas: list[CausaPaquete] = Field(default_factory=list)
    residual_cent: Centimos | None = None
    invariante_ok: bool = True
    confianza_global: float | None = None

    # --- lo dicho y lo no confirmado ------------------------------------- #
    ya_explicado: YaExplicado = Field(default_factory=YaExplicado)
    incertidumbres: list[Incertidumbre] = Field(default_factory=list)

    # --- para leer en ocho segundos y para auditar ----------------------- #
    brief: str = Field(default="", description="Ficha etiquetada, verificada")
    verificacion_brief: VerificacionBrief | None = None
    evidencia: EvidenciaAuditable

    @property
    def apto_para_entregar(self) -> bool:
        """``True`` si el brief pasó el verificador y la cadena de hashes está íntegra.

        Es la condición que cualquier transporte —notificación, plantilla de WhatsApp,
        tarjeta de la consola— debe comprobar antes de mostrar el brief como texto.
        """
        verificado = self.verificacion_brief is not None and (
            self.verificacion_brief.veredicto is VeredictoVerificacion.PASS
        )
        return verificado and self.evidencia.cadena_valida

    def a_texto_plano(self) -> str:
        """El paquete como texto, para un canal que solo admite texto (WhatsApp, SMS).

        Es el brief más las incertidumbres. Deliberadamente **no** incluye el desglose
        línea a línea: en un canal de texto lo que hace falta es la ficha; el desglose
        se consulta en la consola con el ``context_ref``.
        """
        partes = [self.brief]
        if self.incertidumbres:
            partes.append("")
            partes.append("NO CONFIRMADO")
            partes.extend(f"  · {item.detalle}" for item in self.incertidumbres)
        return "\n".join(partes)

    def resumen_para_notificacion(self) -> dict[str, Any]:
        """Carga mínima de un aviso al asesor: quién, cuánto, por qué y dónde seguir.

        Va aparte del paquete completo porque una notificación viaja por sistemas que no
        son nuestros (correo, móvil, panel del contact center) y ahí no debe salir ni un
        importe que no haga falta.
        """
        return {
            "context_ref": self.context_ref,
            "cuenta_id": self.cuenta_id,
            "canal": self.canal,
            "motivo_codigo": self.motivo_codigo,
            "accion_pendiente": self.accion_pendiente,
            "incertidumbres": len(self.incertidumbres),
            "trace_id": self.evidencia.trace_id,
        }
