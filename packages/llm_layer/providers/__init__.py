"""Proveedores de generación: contrato común, mock determinístico, Gemini y LangChain.

``obtener_proveedor()`` resuelve el modo desde ``LLM_MODE``.

Los proveedores concretos se exponen de forma **perezosa** (PEP 562): ``MockProvider``
se apoya en ``packages.llm_layer.plantillas``, que a su vez necesita el contrato de
``providers.base``. Importarlos aquí de forma directa cerraría ese ciclo. Además, así
este paquete se puede importar sin tener ``google-genai`` ni ``langchain-core``
instalados: ambas son dependencias opcionales de su proveedor.
"""

from typing import Any

from packages.llm_layer.providers.base import (
    ESQUEMA_EXPLICACION_V1,
    MODO_GEMINI,
    MODO_LANGCHAIN,
    MODO_MOCK,
    MODOS_VALIDOS,
    NOMBRE_ESQUEMA_SALIDA,
    TIMEOUT_POR_DEFECTO_S,
    CausaExplicadaLLM,
    ErrorConfiguracionProveedor,
    ErrorCuota,
    ErrorProveedor,
    ErrorRespuestaInvalida,
    ErrorTiempoAgotado,
    ExplicacionLLM,
    ProveedorLLM,
    modo_por_defecto,
    obtener_proveedor,
    timeout_por_defecto,
    version_modelo_de,
)

__all__ = [
    "ESQUEMA_EXPLICACION_V1",
    "MODOS_VALIDOS",
    "MODO_GEMINI",
    "MODO_LANGCHAIN",
    "MODO_MOCK",
    "NOMBRE_ESQUEMA_SALIDA",
    "TIMEOUT_POR_DEFECTO_S",
    "CausaExplicadaLLM",
    "ErrorConfiguracionProveedor",
    "ErrorCuota",
    "ErrorProveedor",
    "ErrorRespuestaInvalida",
    "ErrorTiempoAgotado",
    "ExplicacionLLM",
    "GeminiProvider",
    "LangChainProvider",
    "MockProvider",
    "ProveedorLLM",
    "modo_por_defecto",
    "obtener_proveedor",
    "timeout_por_defecto",
    "version_modelo_de",
]

_PEREZOSOS = {
    "MockProvider": "packages.llm_layer.providers.mock",
    "GeminiProvider": "packages.llm_layer.providers.gemini",
    "LangChainProvider": "packages.llm_layer.providers.langchain_",
}


def __getattr__(nombre: str) -> Any:
    """Importa los proveedores concretos solo cuando se piden."""
    modulo = _PEREZOSOS.get(nombre)
    if modulo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
    from importlib import import_module

    valor = getattr(import_module(modulo), nombre)
    globals()[nombre] = valor
    return valor
