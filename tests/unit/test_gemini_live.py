"""Configuración segura de tokens efímeros para Gemini Live."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.errores import ErrorApi
from apps.api.routers.live import crear_token_efimero
from apps.api.settings import Ajustes


class _TokensFalsos:
    def __init__(self) -> None:
        self.config: dict | None = None

    def create(self, *, config: dict):
        self.config = config
        return SimpleNamespace(name="auth_tokens/efimero-prueba")


def test_token_live_restringe_modelo_audio_y_herramienta() -> None:
    tokens = _TokensFalsos()
    cliente = SimpleNamespace(auth_tokens=tokens)
    ajustes = Ajustes(
        GEMINI_API_KEY="secreto-prueba",
        GEMINI_LIVE_MODEL="gemini-3.1-flash-live-preview",
        GEMINI_LIVE_VOICE="Kore",
    )

    respuesta = crear_token_efimero(ajustes, cliente=cliente)

    assert respuesta.token == "auth_tokens/efimero-prueba"
    assert tokens.config is not None
    assert tokens.config["uses"] == 1
    restricciones = tokens.config["live_connect_constraints"]
    assert restricciones["model"] == "gemini-3.1-flash-live-preview"
    assert restricciones["config"]["response_modalities"] == ["AUDIO"]
    herramienta = restricciones["config"]["tools"][0]["function_declarations"][0]
    assert herramienta["name"] == "explicar_recibo"


def test_token_live_exige_api_key() -> None:
    with pytest.raises(ErrorApi) as capturado:
        crear_token_efimero(Ajustes(GEMINI_API_KEY=""), cliente=SimpleNamespace())
    assert capturado.value.cuerpo.codigo == "GEMINI_LIVE_NO_CONFIGURADO"
