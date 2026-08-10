"""Contrato común de los proveedores generativos (sección 5.1 de la especificación).

Aquí vive lo único que el resto del sistema necesita saber de un modelo de lenguaje:

* :class:`ProveedorLLM` — el ``Protocol`` que todos cumplen (``nombre`` + ``completar``).
* :class:`ExplicacionLLM` — el **único** esquema de salida admitido (``explicacion_v1``).
* :class:`ErrorProveedor` — la excepción tipada que obliga al generador a degradar a
  plantilla determinística en vez de propagar un fallo al cliente.
* :func:`obtener_proveedor` — la fábrica que lee ``LLM_MODE`` del entorno.

Regla innegociable nº 2: el modelo **no calcula**. Pedirle ``monto_cent_citado`` como
entero en céntimos hace trivial el verificador: comparar enteros, no cadenas.
"""

from __future__ import annotations

import contextvars
import logging
import os
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import AccionSiguiente

__all__ = [
    "ESQUEMA_EXPLICACION_V1",
    "MODOS_VALIDOS",
    "MODO_GEMINI",
    "MODO_LANGCHAIN",
    "MODO_MOCK",
    "NOMBRE_ESQUEMA_SALIDA",
    "TIMEOUT_POR_DEFECTO_S",
    "VAR_MODO",
    "VAR_TIMEOUT",
    "CausaExplicadaLLM",
    "ErrorConfiguracionProveedor",
    "ErrorCuota",
    "ErrorProveedor",
    "ErrorRespuestaInvalida",
    "ErrorTiempoAgotado",
    "ExplicacionLLM",
    "ProveedorEnCascada",
    "ProveedorLLM",
    "modo_por_defecto",
    "modos_registrados",
    "obtener_proveedor",
    "registrar_proveedor",
    "timeout_por_defecto",
    "version_modelo_de",
]

_LOG = logging.getLogger(__name__)

#: Modelo que resolvió el turno **en este contexto de ejecución**. Lo escribe
#: :class:`ProveedorEnCascada` y lo lee su ``version_modelo``. Es un ``ContextVar`` y no
#: un atributo porque el proveedor puede compartirse entre peticiones concurrentes y la
#: bitácora debe decir qué modelo respondió a *este* turno, no al de al lado.
_ULTIMO_MODELO: contextvars.ContextVar[str] = contextvars.ContextVar(
    "recibo_claro_ultimo_modelo", default=""
)

#: Variables de entorno que gobiernan la capa generativa (ver ``.env.example``).
VAR_MODO = "LLM_MODE"
VAR_TIMEOUT = "LLM_TIMEOUT_S"

MODO_MOCK = "mock"
MODO_GEMINI = "gemini"
#: Adaptador sobre ``langchain-core`` (MIT): cualquier modelo soportado por LangChain
#: sin tocar el generador ni el verificador. Ver ``providers/langchain_.py``.
MODO_LANGCHAIN = "langchain"

#: Los tres modos que vienen **de serie**. ``mock`` es el de la demo: byte-reproducible.
#:
#: No es la autoridad de la fábrica: desde que existe el registro, quien manda es
#: :func:`modos_registrados`, que además incluye lo que hayan dado de alta terceros.
#: Esta tupla se conserva porque es API pública y porque distingue «lo que trae el
#: proyecto» de «lo que alguien enchufó»; para validar un ``LLM_MODE``, use la función.
MODOS_VALIDOS: tuple[str, ...] = (MODO_MOCK, MODO_GEMINI, MODO_LANGCHAIN)

#: Timeout por defecto de una llamada generativa. Al agotarse se cae a plantilla.
TIMEOUT_POR_DEFECTO_S = 4.0

#: Nombre del esquema de salida versionado.
NOMBRE_ESQUEMA_SALIDA = "explicacion_v1"


# --------------------------------------------------------------------------- #
# Errores tipados
# --------------------------------------------------------------------------- #
class ErrorProveedor(RuntimeError):
    """Fallo de un proveedor generativo.

    El generador **siempre** captura esta excepción y degrada a la plantilla
    determinística: un modelo caído no puede convertirse en un error para el cliente
    (la API responde 424 informativo, nunca 500).
    """

    codigo_por_defecto = "ERROR_PROVEEDOR"

    def __init__(
        self,
        mensaje: str,
        *,
        proveedor: str = "desconocido",
        codigo: str | None = None,
        reintentable: bool = False,
    ) -> None:
        super().__init__(mensaje)
        self.proveedor = proveedor
        self.codigo = codigo or self.codigo_por_defecto
        self.reintentable = reintentable

    def a_dict(self) -> dict[str, Any]:
        """Representación para el log de auditoría (etapa ``LLM_CALL``)."""
        return {
            "codigo": self.codigo,
            "proveedor": self.proveedor,
            "detalle": str(self),
            "reintentable": self.reintentable,
        }


