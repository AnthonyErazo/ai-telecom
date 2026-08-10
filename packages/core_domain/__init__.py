"""Dominio canónico de recibo-claro: dinero en céntimos, enums, esquemas y reglas.

Es la única capa que todos los demás paquetes pueden importar. No depende de FastAPI,
de la base de datos ni de ningún proveedor de LLM: es aritmética, tipos y contratos.

Convenciones que este paquete impone al resto del proyecto:

* todo importe es ``int`` en céntimos, con sufijo ``_cent``;
* los rangos de fechas son ``[inicio, fin)`` con extremo derecho exclusivo;
* los identificadores de dominio van en español (``concepto_id``, ``causa``, ``tramo``);
* nada que salga de aquí contiene PII: ``cuenta_id`` es siempre una referencia tokenizada.
"""

__all__ = ["dinero", "enums", "esquemas", "reglas"]
