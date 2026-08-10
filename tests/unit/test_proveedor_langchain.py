"""Adaptador de LangChain como proveedor generativo.

Tres cosas se prueban aquí, y son exactamente las tres que pueden costar una demo:

1. **El proveedor honra el esquema que le pasan.** Sirve a dos consumidores con
   contratos distintos —``explicacion_v1`` y el turno conversacional— y validar siempre
   contra el primero rechazaba los segundos: el sistema degradaba a plantilla creyendo
   que el modelo había fallado. Ese error ya nos costó un día con Gemini; aquí queda
   clavado con una prueba.
2. **Todo fallo se traduce a la jerarquía tipada del proyecto**, para que el generador
   degrade a plantilla igual que con Gemini en vez de propagar un 500.
3. **La ausencia del paquete de integración no rompe nada.** El proyecto solo depende
   de ``langchain-core`` (MIT); ``langchain-openai`` y compañía son opcionales.

Los modelos falsos salen de ``langchain_core.language_models.fake_chat_models``: nada
sale a la red. Si ``langchain-core`` no está instalado, el módulo entero se omite.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip(
    "langchain_core",
    reason="langchain-core es una dependencia opcional: pip install -e '.[langchain]'",
)

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from packages.llm_layer.conversacional import ESQUEMA_CONVERSACIONAL
from packages.llm_layer.providers import langchain_ as modulo
from packages.llm_layer.providers.base import (
    ESQUEMA_EXPLICACION_V1,
    MODO_LANGCHAIN,
    MODOS_VALIDOS,
    ErrorConfiguracionProveedor,
    ErrorCuota,
    ErrorProveedor,
    ErrorRespuestaInvalida,
    ErrorTiempoAgotado,
    ProveedorLLM,
    obtener_proveedor,
)
from packages.llm_layer.providers.langchain_ import (
    CATALOGO_INTEGRACIONES,
    LangChainProvider,
    normalizar_esquema,
)

pytestmark = pytest.mark.usefixtures("_entorno_limpio")

#: Salida válida de ``explicacion_v1`` (todas las cifras, enteras y en céntimos).
SALIDA_EXPLICACION: dict[str, Any] = {
    "resumen": "Su recibo subió por el prorrateo del cambio de plan.",
    "causas": [
        {
            "concepto_id": "PLAN_BASE",
            "frase": "El cambio de plan a mitad de ciclo se cobra en dos tramos.",
            "monto_cent_citado": 731,
        }
    ],
    "siguiente_paso": "VER_DETALLE",
    "cifras_usadas": [731],
}

#: Salida válida del turno conversacional: un único campo de texto, sin cifras.
SALIDA_CONVERSACIONAL: dict[str, Any] = {"respuesta": "Hola, ¿en qué le puedo ayudar?"}


@pytest.fixture
def _entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Borra las variables del adaptador para que el entorno real no filtre valores."""
    for variable in (
        modulo.VAR_MODELO,
        modulo.VAR_PROVEEDOR,
        modulo.VAR_API_KEY,
        modulo.VAR_MAX_TOKENS,
        modulo.VAR_METODO,
        modulo.VAR_EXTRA,
    ):
        monkeypatch.delenv(variable, raising=False)


# --------------------------------------------------------------------------- #
# Modelos falsos de langchain-core
# --------------------------------------------------------------------------- #
class ModeloEstructurado(FakeMessagesListChatModel):
    """Modelo falso que **sí** implementa salida estructurada y anota qué esquema recibe."""

    esquemas_vistos: list[dict] = Field(default_factory=list)
    metodos_vistos: list[str | None] = Field(default_factory=list)
    salida: Any = None
    fallo: Any = None

    def with_structured_output(  # type: ignore[override]
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Any:
        self.esquemas_vistos.append(schema)
        self.metodos_vistos.append(kwargs.get("method"))
        return RunnableLambda(self._responder)

    def _responder(self, _entrada: Any) -> Any:
        if self.fallo is not None:
            raise self.fallo
        return self.salida


class ModeloTexto(FakeMessagesListChatModel):
    """Modelo falso sin salida estructurada: obliga al adaptador al modo texto.

    ``FakeMessagesListChatModel`` no implementa ``bind_tools``, así que el
    ``with_structured_output`` heredado de ``BaseChatModel`` levanta
    ``NotImplementedError``. Es el caso de un modelo pequeño servido por Ollama.
    """

    mensajes_vistos: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.mensajes_vistos.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.responses[0])])