class ErrorConfiguracionProveedor(ErrorProveedor):
    """Falta una credencial, una dependencia o el modo pedido no existe."""

    codigo_por_defecto = "CONFIGURACION_INVALIDA"


class ErrorTiempoAgotado(ErrorProveedor):
    """La llamada superó el timeout. Es reintentable, pero el generador prefiere plantilla."""

    codigo_por_defecto = "TIEMPO_AGOTADO"

    def __init__(self, mensaje: str, **kwargs: Any) -> None:
        kwargs.setdefault("reintentable", True)
        super().__init__(mensaje, **kwargs)


class ErrorCuota(ErrorProveedor):
    """Cuota agotada o límite de tasa del proveedor (HTTP 429 / RESOURCE_EXHAUSTED)."""

    codigo_por_defecto = "CUOTA_AGOTADA"


class ErrorRespuestaInvalida(ErrorProveedor):
    """El proveedor respondió algo que no valida contra ``explicacion_v1``."""

    codigo_por_defecto = "RESPUESTA_INVALIDA"


# --------------------------------------------------------------------------- #
# Esquema de salida — explicacion_v1
# --------------------------------------------------------------------------- #
class CausaExplicadaLLM(BaseModel):
    """Una causa narrada por el modelo, con el importe citado como entero.

    ``monto_cent_citado`` es la pieza que hace trivial el verificador estructural:
    la suma de los importes citados **debe** ser exactamente ``delta_total_cent``.
    """

    model_config = ConfigDict(extra="ignore")

    concepto_id: str = Field(description="Debe existir en el FactSet")
    frase: str = Field(max_length=320, description="Explicación en lenguaje de cliente")
    monto_cent_citado: int = Field(description="Importe en CÉNTIMOS enteros, con signo")


class ExplicacionLLM(BaseModel):
    """Salida estructurada ``explicacion_v1``: lo único que se acepta de un modelo.

    ``extra="ignore"`` es deliberado: un modelo locuaz que añada campos no debe tumbar
    la petición. Lo que no se tolera es una cifra sin anclar, y de eso se ocupa el
    verificador sobre el texto final, no este esquema.
    """

    model_config = ConfigDict(extra="ignore")

    resumen: str = Field(max_length=180, description="Una sola idea, ≤180 caracteres")
    causas: list[CausaExplicadaLLM] = Field(default_factory=list)
    siguiente_paso: AccionSiguiente = AccionSiguiente.VER_DETALLE
    cifras_usadas: list[int] = Field(
        default_factory=list,
        description="Todas las cifras en céntimos que el modelo dice haber usado",
    )

    def suma_citada_cent(self) -> int:
        """Suma de los importes citados por causa (debe igualar ``delta_total_cent``)."""
        return sum(causa.monto_cent_citado for causa in self.causas)

    def frases(self) -> list[str]:
        """Las frases por causa, en el orden en que el modelo las produjo."""
        return [causa.frase for causa in self.causas]


#: JSON Schema de ``explicacion_v1``. Se envía literal al proveedor para forzar
#: salida estructurada; ``propertyOrdering`` lo respeta la API de Google.
ESQUEMA_EXPLICACION_V1: dict[str, Any] = {
    "title": NOMBRE_ESQUEMA_SALIDA,
    "type": "object",
    "required": ["resumen", "causas", "siguiente_paso", "cifras_usadas"],
    "propertyOrdering": ["resumen", "causas", "siguiente_paso", "cifras_usadas"],
    "properties": {
        "resumen": {
            "type": "string",
            "maxLength": 180,
            "description": "Explicación en una frase, español de Perú, trato de usted.",
        },
        "causas": {
            "type": "array",
            "description": "Una entrada por causa de variación presente en FACTSET.",
            "items": {
                "type": "object",
                "required": ["concepto_id", "frase", "monto_cent_citado"],
                "propertyOrdering": ["concepto_id", "frase", "monto_cent_citado"],
                "properties": {
                    "concepto_id": {
                        "type": "string",
                        "description": "Copiado tal cual de FACTSET.lineas[].concepto_id",
                    },
                    "frase": {
                        "type": "string",
                        "maxLength": 320,
                        "description": "Por qué varió, sin tecnicismos y sin inventar cifras.",
                    },
                    "monto_cent_citado": {
                        "type": "integer",
                        "description": "delta_cent de FACTSET, copiado como entero con signo.",
                    },
                },
            },
        },
        "siguiente_paso": {
            "type": "string",
            "enum": [accion.value for accion in AccionSiguiente],
        },
        "cifras_usadas": {
            "type": "array",
            "description": "Enteros en céntimos copiados de FACTSET. Nada más.",
            "items": {"type": "integer"},
        },
    },
}


