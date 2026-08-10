"""``LangChainProvider``: cualquier modelo de LangChain como proveedor de recibo-claro.

Es una pieza **aditiva**: ``mock`` sigue siendo el modo por defecto y ``gemini`` sigue
siendo el camino con SDK propio. Este adaptador abre la puerta a OpenAI, Anthropic,
Ollama, Groq, Mistral, Bedrock… sin tocar el generador, el verificador ni el motor de
hechos: se limita a cumplir :class:`~packages.llm_layer.providers.base.ProveedorLLM`
—un atributo ``nombre`` y un método ``completar(prompt, esquema, timeout_s) -> dict``—
y a traducir los fallos ajenos a la jerarquía tipada del proyecto.

Licencias
---------
Solo se usa **langchain-core** (MIT). No se importa ``langgraph-api``,
``langgraph-cli`` ni ``langsmith``: el primero está bajo Elastic License 2.0 y los
demás son servicio propietario, y la cláusula 9 de las bases cede la propiedad
intelectual a Integratel. Una dependencia con licencia restringida impediría a
Integratel desplegar la solución sin negociar con un tercero.

La telemetría de LangSmith se apaga por entorno (``LANGSMITH_TRACING=false`` y
compañía, ver ``.env.example``). Este módulo **no** la enciende ni la consulta.

Configuración por entorno
-------------------------
==============================  ===============================================
``LLM_MODE=langchain``          activa este proveedor.
``LLM_LANGCHAIN_MODELO``        ``"proveedor:modelo"`` (p. ej. ``openai:gpt-4o-mini``,
                                ``google_genai:gemini-2.5-flash``, ``ollama:llama3.1``)
                                o solo el id del modelo si se fija el proveedor aparte.
``LLM_LANGCHAIN_PROVEEDOR``     proveedor, si no va como prefijo del modelo.
``LLM_LANGCHAIN_API_KEY``       credencial. Si está vacía, la integración leerá la suya
                                (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``…).
``LLM_LANGCHAIN_MAX_TOKENS``    presupuesto de salida (por defecto 8192). Ver más abajo.
``LLM_LANGCHAIN_METODO``        ``function_calling`` | ``json_schema`` | ``json_mode`` |
                                ``texto``. Vacío = lo que decida la integración.
``LLM_LANGCHAIN_EXTRA``         JSON con kwargs extra del constructor del chat model.
``LLM_TIMEOUT_S``               tiempo máximo por llamada; al agotarse se cae a plantilla.
==============================  ===============================================

**Prioridad de la configuración.** ``apps/api/deps.obtener_proveedor_llm()`` reenvía a
la fábrica ``api_key=GEMINI_API_KEY`` y ``modelo=GEMINI_MODEL`` para *todos* los modos,
porque esos son los ajustes que existen hoy. Por eso, y solo por eso, aquí manda la
variable dedicada: ``LLM_LANGCHAIN_MODELO`` y ``LLM_LANGCHAIN_API_KEY`` ganan sobre los
argumentos ``modelo=`` y ``api_key=``. Sin esa regla, un ``GEMINI_MODEL`` heredado del
``.env`` pisaba en silencio al modelo configurado y el adaptador acababa pidiéndole un
id de Google a un proveedor vacío. Con ``google_genai:…`` el reenvío de
``GEMINI_API_KEY`` sigue siendo justo lo que se quiere. El descarte se anota en el log.

Modelos que razonan: la trampa que ya nos costó un día
------------------------------------------------------
Es el mismo aprendizaje que está escrito en ``gemini.py``. Cuando el modelo subyacente
tiene *razonamiento* (Gemini 2.5 con ``thinking``, o3/GPT-5 con ``reasoning``, Claude
con *extended thinking*), los tokens de pensamiento se descuentan del **mismo**
presupuesto de salida. Con un ``max_tokens`` corto el JSON llega **truncado**
—``Unterminated string``— y el sistema degrada a plantilla creyendo que el modelo
devolvió basura. Aquí no hay nada que razonar: los hechos ya vienen calculados y el
modelo solo redacta. Dos palancas, ambas por configuración:

1. ``LLM_LANGCHAIN_MAX_TOKENS=8192`` (valor por defecto de este módulo). Es el
   parámetro estándar de langchain-core y lo entienden todas las integraciones.
2. ``LLM_LANGCHAIN_EXTRA``, para apagar el razonamiento donde la integración lo
   permita. El nombre del parámetro es de cada proveedor, no de LangChain::

       # Gemini vía langchain-google-genai
       LLM_LANGCHAIN_EXTRA={"thinking_budget": 0}
       # OpenAI con modelos de razonamiento
       LLM_LANGCHAIN_EXTRA={"reasoning_effort": "minimal"}
       # Anthropic: basta con no activar extended thinking
       LLM_LANGCHAIN_EXTRA={}

   Si la integración no admite la clave, el constructor la descarta con un aviso en el
   log en vez de reventar (ver :meth:`LangChainProvider._instanciar`).

Salida estructurada
-------------------
Se usa ``BaseChatModel.with_structured_output(esquema)`` de langchain-core con **el
esquema que recibe** ``completar``, nunca uno fijo. Este proveedor sirve a dos
consumidores con contratos distintos —``explicacion_v1`` (``ESQUEMA_EXPLICACION_V1``) y
el turno conversacional (``ESQUEMA_CONVERSACIONAL``, un único campo ``respuesta``)— y
validar siempre contra el primero rechazaba los segundos: el sistema degradaba a
plantilla creyendo que el modelo había fallado. La validación replica exactamente la de
``gemini.py`` para que cambiar ``LLM_MODE`` no cambie el comportamiento.

Si el modelo no implementa salida estructurada (no expone ``bind_tools``), se degrada a
**modo texto**: se le pide el JSON en el propio mensaje, con el esquema incrustado, y se
parsea con ``langchain_core.utils.json.parse_json_markdown``. Un modelo pequeño de
Ollama entra por esta puerta.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import types
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

__all__ = [
    "CATALOGO_INTEGRACIONES",
    "MARGEN_GUARDIA_S",
    "MAX_TOKENS_POR_DEFECTO",
    "VAR_API_KEY",
    "VAR_EXTRA",
    "VAR_MAX_TOKENS",
    "VAR_METODO",
    "VAR_MODELO",
    "VAR_PROVEEDOR",
    "LangChainProvider",
    "normalizar_esquema",
]

_LOG = logging.getLogger(__name__)

VAR_MODELO = "LLM_LANGCHAIN_MODELO"
VAR_PROVEEDOR = "LLM_LANGCHAIN_PROVEEDOR"
VAR_API_KEY = "LLM_LANGCHAIN_API_KEY"
VAR_MAX_TOKENS = "LLM_LANGCHAIN_MAX_TOKENS"
VAR_METODO = "LLM_LANGCHAIN_METODO"
VAR_EXTRA = "LLM_LANGCHAIN_EXTRA"

#: Presupuesto de salida por defecto. Generoso a propósito: ver la nota sobre modelos
#: con razonamiento en el docstring del módulo.
MAX_TOKENS_POR_DEFECTO = 8192

#: Margen que se le concede al timeout propio de la integración antes de que salte el
#: guardián de este módulo. El guardián es una red de seguridad, no el mecanismo
#: principal: si salta, es que la integración ignoró su propio ``timeout``.
MARGEN_GUARDIA_S = 2.0

#: Valor de ``LLM_LANGCHAIN_METODO`` que fuerza el modo texto y salta la salida
#: estructurada. Útil con modelos locales que anuncian ``bind_tools`` y no lo cumplen.
METODO_TEXTO = "texto"

#: Puntos de entrada documentados de cada paquete de integración. **No se importa
#: ninguno al cargar este módulo**: el import es diferido y su ausencia se convierte en
#: :class:`ErrorConfiguracionProveedor` con el ``pip install`` exacto que falta, nunca en
#: un ``ImportError`` que tumbe la API. Si una integración renombra su clase, el error
#: dice qué módulo y qué clase se intentaron; además ``ruta_clase=`` permite apuntar a
#: cualquier otra sin tocar este catálogo.
CATALOGO_INTEGRACIONES: dict[str, tuple[str, str]] = {
    "openai": ("langchain_openai", "ChatOpenAI"),
    "azure_openai": ("langchain_openai", "AzureChatOpenAI"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic"),
    "google_genai": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
    "google_vertexai": ("langchain_google_vertexai", "ChatVertexAI"),
    "groq": ("langchain_groq", "ChatGroq"),
    "mistralai": ("langchain_mistralai", "ChatMistralAI"),
    "ollama": ("langchain_ollama", "ChatOllama"),
    "deepseek": ("langchain_deepseek", "ChatDeepSeek"),
    "fireworks": ("langchain_fireworks", "ChatFireworks"),
    "together": ("langchain_together", "ChatTogether"),
    "xai": ("langchain_xai", "ChatXAI"),
    "bedrock": ("langchain_aws", "ChatBedrockConverse"),
    "huggingface": ("langchain_huggingface", "ChatHuggingFace"),
}

#: Claves que algunos proveedores meten en su JSON Schema y que otros rechazan al
#: validar en modo estricto. ``propertyOrdering`` es de Google y viaja dentro de
#: ``ESQUEMA_EXPLICACION_V1``; se poda antes de dárselo a LangChain.
_CLAVES_NO_ESTANDAR = ("propertyOrdering",)

#: Nombre de función que se le pone a un esquema sin ``title``.
#: ``convert_to_openai_tool`` exige un ``title`` de primer nivel para usarlo como
#: nombre de la herramienta, y ``ESQUEMA_CONVERSACIONAL`` no lo trae.
TITULO_POR_DEFECTO = "salida_estructurada"

_INSTRUCCION_JSON = (
    "Responde ÚNICAMENTE con un objeto JSON válido que cumpla este JSON Schema.\n"
    "Sin markdown, sin vallas de código, sin texto antes ni después.\n\n{esquema}"
)


# --------------------------------------------------------------------------- #
# Esquema: normalización
# --------------------------------------------------------------------------- #
def normalizar_esquema(esquema: dict, *, titulo: str = TITULO_POR_DEFECTO) -> dict:
    """Devuelve una copia del esquema apta para ``with_structured_output``.

    Dos ajustes, ninguno de ellos semántico:

    * Se garantiza un ``title`` de primer nivel; sin él ``convert_to_openai_tool``
      levanta ``ValueError: Unsupported function`` y el turno se perdería.
    * Se podan las claves propietarias (:data:`_CLAVES_NO_ESTANDAR`) en todos los
      niveles, porque son extensiones de un proveedor concreto.

    El esquema original **no se toca**: la validación posterior se hace contra él.
    """
    if not isinstance(esquema, dict):
        raise ErrorRespuestaInvalida(
            f"el esquema debe ser un dict y llegó {type(esquema).__name__}",
            proveedor=LangChainProvider.nombre,
        )
    limpio = _podar(esquema)
    if not str(limpio.get("title") or "").strip():
        limpio["title"] = titulo
    return limpio


def _podar(nodo: Any) -> Any:
    """Copia recursiva sin las claves propietarias de :data:`_CLAVES_NO_ESTANDAR`."""
    if isinstance(nodo, dict):
        return {c: _podar(v) for c, v in nodo.items() if c not in _CLAVES_NO_ESTANDAR}
    if isinstance(nodo, list):
        return [_podar(v) for v in nodo]
    return nodo


# --------------------------------------------------------------------------- #
# Proveedor
# --------------------------------------------------------------------------- #
class LangChainProvider:
    """Adaptador de un ``BaseChatModel`` de LangChain. Cumple :class:`ProveedorLLM`."""

    nombre = "langchain"

    def __init__(
        self,
        *,
        chat: Any | None = None,
        modelo: str | None = None,
        proveedor: str | None = None,
        ruta_clase: str | None = None,
        api_key: str | None = None,
        timeout_s: float | None = None,
        max_tokens: int | None = None,
        temperatura: float = 0.0,
        metodo: str | None = None,
        extra_modelo: dict[str, Any] | None = None,
        guardia: bool = True,
        **_opciones: Any,
    ) -> None:
        """Crea el proveedor.

        Args:
            chat: ``BaseChatModel`` **ya construido**. Si se pasa, se respeta tal cual:
                ni se le cambia la temperatura ni el presupuesto de salida, porque lo
                configuró quien lo construyó. Es también la vía de inyección en pruebas.
            modelo: ``"proveedor:modelo"`` o solo el id del modelo. **``LLM_LANGCHAIN_MODELO``
                tiene prioridad sobre este argumento**; ver la nota de prioridad en el
                docstring del módulo.
            proveedor: clave de :data:`CATALOGO_INTEGRACIONES` si no va como prefijo de
                ``modelo``; ``LLM_LANGCHAIN_PROVEEDOR`` tiene prioridad.
            ruta_clase: escotilla ``"paquete.modulo:Clase"`` para una integración que no
                esté en el catálogo o que haya renombrado su clase.
            api_key: credencial; ``LLM_LANGCHAIN_API_KEY`` tiene prioridad. Si ambas
                quedan vacías, la integración lee la suya del entorno.
            timeout_s: timeout por llamada; por defecto ``LLM_TIMEOUT_S``.
            max_tokens: presupuesto de salida; por defecto ``LLM_LANGCHAIN_MAX_TOKENS``
                o :data:`MAX_TOKENS_POR_DEFECTO`.
            temperatura: 0.0 para que la demo sea reproducible.
            metodo: ``method`` de ``with_structured_output``, o ``"texto"`` para forzar
                el modo texto; por defecto ``LLM_LANGCHAIN_METODO``.
            extra_modelo: kwargs extra del constructor del chat model; por defecto
                ``LLM_LANGCHAIN_EXTRA`` (JSON).
            guardia: si es ``True``, un hilo vigilante corta la llamada al superarse
                ``timeout_s + MARGEN_GUARDIA_S`` aunque la integración ignore su timeout.

        Raises:
            ErrorConfiguracionProveedor: si no hay ni ``chat`` ni ``modelo``, o si el
                valor de ``LLM_LANGCHAIN_EXTRA`` no es un objeto JSON.

        Nota:
            El chat model **no se construye aquí**. Si el paquete de integración falta,
            el fallo aparece en la primera llamada a :meth:`completar`, ya traducido a
            ``ErrorProveedor``, y el generador degrada a plantilla. Construirlo en el
            constructor haría que ``obtener_proveedor_llm()`` devolviera ``None`` y la
            API arrancaría sin decir por qué.
        """
        self.timeout_s = float(timeout_s if timeout_s is not None else timeout_por_defecto())
        self.temperatura = float(temperatura)
        self.guardia = bool(guardia)
        self.max_tokens = int(max_tokens if max_tokens is not None else _max_tokens_entorno())
        self.metodo = (metodo or os.getenv(VAR_METODO) or "").strip().lower() or None
        self.extra_modelo = (
            dict(extra_modelo) if extra_modelo is not None else _extra_entorno(self.nombre)
        )
        self.ruta_clase = (ruta_clase or "").strip() or None

        self._api_key = (api_key or "").strip()
        entorno_key = (os.getenv(VAR_API_KEY) or "").strip()
        if entorno_key:
            # La variable propia manda sobre el reenvío de `GEMINI_API_KEY` que hace
            # `deps.obtener_proveedor_llm()`; ver el aviso del docstring del módulo.
            self._api_key = entorno_key

        self._chat = chat
        self._cache_runnable: dict[str, tuple[Any, bool]] = {}
        self._aviso_sin_estructurado = False

        # La variable propia manda sobre el argumento, igual que con la credencial. No
        # es capricho: `deps.obtener_proveedor_llm()` reenvía `GEMINI_MODEL` como
        # `modelo=` para todos los modos, así que un `GEMINI_MODEL` heredado del `.env`
        # pisaba en silencio a `LLM_LANGCHAIN_MODELO` y el adaptador acababa pidiendo un
        # modelo de Google a un proveedor vacío. Aquí el ajuste dedicado es la verdad.
        entorno_modelo = (os.getenv(VAR_MODELO) or "").strip()
        argumento_modelo = (modelo or "").strip()
        if entorno_modelo and argumento_modelo and entorno_modelo != argumento_modelo:
            _LOG.info(
                "se ignora modelo=%r: %s=%r tiene prioridad",
                argumento_modelo,
                VAR_MODELO,
                entorno_modelo,
            )
        bruto = entorno_modelo or argumento_modelo
        entorno_proveedor = (os.getenv(VAR_PROVEEDOR) or "").strip().lower()
        self.proveedor_modelo = entorno_proveedor or (proveedor or "").strip().lower()
        self.modelo = bruto
        if ":" in bruto and not self.proveedor_modelo:
            self.proveedor_modelo, _, self.modelo = (p.strip() for p in bruto.partition(":"))
            self.proveedor_modelo = self.proveedor_modelo.lower()
        elif ":" in bruto:
            self.modelo = bruto.partition(":")[2].strip()

        if chat is None and not self.modelo and not self.ruta_clase:
            raise ErrorConfiguracionProveedor(
                f"falta {VAR_MODELO}: use LLM_MODE=mock o configure, por ejemplo, "
                f"{VAR_MODELO}=openai:gpt-4o-mini",
                proveedor=self.nombre,
            )

        self.version_modelo = self._describir_version()

    # ------------------------------------------------------------------ #
    # Identidad
    # ------------------------------------------------------------------ #
    def _describir_version(self) -> str:
        """Cadena para ``Gobernanza.model_version``: qué modelo redactó, exactamente."""
        if self._chat is not None:
            del_chat = (
                getattr(self._chat, "model_name", None)
                or getattr(self._chat, "model", None)
                or getattr(self._chat, "model_id", None)
            )
            etiqueta = str(del_chat) if del_chat else type(self._chat).__name__
            return f"langchain:{etiqueta}"
        if self.proveedor_modelo:
            return f"langchain:{self.proveedor_modelo}:{self.modelo}"
        return f"langchain:{self.modelo or self.ruta_clase}"

    # ------------------------------------------------------------------ #
    # Contrato ProveedorLLM
    # ------------------------------------------------------------------ #
    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        """Llama al modelo y devuelve un ``dict`` conforme al ``esquema`` recibido.

        Raises:
            ErrorTiempoAgotado, ErrorCuota, ErrorConfiguracionProveedor,
            ErrorRespuestaInvalida: subclases de ``ErrorProveedor``; el generador las
            captura y degrada a la plantilla determinística, igual que con Gemini.
        """
        efectivo = float(timeout_s or self.timeout_s)
        normalizado = normalizar_esquema(esquema)
        try:
            chat = self._chat_activo()
            runnable, estructurado = self._runnable(chat, normalizado)
            entrada = prompt if estructurado else self._mensajes(prompt, normalizado)
            salida = self._con_guardia(lambda: runnable.invoke(entrada), efectivo)
            crudo = salida if estructurado else self._json_de_mensaje(salida)
        except ErrorProveedor:
            raise
        except Exception as exc:
            raise self._traducir_error(exc) from exc

        return self._validar(crudo, esquema)

    # ------------------------------------------------------------------ #
    # Construcción diferida del chat model
    # ------------------------------------------------------------------ #
    def _chat_activo(self) -> Any:
        """Devuelve el chat model, construyéndolo la primera vez si hace falta."""
        if self._chat is None:
            self._chat = self._construir_chat()
            self.version_modelo = self._describir_version()
        return self._chat

    def _construir_chat(self) -> Any:
        """Importa la integración de forma **diferida** y construye el chat model.

        Orden de resolución: ``ruta_clase`` explícita → :data:`CATALOGO_INTEGRACIONES`
        → ``langchain.chat_models.init_chat_model`` si el paquete ``langchain`` está
        instalado. Cualquier ausencia se traduce a :class:`ErrorConfiguracionProveedor`
        con el ``pip install`` concreto: nunca un ``ImportError`` suelto.
        """
        self._exigir_langchain_core()

        if self.ruta_clase:
            modulo, _, clase = self.ruta_clase.partition(":")
            return self._instanciar(self._importar(modulo.strip(), clase.strip()))

        destino = CATALOGO_INTEGRACIONES.get(self.proveedor_modelo)
        if destino is not None:
            return self._instanciar(self._importar(*destino))

        return self._por_init_chat_model()

    @staticmethod
    def _exigir_langchain_core() -> None:
        """Comprueba que ``langchain-core`` (MIT) está disponible."""
        from importlib.util import find_spec

        try:
            presente = find_spec("langchain_core") is not None
        except (ImportError, ValueError):  # pragma: no cover - entorno roto
            presente = False
        if not presente:
            raise ErrorConfiguracionProveedor(
                "el paquete 'langchain-core' no está instalado "
                "(pip install 'langchain-core>=1.5,<2.0'); use LLM_MODE=mock",
                proveedor=LangChainProvider.nombre,
            )

    def _importar(self, modulo: str, clase: str) -> Any:
        """Importa ``modulo.clase`` de forma diferida, con un error accionable."""
        from importlib import import_module

        try:
            paquete = import_module(modulo)
        except ImportError as exc:
            raise ErrorConfiguracionProveedor(
                f"el paquete de integración '{modulo}' no está instalado "
                f"(pip install {modulo.replace('_', '-')}); use LLM_MODE=mock",
                proveedor=self.nombre,
            ) from exc
        objetivo = getattr(paquete, clase, None)
        if objetivo is None:
            raise ErrorConfiguracionProveedor(
                f"'{modulo}' no expone '{clase}'; indique la clase con ruta_clase="
                f"'{modulo}:OtraClase' o revise la versión instalada",
                proveedor=self.nombre,
            )
        return objetivo

    def _por_init_chat_model(self) -> Any:
        """Escotilla genérica: la fábrica ``init_chat_model`` del paquete ``langchain``.

        Solo se usa si el proveedor no está en el catálogo. Si ``langchain`` no está
        instalado —el caso normal en este proyecto, donde solo se depende de
        ``langchain-core``— el error enumera los proveedores conocidos.
        """
        try:
            from langchain.chat_models import init_chat_model  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ErrorConfiguracionProveedor(
                f"proveedor {self.proveedor_modelo!r} desconocido; use uno de "
                f"{', '.join(sorted(CATALOGO_INTEGRACIONES))}, o indique "
                f"ruta_clase='paquete.modulo:Clase'",
                proveedor=self.nombre,
            ) from exc

        opciones = self._kwargs_modelo()
        opciones.pop("model", None)
        if self.proveedor_modelo:
            opciones["model_provider"] = self.proveedor_modelo
        try:
            return init_chat_model(self.modelo, **opciones)
        except Exception as exc:  # pragma: no cover - depende del paquete instalado
            raise ErrorConfiguracionProveedor(
                f"init_chat_model no pudo construir {self.modelo!r}: {exc}",
                proveedor=self.nombre,
            ) from exc

    def _kwargs_modelo(self) -> dict[str, Any]:
        """Parámetros del constructor, en el orden en que se irán descartando."""
        opciones: dict[str, Any] = {"model": self.modelo}
        if self._api_key:
            opciones["api_key"] = self._api_key
        opciones["temperature"] = self.temperatura
        opciones["timeout"] = self.timeout_s
        opciones["max_tokens"] = self.max_tokens
        opciones.update(self.extra_modelo)
        return opciones

    def _instanciar(self, clase: Any) -> Any:
        """Instancia la clase descartando los parámetros que no admita.

        Las integraciones no comparten firma: unas llaman ``timeout`` a lo que otras
        llaman ``request_timeout``, y ``max_tokens`` no existe en todas. En vez de
        exigir al operador que acierte, se prueba con todo y se van soltando los
        parámetros opcionales —los extras primero, ``temperature`` el último— dejando
        constancia en el log. Lo esencial (``model`` y ``api_key``) nunca se descarta:
        si eso falla, es un error de configuración de verdad.
        """
        opciones = self._kwargs_modelo()
        descartables = [*self.extra_modelo, "max_tokens", "timeout", "temperature"]
        ultimo: Exception | None = None
        while True:
            try:
                return clase(**opciones)
            except TypeError as exc:
                ultimo = exc
                if not descartables:
                    break
                sobra = descartables.pop(0)
                if sobra not in opciones:
                    continue
                opciones.pop(sobra)
                _LOG.warning(
                    "%s no admite %r (%s); se construye sin ese parámetro",
                    getattr(clase, "__name__", clase),
                    sobra,
                    exc,
                )
            except Exception as exc:
                raise ErrorConfiguracionProveedor(
                    f"no se pudo construir {getattr(clase, '__name__', clase)}: {exc}",
                    proveedor=self.nombre,
                ) from exc
        raise ErrorConfiguracionProveedor(
            f"no se pudo construir {getattr(clase, '__name__', clase)}: {ultimo}",
            proveedor=self.nombre,
        )

    # ------------------------------------------------------------------ #
    # Salida estructurada
    # ------------------------------------------------------------------ #
    def _runnable(self, chat: Any, esquema: dict) -> tuple[Any, bool]:
        """``(runnable, estructurado)`` para ``esquema``, memorizado por esquema.

        El segundo elemento dice si el runnable ya devuelve datos (``True``) o un
        mensaje del que hay que extraer el JSON (``False``).
        """
        clave = json.dumps(esquema, sort_keys=True, ensure_ascii=False)
        en_cache = self._cache_runnable.get(clave)
        if en_cache is not None:
            return en_cache

        par = (chat, False) if self.metodo == METODO_TEXTO else self._estructurar(chat, esquema)
        self._cache_runnable[clave] = par
        return par

    def _estructurar(self, chat: Any, esquema: dict) -> tuple[Any, bool]:
        """Intenta ``with_structured_output`` y degrada a modo texto si no se puede."""
        fabricar = getattr(chat, "with_structured_output", None)
        if fabricar is None:
            return chat, False

        if self.metodo:
            try:
                return fabricar(esquema, method=self.metodo), True
            except Exception as exc:
                _LOG.info("method=%r no admitido (%s); se reintenta sin él", self.metodo, exc)
        try:
            return fabricar(esquema), True
        except NotImplementedError:
            if not self._aviso_sin_estructurado:
                _LOG.info(
                    "%s no implementa salida estructurada; se pide el JSON en el mensaje",
                    type(chat).__name__,
                )
                self._aviso_sin_estructurado = True
            return chat, False
        except Exception as exc:
            _LOG.warning("with_structured_output falló al construirse (%s); se usa modo texto", exc)
            return chat, False

    def _mensajes(self, prompt: str, esquema: dict) -> list[Any]:
        """Mensajes del modo texto: el esquema pedido viaja dentro de la instrucción."""
        from langchain_core.messages import HumanMessage, SystemMessage

        instruccion = _INSTRUCCION_JSON.format(
            esquema=json.dumps(esquema, ensure_ascii=False, indent=2)
        )
        return [SystemMessage(content=instruccion), HumanMessage(content=prompt)]

    def _json_de_mensaje(self, salida: Any) -> Any:
        """Extrae y parsea el JSON de un ``AIMessage`` (modo texto).

        Se usa ``parse_json_markdown`` de langchain-core —que sabe quitar las vallas de
        markdown que ponen algunos modelos— pero con ``parser=json.loads``, es decir,
        **estricto**. Su parser por defecto, ``parse_partial_json``, *repara* el JSON
        incompleto: un texto cortado a media frase se convertiría en un objeto válido y
        el cliente recibiría la respuesta a medias. Preferimos degradar a plantilla.
        """
        from langchain_core.utils.json import parse_json_markdown

        texto = _texto_de_mensaje(salida)
        if not texto:
            raise ErrorRespuestaInvalida(
                "respuesta vacía del modelo (posible bloqueo de seguridad o corte de salida)",
                proveedor=self.nombre,
            )
        try:
            return parse_json_markdown(texto, parser=json.loads)
        except Exception as exc:
            # La causa habitual es un JSON truncado porque los tokens de razonamiento
            # se comieron el presupuesto de salida. Ver el docstring del módulo.
            raise ErrorRespuestaInvalida(
                f"la respuesta del modelo no es JSON válido: {exc}. Si termina a media "
                f"cadena, suba {VAR_MAX_TOKENS} (actual: {self.max_tokens}) o apague el "
                f"razonamiento con {VAR_EXTRA}",
                proveedor=self.nombre,
            ) from exc

    # ------------------------------------------------------------------ #
    # Validación contra el esquema PEDIDO
    # ------------------------------------------------------------------ #
    def _validar(self, crudo: Any, esquema: dict) -> dict:
        """Valida la salida contra el esquema que pidió el llamador.

        Réplica literal del criterio de ``gemini.py``: solo se exige ``explicacion_v1``
        cuando el llamador lo pidió, y eso se reconoce por sus campos obligatorios, no
        por una bandera aparte. Validar siempre contra ``explicacion_v1`` rechazaba los
        turnos conversacionales —que solo traen ``respuesta``— y el sistema degradaba a
        plantilla creyendo que el modelo había fallado. Mantener aquí el **mismo**
        criterio es lo que permite cambiar ``LLM_MODE`` sin cambiar el comportamiento.
        """
        datos = crudo
        if hasattr(datos, "model_dump"):  # el modelo devolvió una instancia pydantic
            datos = datos.model_dump(mode="json")

        requeridos = set((esquema or {}).get("required") or ())
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

    # ------------------------------------------------------------------ #
    # Guardián de tiempo
    # ------------------------------------------------------------------ #
    def _con_guardia(self, funcion: Any, timeout_s: float) -> Any:
        """Ejecuta ``funcion`` con un tope duro de tiempo.

        El mecanismo principal sigue siendo el ``timeout`` que se le pasa a la
        integración. Esto es la red debajo: un cliente HTTP mal configurado no puede
        dejar colgado el hilo del endpoint, porque el presupuesto p95 de la API es de
        toda la petición, no solo del modelo. El hilo es *daemon*: si el proveedor
        nunca responde, el proceso puede terminar igualmente.
        """
        if not self.guardia or timeout_s <= 0:
            return funcion()

        limite = timeout_s + MARGEN_GUARDIA_S
        caja: dict[str, Any] = {}

        def _correr() -> None:
            try:
                caja["valor"] = funcion()
            except BaseException as exc:
                caja["error"] = exc

        hilo = threading.Thread(target=_correr, daemon=True, name="langchain-completar")
        hilo.start()
        hilo.join(limite)
        if hilo.is_alive():
            raise ErrorTiempoAgotado(
                f"el modelo no respondió en {limite:.1f} s (timeout {timeout_s:.1f} s "
                f"+ margen {MARGEN_GUARDIA_S:.1f} s)",
                proveedor=self.nombre,
            )
        if "error" in caja:
            raise caja["error"]
        return caja.get("valor")

    # ------------------------------------------------------------------ #
    # Traducción de errores
    # ------------------------------------------------------------------ #
    def _traducir_error(self, exc: Exception) -> ErrorProveedor:
        """Clasifica el fallo de LangChain o de la integración en la jerarquía tipada.

        Las integraciones levantan las excepciones **nativas** de cada SDK
        (``openai.RateLimitError``, ``httpx.ReadTimeout``, ``google.api_core``…), que no
        se pueden importar sin instalar el SDK. Por eso se clasifica por nombre de clase
        y por texto, igual que en ``gemini.py``. Antes se comprueban los tipos de
        ``langchain_core``, que sí son estables.
        """
        if isinstance(exc, ErrorProveedor):
            return exc

        codigo_core = self._clasificar_por_langchain_core(exc)
        if codigo_core is not None:
            return codigo_core

        detalle = f"{type(exc).__name__}: {exc}"
        texto = detalle.lower()

        if any(c in texto for c in ("timeout", "timed out", "deadline")):
            return ErrorTiempoAgotado(detalle, proveedor=self.nombre)
        if any(
            c in texto
            for c in (
                "429",
                "resource_exhausted",
                "quota",
                "rate limit",
                "ratelimit",
                "too many requests",
                "insufficient_quota",
            )
        ):
            return ErrorCuota(detalle, proveedor=self.nombre)
        if any(
            c in texto
            for c in (
                "api key",
                "api_key",
                "unauthenticated",
                "authentication",
                "permission",
                "401",
                "403",
                "invalid_argument",
                "model_not_found",
            )
        ):
            return ErrorConfiguracionProveedor(detalle, proveedor=self.nombre)
        if any(c in texto for c in ("jsondecodeerror", "outputparser", "validationerror")):
            return ErrorRespuestaInvalida(detalle, proveedor=self.nombre)
        return ErrorProveedor(detalle, proveedor=self.nombre, reintentable=True)

    def _clasificar_por_langchain_core(self, exc: Exception) -> ErrorProveedor | None:
        """Tipos propios de ``langchain_core``; ``None`` si no aplica ninguno.

        El import es diferido y tolerante: si ``langchain_core`` no estuviese, la
        clasificación por texto sigue funcionando.
        """
        try:
            from langchain_core.exceptions import ContextOverflowError, OutputParserException
        except ImportError:  # pragma: no cover - langchain-core siempre está si se llegó aquí
            return None

        detalle = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, OutputParserException):
            return ErrorRespuestaInvalida(
                f"{detalle}. Si el JSON llega truncado, suba {VAR_MAX_TOKENS} "
                f"(actual: {self.max_tokens}) o apague el razonamiento con {VAR_EXTRA}",
                proveedor=self.nombre,
            )
        if isinstance(exc, ContextOverflowError):
            return ErrorProveedor(detalle, proveedor=self.nombre, codigo="CONTEXTO_EXCEDIDO")
        return None


# --------------------------------------------------------------------------- #
# Ayudantes de entorno
# --------------------------------------------------------------------------- #
def _max_tokens_entorno() -> int:
    """``LLM_LANGCHAIN_MAX_TOKENS``; :data:`MAX_TOKENS_POR_DEFECTO` si falta o es basura."""
    bruto = os.getenv(VAR_MAX_TOKENS)
    if not bruto:
        return MAX_TOKENS_POR_DEFECTO
    try:
        valor = int(bruto.strip())
    except ValueError:
        _LOG.warning(
            "%s=%r no es un entero; se usa %d", VAR_MAX_TOKENS, bruto, MAX_TOKENS_POR_DEFECTO
        )
        return MAX_TOKENS_POR_DEFECTO
    return valor if valor > 0 else MAX_TOKENS_POR_DEFECTO


def _extra_entorno(proveedor: str) -> dict[str, Any]:
    """``LLM_LANGCHAIN_EXTRA`` como dict; error explícito si no es un objeto JSON."""
    bruto = (os.getenv(VAR_EXTRA) or "").strip()
    if not bruto:
        return {}
    try:
        valor = json.loads(bruto)
    except json.JSONDecodeError as exc:
        raise ErrorConfiguracionProveedor(
            f"{VAR_EXTRA} no es JSON válido: {exc}", proveedor=proveedor
        ) from exc
    if not isinstance(valor, dict):
        raise ErrorConfiguracionProveedor(
            f"{VAR_EXTRA} debe ser un objeto JSON y llegó {type(valor).__name__}",
            proveedor=proveedor,
        )
    return valor


def _texto_de_mensaje(salida: Any) -> str:
    """Texto plano de un ``AIMessage``, sea ``content`` una cadena o bloques.

    En langchain-core 1.x ``mensaje.text`` es un ``TextAccessor``: se comporta como
    cadena y además es invocable por compatibilidad, pero invocarlo emite un
    ``LangChainDeprecationWarning``. Por eso se distingue por tipo —solo se llama si es
    un método enlazado, que es la forma de langchain-core < 1.0— en vez de por
    ``callable()``.
    """
    if isinstance(salida, str):
        return salida.strip()

    accesor = getattr(salida, "text", None)
    if isinstance(accesor, types.MethodType):  # pragma: no cover - langchain-core < 1.0
        invocado = accesor()
        if isinstance(invocado, str):
            return invocado.strip()
    elif accesor is not None:
        return str(accesor).strip()

    contenido = getattr(salida, "content", None)
    if isinstance(contenido, str):
        return contenido.strip()
    if isinstance(contenido, list):
        trozos = [str(b.get("text", "")) for b in contenido if isinstance(b, dict) and "text" in b]
        return "".join(trozos).strip()
    return ""
