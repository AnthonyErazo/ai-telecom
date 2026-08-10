"""Capa LLM: proveedores, prompts, plantillas determinísticas, generador y verificador.

Punto de entrada del módulo::

    from packages.llm_layer import explicar, generar_explicacion, verificar

Garantía que sostiene todo lo demás: **ninguna cifra del texto entregado proviene del
modelo**. El modelo aporta prosa; los importes los inyecta el sistema desde el FactSet
y :func:`~packages.llm_layer.verificador.verificar` audita el resultado cifra a cifra
antes de que salga. Si algo no se puede anclar, la respuesta se bloquea.
"""

from packages.llm_layer.generador import (
    IntentoGeneracion,
    ResultadoGeneracion,
    a_respuesta,
    componer_bloques,
    explicar,
    generar_explicacion,
)
from packages.llm_layer.providers.base import (
    ESQUEMA_EXPLICACION_V1,
    ErrorProveedor,
    ExplicacionLLM,
    ProveedorLLM,
    obtener_proveedor,
)
from packages.llm_layer.verificador import (
    ConjuntoPermitido,
    DerivacionNumerica,
    ResultadoVerificacion,
    construir_permitidos,
    extraer_aserciones,
    extraer_numeros,
    inyectar_alucinacion,
    verificar,
)

__all__ = [
    "ESQUEMA_EXPLICACION_V1",
    "ConjuntoPermitido",
    "DerivacionNumerica",
    "ErrorProveedor",
    "ExplicacionLLM",
    "IntentoGeneracion",
    "ProveedorLLM",
    "ResultadoGeneracion",
    "ResultadoVerificacion",
    "a_respuesta",
    "componer_bloques",
    "construir_permitidos",
    "explicar",
    "extraer_aserciones",
    "extraer_numeros",
    "generar_explicacion",
    "inyectar_alucinacion",
    "obtener_proveedor",
    "verificar",
]