class ModeloQueRevienta(FakeMessagesListChatModel):
    """Modelo falso cuyo ``invoke`` levanta la excepción que se le indique."""

    error: Any = None

    def invoke(self, entrada: Any, config: Any = None, **kwargs: Any) -> Any:
        raise self.error


def _proveedor(chat: Any, **opciones: Any) -> LangChainProvider:
    """Adaptador sobre un chat ya construido; el guardián de tiempo se apaga por defecto."""
    opciones.setdefault("guardia", False)
    return LangChainProvider(chat=chat, **opciones)


def _mensajes_vacios() -> list[BaseMessage]:
    return [AIMessage(content="")]


# --------------------------------------------------------------------------- #
# 1. El esquema pedido es el que manda
# --------------------------------------------------------------------------- #
def test_honra_el_esquema_de_explicacion_v1() -> None:
    """Con ``explicacion_v1`` la salida se valida y se normaliza a su ``model_dump``."""
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_EXPLICACION)
    resultado = _proveedor(chat).completar("PROMPT", ESQUEMA_EXPLICACION_V1)

    assert resultado["resumen"] == SALIDA_EXPLICACION["resumen"]
    assert resultado["causas"][0]["monto_cent_citado"] == 731
    assert isinstance(resultado["causas"][0]["monto_cent_citado"], int)
    assert chat.esquemas_vistos[0]["title"] == "explicacion_v1"


def test_honra_el_esquema_conversacional_sin_exigir_explicacion_v1() -> None:
    """El turno conversacional NO se valida contra ``explicacion_v1``.

    Esta es la regresión que costó un día: forzar ``explicacion_v1`` sobre un esquema
    de un solo campo hacía que el turno cayera a plantilla sin motivo.
    """
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_CONVERSACIONAL)
    resultado = _proveedor(chat).completar("PROMPT", ESQUEMA_CONVERSACIONAL)

    assert resultado == SALIDA_CONVERSACIONAL
    assert chat.esquemas_vistos[0]["required"] == ["respuesta"]
    assert "resumen" not in chat.esquemas_vistos[0]["properties"]


def test_el_mismo_proveedor_atiende_los_dos_contratos_seguidos() -> None:
    """Un único proveedor sirve ambos esquemas sin contaminarse entre llamadas."""
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_EXPLICACION)
    proveedor = _proveedor(chat)
    assert proveedor.completar("P", ESQUEMA_EXPLICACION_V1)["causas"][0]["concepto_id"]

    chat.salida = SALIDA_CONVERSACIONAL
    assert proveedor.completar("P", ESQUEMA_CONVERSACIONAL) == SALIDA_CONVERSACIONAL

    titulos = [e["title"] for e in chat.esquemas_vistos]
    assert titulos == ["explicacion_v1", modulo.TITULO_POR_DEFECTO]


def test_rechaza_una_salida_que_no_cumple_el_esquema_pedido() -> None:
    """Si falta un campo obligatorio del esquema recibido, es ``ErrorRespuestaInvalida``."""
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida={"otra_cosa": "x"})
    with pytest.raises(ErrorRespuestaInvalida, match="respuesta"):
        _proveedor(chat).completar("PROMPT", ESQUEMA_CONVERSACIONAL)


def test_rechaza_una_explicacion_con_monto_no_entero() -> None:
    """La regla nº 2: los importes son enteros en céntimos, no cadenas ni decimales."""
    rota = json.loads(json.dumps(SALIDA_EXPLICACION))
    rota["causas"][0]["monto_cent_citado"] = "siete soles con treinta y uno"
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=rota)
    with pytest.raises(ErrorRespuestaInvalida, match="explicacion_v1"):
        _proveedor(chat).completar("PROMPT", ESQUEMA_EXPLICACION_V1)