# --------------------------------------------------------------------------- #
# Protocolo
# --------------------------------------------------------------------------- #
@runtime_checkable
class ProveedorLLM(Protocol):
    """Contrato mínimo de un proveedor generativo (literal de la sección 5.1).

    Un proveedor **solo redacta**: recibe un prompt ya construido (con el FactSet
    dentro) y devuelve un ``dict`` que debe validar contra ``explicacion_v1``. No
    accede a la base de datos, no ejecuta acciones y no hace aritmética.
    """

    nombre: str

    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        """Genera una respuesta estructurada conforme a ``esquema``.

        Raises:
            ErrorProveedor: cualquier fallo (red, cuota, timeout, salida inválida).
        """
        ...


def version_modelo_de(proveedor: ProveedorLLM) -> str:
    """Versión del modelo para ``Gobernanza.model_version``.

    El ``Protocol`` de la especificación solo exige ``nombre``; los proveedores
    concretos publican además ``version_modelo``. Este ayudante evita ampliar el
    contrato para un dato que es puramente de trazabilidad.
    """
    return str(getattr(proveedor, "version_modelo", None) or proveedor.nombre)


# --------------------------------------------------------------------------- #
# Fábrica
# --------------------------------------------------------------------------- #
def modo_por_defecto() -> str:
    """Modo de generación configurado en ``LLM_MODE`` (``mock`` si no está definido)."""
    return (os.getenv(VAR_MODO) or MODO_MOCK).strip().lower()


def timeout_por_defecto() -> float:
    """Timeout configurado en ``LLM_TIMEOUT_S``; 4 s si no está definido o es inválido."""
    bruto = os.getenv(VAR_TIMEOUT)
    if not bruto:
        return TIMEOUT_POR_DEFECTO_S
    try:
        valor = float(bruto.strip())
    except ValueError:
        return TIMEOUT_POR_DEFECTO_S
    return valor if valor > 0 else TIMEOUT_POR_DEFECTO_S


#: Registro de proveedores: ``modo -> (módulo, clase)``.
#:
#: Es un **mapa y no una cadena de ``if``** por una razón concreta: BASES §9 cede la
#: propiedad intelectual a Integratel, y un integrador que quiera enchufar el modelo
#: corporativo debe poder hacerlo **sin editar esta función**. Con el registro, añadir
#: un fabricante es una línea de alta; con la cadena de ``if``, era un parche en el
#: despachador y un conflicto de fusión garantizado.
#:
#: El valor es ``(módulo, clase)`` y no la clase misma para conservar el **import
#: diferido**: `langchain-core` es dependencia opcional del proveedor, y con
#: ``LLM_MODE=gemini`` no debe importarse nunca.
_REGISTRO: dict[str, tuple[str, str]] = {
    MODO_MOCK: ("packages.llm_layer.providers.mock", "MockProvider"),
    MODO_GEMINI: ("packages.llm_layer.providers.gemini", "GeminiProvider"),
    MODO_LANGCHAIN: ("packages.llm_layer.providers.langchain_", "LangChainProvider"),
}


def registrar_proveedor(modo: str, modulo: str, clase: str) -> None:
    """Da de alta un proveedor sin tocar la fábrica.

    Pensado para que un tercero —Integratel, por ejemplo— enchufe su modelo
    corporativo desde fuera del paquete::

        registrar_proveedor("movistar", "mi_paquete.proveedor", "ProveedorMovistar")

    La clase solo tiene que cumplir :class:`ProveedorLLM`. No se importa aquí: se
    resuelve la primera vez que se pide ese modo.

    Args:
        modo: clave para ``LLM_MODE``. Se normaliza a minúsculas.
        modulo: ruta de importación del módulo que contiene la clase.
        clase: nombre de la clase dentro de ese módulo.

    Raises:
        ValueError: si ``modo`` está vacío.
    """
    clave = (modo or "").strip().lower()
    if not clave:
        raise ValueError("el modo no puede estar vacío")
    _REGISTRO[clave] = (modulo, clase)


def modos_registrados() -> tuple[str, ...]:
    """Modos que la fábrica sabe resolver ahora mismo, en orden alfabético."""
    return tuple(sorted(_REGISTRO))


def _instanciar(modo: str, **opciones: Any) -> ProveedorLLM:
    """Resuelve un modo simple del registro, con import diferido."""
    entrada = _REGISTRO.get(modo)
    if entrada is None:
        raise ErrorConfiguracionProveedor(
            f"LLM_MODE={modo!r} no es un modo válido; use uno de {', '.join(modos_registrados())}",
            proveedor=modo,
        )
    nombre_modulo, nombre_clase = entrada
    from importlib import import_module

    return getattr(import_module(nombre_modulo), nombre_clase)(**opciones)


