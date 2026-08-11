"""Token efímero para conectar Web o React Native con Gemini Live.

La API key permanente nunca sale de FastAPI. El token queda restringido al modelo,
audio, transcripciones y una herramienta que reutiliza ``POST /v1/explicar``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict

from apps.api.deps import AjustesDep
from apps.api.errores import ErrorApi
from apps.api.security import Identidad, requiere_nivel
from apps.api.settings import Ajustes
from packages.core_domain.enums import NivelAseguramiento

__all__ = ["crear_token_efimero", "router"]

router = APIRouter(prefix="/v1/live", tags=["gemini-live"])

_HERRAMIENTA_EXPLICAR = {
    "name": "explicar_recibo",
    "description": (
        "Obtiene una explicación financiera calculada, auditada y verificada. "
        "Úsala para toda pregunta sobre recibos, cargos, diferencias o montos."
    ),
    "parameters_json_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pregunta": {
                "type": "string",
                "description": "Pregunta literal del cliente.",
            },
            "periodo": {
                "type": ["string", "null"],
                "description": "Periodo YYYY-MM si fue indicado; null en otro caso.",
            },
            "verbosidad": {"type": "string", "enum": ["CORTO", "DETALLE"]},
        },
        "required": ["pregunta", "verbosidad"],
    },
}

_INSTRUCCION = """Eres la interfaz de voz de Recibo Claro para Movistar Perú.
Habla en español peruano, de usted, con calidez y frases breves.
Para cualquier pregunta sobre recibos, cargos, importes, periodos, planes o consumo,
DEBES llamar a explicar_recibo antes de responder. Nunca calcules ni inventes cifras.
Después de recibir la herramienta, comunica únicamente la respuesta_verificada y lee
los importes literalmente. Si la herramienta falla, ofrece derivar a un asesor.
No solicites DNI, teléfono, contraseña, tarjeta ni otros datos sensibles.
"""


class RespuestaTokenLive(BaseModel):
    """Credencial breve y metadatos públicos para abrir una sesión Live."""

    model_config = ConfigDict(extra="forbid")

    token: str
    model: str
    voice: str
    expire_time: datetime
    new_session_expire_time: datetime


def crear_token_efimero(ajustes: Ajustes, *, cliente: Any | None = None) -> RespuestaTokenLive:
    """Crea un token de un uso, restringido a audio y a la herramienta verificada."""
    if not ajustes.gemini_api_key.strip():
        raise ErrorApi(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "GEMINI_LIVE_NO_CONFIGURADO",
            "falta GEMINI_API_KEY para crear una sesión de voz",
        )

    if cliente is None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependencia declarada
            raise ErrorApi(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "GEMINI_LIVE_NO_DISPONIBLE",
                "el paquete google-genai no está instalado",
            ) from exc
        cliente = genai.Client(api_key=ajustes.gemini_api_key)

    ahora = datetime.now(UTC)
    expira = ahora + timedelta(minutes=ajustes.gemini_live_session_min)
    nueva_sesion_expira = ahora + timedelta(minutes=1)
    configuracion = {
        "uses": 1,
        "expire_time": expira,
        "new_session_expire_time": nueva_sesion_expira,
        "live_connect_constraints": {
            "model": ajustes.gemini_live_model,
            "config": {
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "voice_config": {
                        "prebuilt_voice_config": {"voice_name": ajustes.gemini_live_voice}
                    },
                    "language_code": "es-PE",
                },
                "system_instruction": _INSTRUCCION,
                "tools": [{"function_declarations": [_HERRAMIENTA_EXPLICAR]}],
                "session_resumption": {},
                "input_audio_transcription": {},
                "output_audio_transcription": {},
            },
        },
    }
    try:
        emitido = cliente.auth_tokens.create(config=configuracion)
    except Exception as exc:
        raise ErrorApi(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "GEMINI_LIVE_TOKEN_FALLO",
            f"Gemini no pudo crear el token efímero: {type(exc).__name__}",
        ) from exc

    nombre = str(getattr(emitido, "name", "") or "").strip()
    if not nombre:
        raise ErrorApi(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "GEMINI_LIVE_TOKEN_INVALIDO",
            "Gemini devolvió un token efímero vacío",
        )
    return RespuestaTokenLive(
        token=nombre,
        model=ajustes.gemini_live_model,
        voice=ajustes.gemini_live_voice,
        expire_time=expira,
        new_session_expire_time=nueva_sesion_expira,
    )


@router.post("/token", response_model=RespuestaTokenLive, summary="Token efímero para Gemini Live")
def token_live(
    _identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    ajustes: AjustesDep,
) -> RespuestaTokenLive:
    """Entrega una credencial de un uso; la API key nunca sale del backend."""
    return crear_token_efimero(ajustes)