def test_acepta_una_instancia_pydantic_del_modelo() -> None:
    """``with_structured_output`` con una clase pydantic devuelve una instancia, no un dict."""
    from packages.llm_layer.providers.base import ExplicacionLLM

    chat = ModeloEstructurado(
        responses=_mensajes_vacios(), salida=ExplicacionLLM.model_validate(SALIDA_EXPLICACION)
    )
    resultado = _proveedor(chat).completar("PROMPT", ESQUEMA_EXPLICACION_V1)
    assert isinstance(resultado, dict)
    assert resultado["cifras_usadas"] == [731]


def test_pasa_el_metodo_configurado_a_with_structured_output() -> None:
    """``LLM_LANGCHAIN_METODO`` llega tal cual a ``with_structured_output``."""
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_CONVERSACIONAL)
    _proveedor(chat, metodo="json_schema").completar("P", ESQUEMA_CONVERSACIONAL)
    assert chat.metodos_vistos == ["json_schema"]


# --------------------------------------------------------------------------- #
# 2. Normalización del esquema
# --------------------------------------------------------------------------- #
def test_normalizar_esquema_inyecta_titulo_y_no_toca_el_original() -> None:
    """``convert_to_openai_tool`` exige ``title``; ``ESQUEMA_CONVERSACIONAL`` no lo trae."""
    normalizado = normalizar_esquema(ESQUEMA_CONVERSACIONAL)
    assert normalizado["title"] == modulo.TITULO_POR_DEFECTO
    assert "title" not in ESQUEMA_CONVERSACIONAL  # el original queda intacto


def test_normalizar_esquema_poda_claves_propietarias_en_todos_los_niveles() -> None:
    """``propertyOrdering`` es de Google y viaja anidado dentro de ``explicacion_v1``."""
    normalizado = normalizar_esquema(ESQUEMA_EXPLICACION_V1)
    serializado = json.dumps(normalizado)
    assert "propertyOrdering" not in serializado
    assert "propertyOrdering" in json.dumps(ESQUEMA_EXPLICACION_V1)
    assert normalizado["properties"]["causas"]["items"]["required"] == [
        "concepto_id",
        "frase",
        "monto_cent_citado",
    ]


