"""Routers de la API, uno por recurso (sección 9 de la especificación).

=========================  ======  ===============================================
Router                     Nivel   Ruta
=========================  ======  ===============================================
``salud``                  —       ``GET /salud``, ``/salud/preparacion``
``catalogo``               LOA0    ``GET /v1/catalogo/{concepto_id}``
``explicar``               LOA1    ``POST /v1/explicar``
``derivacion``             LOA1    ``POST /v1/derivacion``
``hechos``                 LOA2    ``GET /v1/hechos``
``historial``              LOA2    ``GET /v1/historial``
``evidencia``              LOA2    ``GET /v1/evidencia/{explicacion_id}``
``auditoria``              LOA2    ``GET /v1/auditoria``
``dev``                    —       ``POST /dev/token``, ``/dev/alucinar`` (solo dev)
=========================  ======  ===============================================
"""

from apps.api.routers import (
    auditoria,
    catalogo,
    conversaciones,
    derivacion,
    dev,
    evidencia,
    explicar,
    hechos,
    historial,
    salud,
)

__all__ = [
    "auditoria",
    "catalogo",
    "conversaciones",
    "derivacion",
    "dev",
    "evidencia",
    "explicar",
    "hechos",
    "historial",
    "salud",
]