class ProveedorEnCascada:
    """Varios proveedores en orden: si el primero falla, se intenta el siguiente.

    Existe por una petición explícita: *«usemos siempre la IA, no importa que se haya
    agotado»*. Sin cascada, un ``429`` de cuota degradaba directamente a la plantilla
    determinística; con ella, primero se agota la lista de modelos disponibles.

    **Qué se reintenta y qué no.** Un :class:`ErrorConfiguracionProveedor` significa que
    ese proveedor no está utilizable en esta instalación —falta la credencial o el SDK—,
    así que se salta en silencio: no es un fallo del turno. Cualquier otro
    :class:`ErrorProveedor` (cuota, tiempo agotado, respuesta inválida) sí es un intento
    consumido y se anota. Si se acaba la lista, se relanza el **último** error, de modo
    que el generador degrade a plantilla exactamente como hacía antes. La red de
    seguridad no se toca: solo se retrasa.

    **Por qué la traza usa ``contextvars``.** ``version_modelo`` tiene que decir *qué
    modelo respondió de verdad*, y este objeto puede ser único y compartido entre
    peticiones concurrentes. Guardarlo en un atributo de instancia haría que la petición
    A leyese el modelo que resolvió la B — una mentira en la bitácora, que es justo lo
    que este sistema no se puede permitir. Un ``ContextVar`` es por contexto de
    ejecución, así que cada turno lee el suyo.
    """

    nombre = "cascada"

    def __init__(self, proveedores: Sequence[ProveedorLLM]) -> None:
        """Crea la cascada.

        Args:
            proveedores: en orden de preferencia; el primero es el principal.

        Raises:
            ErrorConfiguracionProveedor: si la lista está vacía.
        """
        if not proveedores:
            raise ErrorConfiguracionProveedor(
                "una cascada necesita al menos un proveedor", proveedor="cascada"
            )
        self._proveedores: tuple[ProveedorLLM, ...] = tuple(proveedores)

    @property
    def proveedores(self) -> tuple[ProveedorLLM, ...]:
        """Los proveedores configurados, en orden."""
        return self._proveedores

    @property
    def version_modelo(self) -> str:
        """Modelo que respondió en **este** contexto; el principal si aún no respondió."""
        return _ULTIMO_MODELO.get() or version_modelo_de(self._proveedores[0])

    def completar(
        self, prompt: str, esquema: dict, timeout_s: float = TIMEOUT_POR_DEFECTO_S
    ) -> dict:
        """Intenta cada proveedor en orden hasta que uno responda.

        Raises:
            ErrorProveedor: el último error, si ninguno respondió.
        """
        ultimo: ErrorProveedor | None = None
        for proveedor in self._proveedores:
            try:
                respuesta = proveedor.completar(prompt, esquema, timeout_s)
            except ErrorConfiguracionProveedor as exc:
                # No utilizable aquí (sin credencial o sin SDK): no cuenta como intento.
                _LOG.debug("cascada: %s no disponible (%s)", proveedor.nombre, exc)
                ultimo = ultimo or exc
                continue
            except ErrorProveedor as exc:
                _LOG.warning(
                    "cascada: %s falló (%s); se intenta el siguiente",
                    proveedor.nombre,
                    type(exc).__name__,
                )
                ultimo = exc
                continue
            _ULTIMO_MODELO.set(version_modelo_de(proveedor))
            return respuesta
        assert ultimo is not None  # la lista nunca está vacía (ver __init__)
        raise ultimo


def obtener_proveedor(modo: str | None = None, **opciones: Any) -> ProveedorLLM:
    """Devuelve el proveedor correspondiente a ``modo`` (``LLM_MODE`` si es ``None``).

    Acepta **un modo o varios separados por comas**. Con varios se devuelve un
    :class:`ProveedorEnCascada`::

        LLM_MODE=gemini            un solo proveedor (comportamiento de siempre)
        LLM_MODE=gemini,langchain  si Gemini agota cuota, se intenta LangChain

    Args:
        modo: clave registrada, o varias separadas por comas. ``None`` lee ``LLM_MODE``.
        **opciones: se pasan al constructor de cada proveedor (``modelo``, ``api_key``…).

    Raises:
        ErrorConfiguracionProveedor: si algún modo no está registrado, o si el proveedor
            no se puede instanciar (falta la credencial o el SDK).
    """
    bruto = modo if modo is not None else modo_por_defecto()
    modos = [parte.strip().lower() for parte in bruto.split(",") if parte.strip()]
    if not modos:
        modos = [MODO_MOCK]
    if len(modos) == 1:
        return _instanciar(modos[0], **opciones)
    return ProveedorEnCascada([_instanciar(m, **opciones) for m in modos])