def test_el_esquema_normalizado_es_convertible_a_herramienta_openai() -> None:
    """La razón de existir de la normalización, comprobada contra langchain-core."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    with pytest.raises(ValueError, match="title"):
        convert_to_openai_tool(ESQUEMA_CONVERSACIONAL)

    herramienta = convert_to_openai_tool(normalizar_esquema(ESQUEMA_CONVERSACIONAL))
    assert herramienta["function"]["name"] == modulo.TITULO_POR_DEFECTO


# --------------------------------------------------------------------------- #
# 3. Modo texto: modelos sin salida estructurada
# --------------------------------------------------------------------------- #
def test_modo_texto_cuando_el_modelo_no_soporta_salida_estructurada() -> None:
    """Sin ``bind_tools`` se pide el JSON en el mensaje, con el esquema incrustado."""
    chat = ModeloTexto(responses=[AIMessage(content=json.dumps(SALIDA_CONVERSACIONAL))])
    resultado = _proveedor(chat).completar("PROMPT DEL SISTEMA", ESQUEMA_CONVERSACIONAL)

    assert resultado == SALIDA_CONVERSACIONAL
    instruccion = str(chat.mensajes_vistos[0][0].content)
    assert "JSON Schema" in instruccion
    # El esquema PEDIDO viaja dentro, no uno fijo.
    assert '"respuesta"' in instruccion
    assert "resumen" not in instruccion
    assert str(chat.mensajes_vistos[0][1].content) == "PROMPT DEL SISTEMA"


def test_modo_texto_tolera_vallas_de_markdown() -> None:
    """Un modelo locuaz que envuelve el JSON en ```json no debe tumbar el turno."""
    envuelto = "```json\n" + json.dumps(SALIDA_CONVERSACIONAL) + "\n```"
    chat = ModeloTexto(responses=[AIMessage(content=envuelto)])
    assert _proveedor(chat).completar("P", ESQUEMA_CONVERSACIONAL) == SALIDA_CONVERSACIONAL


@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param('{"respuesta": "Hola, ¿en qué le pue', id="json-truncado"),
        pytest.param("Claro que sí, con mucho gusto.", id="prosa-libre"),
    ],
)
def test_una_salida_a_medias_degrada_y_senala_el_presupuesto(contenido: str) -> None:
    """El JSON cortado a media cadena es la firma de los tokens de razonamiento.

    El parser de langchain-core se usa en modo **estricto**: su modo por defecto
    *repara* el JSON parcial y el cliente recibiría la frase a medias. Aquí se prefiere
    degradar a plantilla, y el error apunta a la palanca que arregla la causa.
    """
    chat = ModeloTexto(responses=[AIMessage(content=contenido)])
    with pytest.raises(ErrorRespuestaInvalida) as excinfo:
        _proveedor(chat).completar("P", ESQUEMA_CONVERSACIONAL)
    assert modulo.VAR_MAX_TOKENS in str(excinfo.value)
    assert modulo.VAR_EXTRA in str(excinfo.value)


def test_modo_texto_con_respuesta_vacia() -> None:
    """Respuesta vacía (bloqueo de seguridad o corte) es ``ErrorRespuestaInvalida``."""
    chat = ModeloTexto(responses=[AIMessage(content="")])
    with pytest.raises(ErrorRespuestaInvalida, match="vacía"):
        _proveedor(chat).completar("P", ESQUEMA_CONVERSACIONAL)


def test_metodo_texto_fuerza_el_modo_texto_aunque_haya_estructurado() -> None:
    """``LLM_LANGCHAIN_METODO=texto`` es la escotilla para modelos que mienten."""
    chat = ModeloEstructurado(
        responses=[AIMessage(content=json.dumps(SALIDA_CONVERSACIONAL))],
        salida={"jamas": "se usa"},
    )
    resultado = _proveedor(chat, metodo=modulo.METODO_TEXTO).completar("P", ESQUEMA_CONVERSACIONAL)
    assert resultado == SALIDA_CONVERSACIONAL
    assert chat.esquemas_vistos == []


# --------------------------------------------------------------------------- #
# 4. Traducción de errores a la jerarquía del proyecto
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("excepcion", "esperado", "codigo"),
    [
        (TimeoutError("request timed out after 30s"), ErrorTiempoAgotado, "TIEMPO_AGOTADO"),
        (
            RuntimeError("Error code: 429 - rate limit exceeded for gpt-4o-mini"),
            ErrorCuota,
            "CUOTA_AGOTADA",
        ),
        (
            RuntimeError("Error code: 401 - Incorrect api key provided"),
            ErrorConfiguracionProveedor,
            "CONFIGURACION_INVALIDA",
        ),
        (
            OutputParserException("Invalid json output: {'resumen'"),
            ErrorRespuestaInvalida,
            "RESPUESTA_INVALIDA",
        ),
        (RuntimeError("el servidor del proveedor devolvió un 5xx"), ErrorProveedor, None),
    ],
)
def test_traduce_los_errores_del_modelo(
    excepcion: Exception, esperado: type[ErrorProveedor], codigo: str | None
) -> None:
    """Todo fallo sale como ``ErrorProveedor``: es la señal que degrada a plantilla."""
    chat = ModeloQueRevienta(responses=_mensajes_vacios(), error=excepcion)
    with pytest.raises(esperado) as excinfo:
        _proveedor(chat).completar("P", ESQUEMA_CONVERSACIONAL)

    error = excinfo.value
    assert isinstance(error, ErrorProveedor)
    assert error.proveedor == "langchain"
    if codigo is not None:
        assert error.codigo == codigo
    assert set(error.a_dict()) == {"codigo", "proveedor", "detalle", "reintentable"}


def test_un_fallo_desconocido_se_marca_reintentable() -> None:
    """Lo que no se sabe clasificar se marca reintentable, no se da por definitivo."""
    chat = ModeloQueRevienta(responses=_mensajes_vacios(), error=RuntimeError("vaya por dios"))
    with pytest.raises(ErrorProveedor) as excinfo:
        _proveedor(chat).completar("P", ESQUEMA_CONVERSACIONAL)
    assert excinfo.value.reintentable is True


def test_el_guardian_de_tiempo_corta_un_modelo_colgado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un cliente HTTP sin timeout no puede dejar colgado el hilo del endpoint."""
    monkeypatch.setattr(modulo, "MARGEN_GUARDIA_S", 0.05)
    # `FakeMessagesListChatModel` duerme dentro de `_generate`: un modelo colgado.
    chat = FakeMessagesListChatModel(responses=[AIMessage(content="{}")], sleep=5.0)
    proveedor = LangChainProvider(chat=chat, guardia=True)
    with pytest.raises(ErrorTiempoAgotado):
        proveedor.completar("P", ESQUEMA_CONVERSACIONAL, timeout_s=0.01)


def test_el_guardian_no_estorba_a_una_llamada_normal() -> None:
    """Con el guardián activo, la ruta feliz devuelve el valor igual."""
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_CONVERSACIONAL)
    proveedor = LangChainProvider(chat=chat, guardia=True)
    assert proveedor.completar("P", ESQUEMA_CONVERSACIONAL, timeout_s=5.0) == SALIDA_CONVERSACIONAL


# --------------------------------------------------------------------------- #
# 5. Sin paquete de integración: se degrada, no se rompe
# --------------------------------------------------------------------------- #
def test_construir_el_proveedor_no_importa_ninguna_integracion() -> None:
    """El constructor no toca la red ni importa nada: el fallo llega en ``completar``."""
    proveedor = LangChainProvider(modelo="openai:gpt-4o-mini")
    assert proveedor.proveedor_modelo == "openai"
    assert proveedor.modelo == "gpt-4o-mini"
    assert proveedor.version_modelo == "langchain:openai:gpt-4o-mini"


def test_paquete_de_integracion_ausente_da_error_configuracion() -> None:
    """Falta el paquete: ``ErrorConfiguracionProveedor`` con el ``pip install`` exacto."""
    proveedor = LangChainProvider(ruta_clase="paquete_inexistente_recibo_claro:ChatFalso")
    with pytest.raises(ErrorConfiguracionProveedor) as excinfo:
        proveedor.completar("P", ESQUEMA_CONVERSACIONAL)
    assert "pip install" in str(excinfo.value)
    assert excinfo.value.codigo == "CONFIGURACION_INVALIDA"


def test_clase_ausente_en_un_paquete_presente() -> None:
    """Si la integración renombró su clase, el error dice qué se intentó importar."""
    proveedor = LangChainProvider(ruta_clase="json:ChatQueNoExiste")
    with pytest.raises(ErrorConfiguracionProveedor, match="ChatQueNoExiste"):
        proveedor.completar("P", ESQUEMA_CONVERSACIONAL)


def test_proveedor_desconocido_enumera_los_conocidos() -> None:
    """Un proveedor que no está en el catálogo no revienta: enumera los que sí están."""
    proveedor = LangChainProvider(modelo="proveedor_inventado:modelo-x")
    with pytest.raises(ErrorConfiguracionProveedor):
        proveedor.completar("P", ESQUEMA_CONVERSACIONAL)


def test_sin_modelo_ni_chat_el_constructor_avisa() -> None:
    """Sin ``LLM_LANGCHAIN_MODELO`` no hay nada que construir; se dice claramente."""
    with pytest.raises(ErrorConfiguracionProveedor, match=modulo.VAR_MODELO):
        LangChainProvider()


def test_extra_mal_formado_es_error_de_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LLM_LANGCHAIN_EXTRA`` con JSON roto se detecta al construir, no en producción."""
    monkeypatch.setenv(modulo.VAR_EXTRA, "{esto no es json}")
    with pytest.raises(ErrorConfiguracionProveedor, match=modulo.VAR_EXTRA):
        LangChainProvider(modelo="openai:gpt-4o-mini")


def test_se_descartan_los_parametros_que_la_integracion_no_admite() -> None:
    """Las integraciones no comparten firma; el adaptador no exige que el operador acierte."""

    class ChatEstrecho:
        """Integración imaginaria que solo acepta ``model``."""

        def __init__(self, *, model: str) -> None:
            self.model = model

    proveedor = LangChainProvider(modelo="x:mini", max_tokens=8192)
    construido = proveedor._instanciar(ChatEstrecho)
    assert construido.model == "mini"


def test_si_no_se_puede_construir_ni_con_lo_minimo_hay_error_tipado() -> None:
    """Cuando ni ``model`` sirve, el fallo sigue siendo de la jerarquía del proyecto."""

    class ChatImposible:
        def __init__(self, **_kwargs: Any) -> None:
            raise TypeError("esta clase no acepta nada")

    proveedor = LangChainProvider(modelo="x:mini")
    with pytest.raises(ErrorConfiguracionProveedor, match="ChatImposible"):
        proveedor._instanciar(ChatImposible)


# --------------------------------------------------------------------------- #
# 6. Registro en la fábrica, sin romper los modos existentes
# --------------------------------------------------------------------------- #
def test_el_modo_langchain_esta_registrado() -> None:
    assert MODO_LANGCHAIN == "langchain"
    assert MODOS_VALIDOS == ("mock", "gemini", "langchain")


def test_la_fabrica_devuelve_el_adaptador() -> None:
    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_CONVERSACIONAL)
    proveedor = obtener_proveedor(MODO_LANGCHAIN, chat=chat, guardia=False)
    assert isinstance(proveedor, LangChainProvider)
    assert proveedor.nombre == "langchain"
    assert isinstance(proveedor, ProveedorLLM)  # el Protocol es runtime_checkable


def test_la_fabrica_no_rompe_los_modos_existentes() -> None:
    """``mock`` sigue siendo el camino por defecto y el modo inválido sigue avisando."""
    from packages.llm_layer.providers.mock import MockProvider

    assert isinstance(obtener_proveedor("mock"), MockProvider)
    with pytest.raises(ErrorConfiguracionProveedor, match="langchain"):
        obtener_proveedor("no-existe")


def test_version_modelo_para_gobernanza() -> None:
    """``Gobernanza.model_version`` debe decir qué modelo redactó, exactamente."""
    from packages.llm_layer.providers.base import version_modelo_de

    chat = ModeloEstructurado(responses=_mensajes_vacios(), salida=SALIDA_CONVERSACIONAL)
    assert version_modelo_de(_proveedor(chat)).startswith("langchain:")
    assert version_modelo_de(LangChainProvider(modelo="ollama:llama3.1")) == (
        "langchain:ollama:llama3.1"
    )


def test_el_catalogo_es_solo_un_mapa_de_nombres() -> None:
    """Cargar el módulo no importa ninguna integración: el catálogo son cadenas."""
    assert CATALOGO_INTEGRACIONES["openai"] == ("langchain_openai", "ChatOpenAI")
    assert all(
        isinstance(paquete, str) and isinstance(clase, str)
        for paquete, clase in CATALOGO_INTEGRACIONES.values()
    )


def test_no_hay_dependencias_con_licencia_restringida() -> None:
    """Guardia de licencia: la cláusula 9 de las bases cede la PI a Integratel.

    ``langgraph-api`` está bajo Elastic License 2.0 y ``langgraph-cli`` acompaña a
    LangGraph Platform. Si alguno acabara instalado, Integratel no podría desplegar la
    solución sin negociar con un tercero.
    """
    from importlib.util import find_spec

    for prohibido in ("langgraph_api", "langgraph_cli"):
        assert find_spec(prohibido) is None, f"{prohibido} no puede estar instalado"


def test_la_telemetria_de_langsmith_esta_apagada() -> None:
    """Nada de este proveedor sale hacia LangSmith (ver el bloque de ``.env.example``)."""
    from langsmith.utils import tracing_is_enabled

    assert tracing_is_enabled() is False


# --------------------------------------------------------------------------- #
# 7. Configuración por entorno
# --------------------------------------------------------------------------- #
def test_lee_el_modelo_y_el_proveedor_del_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(modulo.VAR_MODELO, "  Anthropic:claude-x  ")
    proveedor = LangChainProvider()
    assert proveedor.proveedor_modelo == "anthropic"
    assert proveedor.modelo == "claude-x"


def test_el_proveedor_puede_ir_en_variable_aparte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(modulo.VAR_MODELO, "llama3.1")
    monkeypatch.setenv(modulo.VAR_PROVEEDOR, "OLLAMA")
    proveedor = LangChainProvider()
    assert (proveedor.proveedor_modelo, proveedor.modelo) == ("ollama", "llama3.1")


def test_presupuesto_de_salida_por_defecto_y_por_entorno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8192 por defecto: con menos, el JSON de un modelo que razona llega truncado."""
    assert LangChainProvider(modelo="x:y").max_tokens == modulo.MAX_TOKENS_POR_DEFECTO
    monkeypatch.setenv(modulo.VAR_MAX_TOKENS, "16384")
    assert LangChainProvider(modelo="x:y").max_tokens == 16384
    monkeypatch.setenv(modulo.VAR_MAX_TOKENS, "no-es-un-numero")
    assert LangChainProvider(modelo="x:y").max_tokens == modulo.MAX_TOKENS_POR_DEFECTO


def test_la_variable_propia_manda_sobre_la_api_key_reenviada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``deps`` reenvía ``GEMINI_API_KEY``; ``LLM_LANGCHAIN_API_KEY`` tiene prioridad."""
    monkeypatch.setenv(modulo.VAR_API_KEY, "clave-propia")
    proveedor = LangChainProvider(modelo="openai:gpt-4o-mini", api_key="clave-de-gemini")
    assert proveedor._kwargs_modelo()["api_key"] == "clave-propia"


def test_gemini_model_heredado_no_pisa_al_modelo_de_langchain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce el fallo real: ``deps`` reenvía ``GEMINI_MODEL`` como ``modelo=``.

    Con un ``GEMINI_MODEL=gemini-2.5-flash`` heredado del ``.env``, el adaptador
    acababa pidiendo un id de Google a un proveedor vacío y toda respuesta caía a
    plantilla. El ajuste dedicado es el que manda.
    """
    monkeypatch.setenv(modulo.VAR_MODELO, "openai:gpt-4o-mini")
    proveedor = LangChainProvider(modelo="gemini-2.5-flash")  # lo que reenvía deps
    assert (proveedor.proveedor_modelo, proveedor.modelo) == ("openai", "gpt-4o-mini")
    assert proveedor.version_modelo == "langchain:openai:gpt-4o-mini"


def test_sin_variable_dedicada_se_usa_el_argumento() -> None:
    """Sin ``LLM_LANGCHAIN_MODELO``, el argumento sigue valiendo: nadie queda sin modelo."""
    proveedor = LangChainProvider(modelo="ollama:llama3.1")
    assert (proveedor.proveedor_modelo, proveedor.modelo) == ("ollama", "llama3.1")


def test_los_extras_del_entorno_llegan_al_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    """La palanca para apagar el razonamiento: ``LLM_LANGCHAIN_EXTRA``."""
    monkeypatch.setenv(modulo.VAR_EXTRA, '{"thinking_budget": 0}')
    opciones = LangChainProvider(modelo="google_genai:gemini-2.5-flash")._kwargs_modelo()
    assert opciones["thinking_budget"] == 0
    assert opciones["max_tokens"] == modulo.MAX_TOKENS_POR_DEFECTO
    assert opciones["temperature"] == 0.0
