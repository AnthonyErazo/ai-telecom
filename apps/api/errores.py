"""Errores de negocio de la API con códigos estables.

El cuerpo de todo error es un :class:`RespuestaError` del dominio
(``{codigo, detalle, trace_id, nivel_requerido, datos}``), nunca el ``{"detail": ...}``
por defecto de FastAPI: el código es parte del contrato y los clientes (App, Bot Lucía,
WhatsApp) enrutan por él, no por el texto.

Catálogo de códigos:

===========================  ======  =====================================================
Código                       HTTP    Cuándo
===========================  ======  =====================================================
``TOKEN_AUSENTE``            401     No llegó cabecera ``Authorization: Bearer``.
``TOKEN_INVALIDO``           401     Firma, emisor o audiencia incorrectos.
``TOKEN_EXPIRADO``           401     ``exp`` vencido.
``NIVEL_INSUFICIENTE``       403     El ``acr`` del token no alcanza el mínimo del recurso.
``ACTOR_REQUERIDO``          403     ``LOA_ASESOR`` sin ``acting_on_behalf_of``.
``CUENTA_NO_AUTORIZADA``     403     Se pidió una cuenta distinta a la del token.
``CUENTA_NO_ENCONTRADA``     404     BrainyBill no tiene esa cuenta.
``PERIODO_NO_ENCONTRADO``    404     La cuenta existe pero no ese periodo.
``CONCEPTO_NO_ENCONTRADO``   404     ``concepto_id`` fuera del catálogo.
``EXPLICACION_NO_ENCONTRADA``404     ``explicacion_id`` desconocido o caducado.
``TRAZA_NO_ENCONTRADA``      404     ``trace_id`` sin eventos en la bitácora.
``SIN_RECIBO_PREVIO``        422     Solo hay un recibo: no hay variación que explicar.
``INVARIANTE_FALLIDO``       409     ``|residual_cent| > 1``. **Solo en ``/v1/hechos``.**
``SISTEMA_EXTERNO_CAIDO``    503     BrainyBill o Amdocs no responden.
``FUNCION_NO_DISPONIBLE``    404     Router ``/dev`` con ``ENTORNO != dev``.
===========================  ======  =====================================================

**El LLM caído no está en esta tabla a propósito.** La tabla de la sección 9 anota
``424`` junto a ``/v1/explicar``, pero aclara entre paréntesis que degradar a plantilla
*"**no** es error"*. Se resuelve como degradación: ``200`` con
``gobernanza.modo = "PLANTILLA"``, la cabecera ``X-Degradado`` y
``telemetria["degradado"]``. Un canal que trate el 424 como fallo dejaría al cliente sin
respuesta cuando el sistema sí sabe responder.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from packages.core_domain.enums import NivelAseguramiento
from packages.core_domain.esquemas.respuesta import RespuestaError

__all__ = [
    "ErrorApi",
    "cuenta_no_autorizada",
    "cuenta_no_encontrada",
    "invariante_fallido",
    "nivel_insuficiente",
    "no_encontrado",
    "sistema_externo_caido",
]


class ErrorApi(HTTPException):
    """Excepción HTTP cuyo cuerpo es un :class:`RespuestaError`."""

    def __init__(
        self,
        estado: int,
        codigo: str,
        detalle: str,
        *,
        trace_id: str | None = None,
        nivel_requerido: NivelAseguramiento | None = None,
        datos: dict[str, Any] | None = None,
        cabeceras: dict[str, str] | None = None,
    ) -> None:
        self.cuerpo = RespuestaError(
            codigo=codigo,
            detalle=detalle,
            trace_id=trace_id,
            nivel_requerido=nivel_requerido,
            datos=datos or {},
        )
        super().__init__(
            status_code=estado,
            detail=self.cuerpo.model_dump(mode="json"),
            headers=cabeceras,
        )


def no_encontrado(codigo: str, detalle: str, **datos: Any) -> ErrorApi:
    """Atajo para los 404 del catálogo."""
    return ErrorApi(status.HTTP_404_NOT_FOUND, codigo, detalle, datos=datos)


def cuenta_no_encontrada(cuenta_id: str) -> ErrorApi:
    """404 cuando BrainyBill no conoce la cuenta."""
    return no_encontrado(
        "CUENTA_NO_ENCONTRADA",
        f"no hay recibos para la cuenta {cuenta_id} en BrainyBill",
        cuenta_id=cuenta_id,
    )


def cuenta_no_autorizada(pedida: str, del_token: str) -> ErrorApi:
    """403 cuando el cliente pide una cuenta distinta a la de su token.

    El ``account_ref`` sale SIEMPRE del token. Que el cuerpo o la query traigan otra
    cuenta no es un caso de "usar el del token en silencio": es un intento de acceso
    cruzado y se rechaza con ruido para que quede en la bitácora.
    """
    return ErrorApi(
        status.HTTP_403_FORBIDDEN,
        "CUENTA_NO_AUTORIZADA",
        "el identificador de cuenta pedido no coincide con el del token; "
        "la cuenta se deriva siempre del token",
        datos={"cuenta_pedida": pedida, "cuenta_del_token": del_token},
    )


def nivel_insuficiente(actual: NivelAseguramiento, minimo: NivelAseguramiento) -> ErrorApi:
    """403 con el nivel exigido, para que el canal sepa a qué escalar la autenticación."""
    return ErrorApi(
        status.HTTP_403_FORBIDDEN,
        "NIVEL_INSUFICIENTE",
        f"el recurso exige nivel {minimo} y su sesión está autenticada como {actual}",
        nivel_requerido=minimo,
        datos={"nivel_actual": str(actual)},
    )


def invariante_fallido(
    cuenta_id: str, periodo: str, residual_cent: int, *, trace_id: str | None = None
) -> ErrorApi:
    """409 de ``/v1/hechos``: el recibo no concilia, así que no se explica, se deriva."""
    return ErrorApi(
        status.HTTP_409_CONFLICT,
        "INVARIANTE_FALLIDO",
        "la suma de las variaciones por concepto no reproduce la diferencia entre "
        "totales; el recibo no se explica, se deriva a un asesor",
        trace_id=trace_id,
        datos={
            "cuenta_id": cuenta_id,
            "periodo": periodo,
            "residual_cent": residual_cent,
            "tolerancia_cent": 1,
        },
    )


def sistema_externo_caido(sistema: str, detalle: str) -> ErrorApi:
    """503 cuando BrainyBill o Amdocs no responden."""
    return ErrorApi(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "SISTEMA_EXTERNO_CAIDO",
        f"{sistema} no está disponible: {detalle}",
        datos={"sistema": sistema},
    )
