"""La fábrica de proveedores: registro extensible y cascada entre modelos.

Qué se protege aquí
-------------------
1. **Que añadir un fabricante no obligue a editar la fábrica.** BASES §9 cede la
   propiedad intelectual a Integratel: si enchufar su modelo corporativo exigiera
   parchear un ``if`` interno, cada actualización nuestra sería un conflicto de fusión
   suyo.
2. **Que agotar la cuota no degrade a plantilla mientras quede otro modelo vivo.** Es
   la petición literal: *«usemos siempre la IA, no importa que se haya agotado»*.
3. **Que la red de seguridad siga intacta.** Si fallan todos, la excepción se relanza y
   el generador degrada a plantilla determinística exactamente como antes. La cascada
   retrasa el respaldo; no lo sustituye.
4. **Que la bitácora no mienta bajo concurrencia.** ``version_modelo`` debe decir qué
   modelo respondió a *este* turno, no al del hilo de al lado.
"""

from __future__ import annotations

import pytest

from packages.llm_layer.providers.base import (
    ErrorConfiguracionProveedor,
    ErrorCuota,
    ErrorRespuestaInvalida,
    ProveedorEnCascada,
    modos_registrados,
    obtener_proveedor,
    registrar_proveedor,
)


class _Falla:
    """Proveedor que siempre agota cuota."""

    nombre = "falla"
    version_modelo = "falla-1"

    def __init__(self) -> None:
        self.llamadas = 0

    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        self.llamadas += 1
        raise ErrorCuota("429", proveedor=self.nombre)


class _NoConfigurado:
    """Proveedor sin credencial en esta instalación."""

    nombre = "sin_credencial"
    version_modelo = "sin_credencial-1"

    def __init__(self) -> None:
        self.llamadas = 0

    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        self.llamadas += 1
        raise ErrorConfiguracionProveedor("falta la clave", proveedor=self.nombre)


class _Responde:
    """Proveedor que responde."""

    nombre = "responde"
    version_modelo = "responde-1"

    def __init__(self) -> None:
        self.llamadas = 0

    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        self.llamadas += 1
        return {"resumen": "ok"}


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #
def test_los_tres_modos_de_serie_estan_registrados() -> None:
    assert {"mock", "gemini", "langchain"} <= set(modos_registrados())


def test_un_tercero_registra_su_proveedor_sin_tocar_la_fabrica() -> None:
    """El caso Integratel: dar de alta un fabricante desde fuera del paquete."""
    registrar_proveedor(
        "corporativo_de_prueba", "packages.llm_layer.providers.mock", "MockProvider"
    )
    assert "corporativo_de_prueba" in modos_registrados()
    assert obtener_proveedor("corporativo_de_prueba") is not None


def test_un_modo_desconocido_dice_cuales_hay() -> None:
    """El error tiene que ser accionable, no un KeyError."""
    with pytest.raises(ErrorConfiguracionProveedor) as exc:
        obtener_proveedor("no_existe_este_modo")
    mensaje = str(exc.value)
    assert "no_existe_este_modo" in mensaje
    assert "mock" in mensaje  # enumera los disponibles


def test_registrar_con_modo_vacio_es_un_error() -> None:
    with pytest.raises(ValueError):
        registrar_proveedor("   ", "modulo", "Clase")


# --------------------------------------------------------------------------- #
# Cascada
# --------------------------------------------------------------------------- #
def test_un_modo_simple_no_envuelve_en_cascada() -> None:
    """El comportamiento de siempre no cambia: `LLM_MODE=mock` da un MockProvider."""
    proveedor = obtener_proveedor("mock")
    assert not isinstance(proveedor, ProveedorEnCascada)


def test_llm_mode_con_comas_construye_una_cascada() -> None:
    proveedor = obtener_proveedor("mock,mock")
    assert isinstance(proveedor, ProveedorEnCascada)
    assert len(proveedor.proveedores) == 2


