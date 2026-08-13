"""``GeminiProvider``: proveedor generativo primario, sobre el SDK oficial ``google-genai``.

Configuración por entorno (ver ``.env.example``):

======================  =======================================================
``GEMINI_API_KEY``      credencial; **nunca** se commitea. Si falta, no se instancia.
``GEMINI_MODEL``        id del modelo. **[POR VALIDAR]** verifíquelo en la documentación
                        vigente de Google antes de la demo: los identificadores y su
                        disponibilidad cambian entre versiones.
``LLM_TIMEOUT_S``       tiempo máximo por llamada; al agotarse se cae a plantilla.
======================  =======================================================

Decisiones que no son negociables en este archivo:

* ``temperature=0`` y ``candidate_count=1``: la demo debe ser reproducible.
* Salida **JSON estructurada** contra ``explicacion_v1``: si el modelo devuelve prosa
  libre, la respuesta se rechaza aquí y no llega al verificador.
* Todo fallo —red, cuota, timeout, salida inválida— se traduce a
  :class:`~packages.llm_layer.providers.base.ErrorProveedor`, que es la señal que usa
  el generador para degradar a la plantilla determinística.

El SDK se importa de forma diferida: el resto del sistema (y toda la batería de
tests en modo mock) funciona sin tener ``google-genai`` instalado.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import suppress
from typing import Any

from packages.llm_layer.providers.base import (
    ErrorConfiguracionProveedor,
    ErrorCuota,
    ErrorProveedor,
    ErrorRespuestaInvalida,
    ErrorTiempoAgotado,
    ExplicacionLLM,
    timeout_por_defecto,
)

__all__ = ["MODELO_POR_DEFECTO", "VAR_API_KEY", "VAR_MODELO", "GeminiProvider"]

_LOG = logging.getLogger(__name__)

VAR_API_KEY = "GEMINI_API_KEY"
VAR_MODELO = "GEMINI_MODEL"

#: Valor por defecto **configurable**, nunca un id incrustado en la lógica.
#: **[POR VALIDAR]** confirme el identificador vigente en la documentación de Google
#: (``ai.google.dev`` / ``models.list``) antes de usarlo: la familia y el sufijo de
#: versión cambian con frecuencia y un id caducado devuelve 404 NOT_FOUND.
MODELO_POR_DEFECTO = "gemini-2.5-flash"

#: Recorte defensivo de vallas de markdown si el modelo ignora ``response_mime_type``.
_VALLA_MARKDOWN = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class GeminiProvider:
    """Proveedor sobre la API de Google Gemini. Cumple :class:`ProveedorLLM`."""

    nombre = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        modelo: str | None = None,
        timeout_s: float | None = None,
        cliente: Any | None = None,
        **_opciones: Any,
    ) -> None:
        """Crea el proveedor y el cliente del SDK.

        Args:
            api_key: credencial; por defecto ``GEMINI_API_KEY``.
            modelo: id del modelo; por defecto ``GEMINI_MODEL`` o
                :data:`MODELO_POR_DEFECTO`.
            timeout_s: timeout por llamada; por defecto ``LLM_TIMEOUT_S``.
            cliente: cliente ya construido (inyección para pruebas). Si se pasa, no
                se importa el SDK ni se exige la credencial.

        Raises:
            ErrorConfiguracionProveedor: si falta la credencial o el SDK.
        """
        self.modelo = (modelo or os.getenv(VAR_MODELO) or MODELO_POR_DEFECTO).strip()
        self.timeout_s = float(timeout_s if timeout_s is not None else timeout_por_defecto())
        self.version_modelo = f"gemini:{self.modelo}"
        self._api_key = (api_key or os.getenv(VAR_API_KEY) or "").strip()
        self._types: Any = None

        if cliente is not None:
            self._cliente = cliente
            return
        if not self._api_key:
            raise ErrorConfiguracionProveedor(
                f"falta {VAR_API_KEY}: use LLM_MODE=mock o configure la credencial",
                proveedor=self.nombre,
            )
        self._cliente = self._crear_cliente()

    # ------------------------------------------------------------------ #
    # Construcción del cliente
    # ------------------------------------------------------------------ #
    def _crear_cliente(self) -> Any:
        """Importa el SDK de forma diferida y construye el cliente con timeout."""
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai import types  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ErrorConfiguracionProveedor(
                "el paquete 'google-genai' no está instalado; use LLM_MODE=mock",
                proveedor=self.nombre,
            ) from exc

        self._types = types
        try:
            # El SDK expresa el timeout en milisegundos.
            opciones = types.HttpOptions(timeout=int(self.timeout_s * 1000))
            return genai.Client(api_key=self._api_key, http_options=opciones)
        except TypeError:  # pragma: no cover - versiones antiguas del SDK
            _LOG.warning("HttpOptions(timeout=...) no admitido por esta versión de google-genai")
            return genai.Client(api_key=self._api_key)
        except Exception as exc:  # pragma: no cover - fallo de construcción
            raise ErrorConfiguracionProveedor(
                f"no se pudo crear el cliente de Gemini: {exc}", proveedor=self.nombre
            ) from exc

    #: Deadline mínimo que acepta la API de Gemini. Por debajo devuelve
    #: ``400 INVALID_ARGUMENT`` antes de generar nada, así que un timeout más corto
    #: no protege: solo garantiza que la llamada falle siempre. El proveedor conoce
    #: esta restricción de su propia API y la hace respetar, en vez de dejar que
    #: cada llamador la descubra en producción.
    DEADLINE_MINIMO_S: float = 10.0

    def _deadline(self, timeout_s: float) -> float:
        """Eleva el timeout al mínimo que admite la API, avisando una sola vez."""
        efectivo = float(timeout_s)
        if efectivo < self.DEADLINE_MINIMO_S:
            if not getattr(self, "_aviso_deadline", False):
                _LOG.warning(
                    "timeout de %.1f s por debajo del mínimo de Gemini (%.0f s); se eleva",
                    efectivo,
                    self.DEADLINE_MINIMO_S,
                )
                self._aviso_deadline = True
            return self.DEADLINE_MINIMO_S
        return efectivo

    def _configuracion(self, esquema: dict, timeout_s: float) -> Any:
        """Construye ``GenerateContentConfig`` con salida JSON estructurada.

        Se intenta primero ``response_json_schema`` (JSON Schema completo) y, si la
        versión instalada del SDK no lo admite, ``response_schema``. Si ninguna de las
        dos existe se degrada a ``application/json`` sin esquema: la validación dura
        la hace igualmente :class:`ExplicacionLLM`.
        """
        if self._types is None:  # cliente inyectado en pruebas
            return {
                "temperature": 0.0,
                "candidate_count": 1,
                "response_mime_type": "application/json",
                "response_json_schema": esquema,
            }

        comun: dict[str, Any] = {
            "temperature": 0.0,
            "candidate_count": 1,
            "response_mime_type": "application/json",
            # Los modelos con razonamiento gastan tokens de pensamiento contra este
            # mismo presupuesto. Con 2048 el JSON llegaba TRUNCADO —"Unterminated
            # string"— y el sistema degradaba a plantilla creyendo que el modelo
            # había devuelto basura. Aquí no hay nada que razonar: los hechos ya
            # vienen calculados y el modelo solo redacta.
            "max_output_tokens": 8192,
        }
        # Desactivar el pensamiento donde el SDK **y el modelo** lo permitan: ahorra
        # latencia y evita que se coma el presupuesto de salida. Si la versión no lo
        # admite, basta con el límite ampliado de arriba; si lo admite el SDK pero lo
        # rechaza el modelo, se descubre en la primera llamada y no se vuelve a enviar
        # (ver `_es_pensamiento_no_desactivable`).
        if not getattr(self, "_sin_pensamiento_configurable", False):
            with suppress(Exception):  # pragma: no cover - depende de la versión
                comun["thinking_config"] = self._types.ThinkingConfig(thinking_budget=0)
        # El timeout por llamada es opcional según la versión del SDK.
        with suppress(Exception):  # pragma: no cover - depende de la versión
            comun["http_options"] = self._types.HttpOptions(
                timeout=int(self._deadline(timeout_s) * 1000)
            )

        for clave in ("response_json_schema", "response_schema"):
            try:
                return self._types.GenerateContentConfig(**comun, **{clave: esquema})
            except Exception:  # se prueba la siguiente forma admitida por el SDK
                continue
        _LOG.warning("el SDK no admite esquema de respuesta; se valida solo con ExplicacionLLM")
        return self._types.GenerateContentConfig(**comun)

    # ------------------------------------------------------------------ #
    # Contrato ProveedorLLM
    # ------------------------------------------------------------------ #
    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        """Llama al modelo y devuelve la explicación estructurada ya validada.

        Raises:
            ErrorTiempoAgotado, ErrorCuota, ErrorConfiguracionProveedor,
            ErrorRespuestaInvalida: subclases de ``ErrorProveedor``; el generador las
            captura y degrada a plantilla determinística.
        """
        efectivo = float(timeout_s or self.timeout_s)
        try:
            respuesta = self._cliente.models.generate_content(
                model=self.modelo,
                contents=prompt,
                config=self._configuracion(esquema, efectivo),
            )
        except Exception as exc:
            if self._es_pensamiento_no_desactivable(exc):
                # Reintento sin `thinking_config`. Los modelos de la generación 3 no
                # admiten `thinking_budget=0` y responden 400 INVALID_ARGUMENT, así que
                # pedir un modelo nuevo dejaba al sistema degradando a plantilla en TODOS
                # los turnos sin decir por qué: el error solo dice «argumento inválido»,
                # sin nombrar el argumento. El `suppress` de `_configuracion` no cubría
                # esto porque construir la opción funciona; lo que falla es enviarla.
                #
                # Se reintenta una vez y sin conmutador de configuración: qué modelos
                # aceptan desactivar el pensamiento cambia con el catálogo de Google, y
                # una tabla de capacidades caducada miente peor que no tenerla.
                self._sin_pensamiento_configurable = True
                _LOG.info(
                    "%s no admite desactivar el pensamiento; se reintenta sin esa opción",
                    self.modelo,
                )
                try:
                    respuesta = self._cliente.models.generate_content(
                        model=self.modelo,
                        contents=prompt,
                        config=self._configuracion(esquema, efectivo),
                    )
                except Exception as reintento:
                    raise self._traducir_error(reintento) from reintento
            else:
                raise self._traducir_error(exc) from exc

        return self._parsear(respuesta, esquema)

    def _es_pensamiento_no_desactivable(self, error: Exception) -> bool:
        """``True`` si el 400 se debe a haber pedido ``thinking_budget=0``."""
        if getattr(self, "_sin_pensamiento_configurable", False):
            return False  # ya se sabe: la opción no se envió, el fallo es otro
        texto = str(error)
        return "INVALID_ARGUMENT" in texto or "400" in texto

    # ------------------------------------------------------------------ #
    # Interno
    # ------------------------------------------------------------------ #
    def _parsear(self, respuesta: Any, esquema: dict | None = None) -> dict:
        """Extrae el JSON y lo valida contra el esquema que pidió el llamador.

        El proveedor sirve a dos consumidores con contratos distintos: la explicación
        del recibo (``explicacion_v1``) y los turnos conversacionales sin cifras.
        Validar siempre contra el primero rechazaba los segundos, y el sistema
        degradaba a plantilla creyendo que el modelo había fallado.
        """
        texto = getattr(respuesta, "text", None)
        if not texto:
            motivo = getattr(respuesta, "prompt_feedback", None)
            raise ErrorRespuestaInvalida(
                f"respuesta vacía del modelo (posible bloqueo de seguridad): {motivo}",
                proveedor=self.nombre,
            )
        limpio = _VALLA_MARKDOWN.sub("", str(texto)).strip()
        try:
            datos = json.loads(limpio)
        except json.JSONDecodeError as exc:
            raise ErrorRespuestaInvalida(
                f"la respuesta del modelo no es JSON válido: {exc}", proveedor=self.nombre
            ) from exc
        requeridos = set((esquema or {}).get("required") or ())
        # Solo se valida con el modelo fuerte cuando el llamador pidió `explicacion_v1`.
        # Se reconoce por sus campos obligatorios, no por una bandera aparte.
        if not requeridos or {"resumen", "causas"} & requeridos:
            try:
                explicacion = ExplicacionLLM.model_validate(datos)
            except Exception as exc:
                raise ErrorRespuestaInvalida(
                    f"la respuesta no cumple explicacion_v1: {exc}", proveedor=self.nombre
                ) from exc
            return explicacion.model_dump(mode="json")

        if not isinstance(datos, dict):
            raise ErrorRespuestaInvalida(
                f"se esperaba un objeto JSON y llegó {type(datos).__name__}",
                proveedor=self.nombre,
            )
        faltan = requeridos - set(datos)
        if faltan:
            raise ErrorRespuestaInvalida(
                f"faltan campos obligatorios del esquema pedido: {sorted(faltan)}",
                proveedor=self.nombre,
            )
        return datos

    def _traducir_error(self, exc: Exception) -> ErrorProveedor:
        """Clasifica el fallo del SDK en la jerarquía tipada del proyecto."""
        if isinstance(exc, ErrorProveedor):
            return exc
        detalle = f"{type(exc).__name__}: {exc}"
        texto = detalle.lower()

        if any(clave in texto for clave in ("timeout", "timed out", "deadline")):
            return ErrorTiempoAgotado(detalle, proveedor=self.nombre)
        if any(
            clave in texto
            for clave in ("429", "resource_exhausted", "quota", "rate limit", "too many requests")
        ):
            return ErrorCuota(detalle, proveedor=self.nombre)
        if any(
            clave in texto
            for clave in (
                "api key",
                "unauthenticated",
                "permission",
                "401",
                "403",
                "invalid_argument",
            )
        ):
            return ErrorConfiguracionProveedor(detalle, proveedor=self.nombre)
        return ErrorProveedor(detalle, proveedor=self.nombre, reintentable=True)