def test_al_agotarse_la_cuota_se_intenta_el_siguiente() -> None:
    """La petición explícita: cuota agotada no debe significar plantilla."""
    agotado, vivo = _Falla(), _Responde()
    resultado = ProveedorEnCascada([agotado, vivo]).completar("prompt", {})
    assert resultado == {"resumen": "ok"}
    assert agotado.llamadas == 1
    assert vivo.llamadas == 1


def test_un_proveedor_sin_credencial_se_salta_sin_consumir_intento() -> None:
    """No estar configurado no es un fallo del turno: es no estar disponible."""
    sin_clave, vivo = _NoConfigurado(), _Responde()
    assert ProveedorEnCascada([sin_clave, vivo]).completar("p", {}) == {"resumen": "ok"}


def test_si_fallan_todos_se_relanza_para_que_el_generador_use_plantilla() -> None:
    """La red de seguridad se retrasa, no se quita."""
    with pytest.raises(ErrorCuota):
        ProveedorEnCascada([_Falla(), _Falla()]).completar("p", {})


def test_se_relanza_el_ULTIMO_error_no_el_primero() -> None:
    """Diagnóstico honesto: interesa por qué falló el último recurso."""

    class _Invalida:
        nombre = "invalida"
        version_modelo = "invalida-1"

        def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
            raise ErrorRespuestaInvalida("json roto", proveedor=self.nombre)

    with pytest.raises(ErrorRespuestaInvalida):
        ProveedorEnCascada([_Falla(), _Invalida()]).completar("p", {})


def test_el_primero_que_responde_corta_la_cascada() -> None:
    """No se llama a los de más abajo si el principal contesta: ni coste ni latencia."""
    vivo, nunca = _Responde(), _Falla()
    ProveedorEnCascada([vivo, nunca]).completar("p", {})
    assert nunca.llamadas == 0


def test_una_cascada_vacia_es_un_error_de_configuracion() -> None:
    with pytest.raises(ErrorConfiguracionProveedor):
        ProveedorEnCascada([])


# --------------------------------------------------------------------------- #
# Trazabilidad
# --------------------------------------------------------------------------- #
def test_version_modelo_nombra_a_quien_respondio_de_verdad() -> None:
    """La bitácora debe registrar el modelo que contestó, no el que se pidió."""
    cascada = ProveedorEnCascada([_Falla(), _Responde()])
    cascada.completar("p", {})
    assert cascada.version_modelo == "responde-1"


def test_la_version_es_por_contexto_y_no_se_filtra_entre_turnos() -> None:
    """Bajo concurrencia, un turno no puede leer el modelo que resolvió otro.

    Cada tarea de ``asyncio`` —y por tanto cada petición de FastAPI— corre en una copia
    del contexto, así que lo que un turno escriba no debe verse desde otro. Para que el
    escape sea *detectable*, los dos contextos resuelven a modelos **distintos**: si la
    variable se filtrase, el de fuera acabaría diciendo ``otro-1``.

    Si ``version_modelo`` viviera en un atributo de instancia, esta prueba fallaría —y
    la bitácora atribuiría a un turno el modelo que respondió al de al lado.
    """
    import contextvars

    class _Otro:
        nombre = "otro"
        version_modelo = "otro-1"

        def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
            return {"resumen": "ok"}

    # Turno 1, en el contexto actual: responde el segundo de la cascada.
    fuera = ProveedorEnCascada([_Falla(), _Responde()])
    fuera.completar("p", {})
    assert fuera.version_modelo == "responde-1"

    # Turno 2, en un contexto aislado: responde un modelo distinto.
    dentro = ProveedorEnCascada([_Otro()])
    contextvars.copy_context().run(lambda: dentro.completar("p", {}))

    # El turno 1 sigue diciendo la verdad: nadie le pisó la atribución.
    assert fuera.version_modelo == "responde-1"
