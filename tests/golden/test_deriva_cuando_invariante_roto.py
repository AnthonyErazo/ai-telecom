"""Invariante roto ⇒ **no se explica, se deriva** (reglas innegociables nº 5 y sección 4.6).

    ``|residual_cent| > 1  →  invariante.ok = False  →  409 INVARIANTE_FALLIDO
                           →  derivación automática``

Nunca se entrega una "explicación aproximada". Si las líneas del recibo no reproducen la
diferencia entre los dos totales, el sistema no sabe realmente qué pasó, y decirle al
cliente una causa plausible sería inventarse la parte que falta. La política correcta es
reconocer el descuadre y pasar el caso a un asesor con todo el contexto cargado.

**Cómo se provoca el descuadre.** No se fabrica un FactSet inconsistente a mano —eso no
probaría nada—: se simula el fallo real, que es que BrainyBill devuelva el detalle del
recibo **incompleto**. Se toma el recibo previo del cliente de demostración y se le
quita una línea sin tocar su total, exactamente como llegaría un documento truncado por
un corte de la API. A partir de ahí el motor hace su trabajo y el residual aparece solo.
"""

from __future__ import annotations

import pytest

from packages.core_domain.enums import (
    AccionSiguiente,
    MotivoDerivacion,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.recibo import Recibo
from packages.facts_engine.confianza import evaluar_incomprension
from packages.facts_engine.invariante import debe_derivar, mensaje_descuadre
from packages.facts_engine.motor import construir_factset, resumen_de_conciliacion
from packages.llm_layer import explicar, extraer_numeros

pytestmark = pytest.mark.golden

CUENTA_DEMO = "C-DEMO-01"
PERIODO = "2026-07"


@pytest.fixture(scope="module")
def cuenta(exige_dataset: None):
    """Cuenta de demostración con sus seis recibos y sus órdenes."""
    from eval.datos import cargar_cuenta

    return cargar_cuenta(CUENTA_DEMO)


@pytest.fixture(scope="module")
def recibo_previo_truncado(cuenta) -> Recibo:
    """Recibo previo al que le falta una línea: ``Σ líneas != total_cent``.

    ``model_copy`` no revalida a propósito: es la única forma de reproducir un documento
    corrupto que el modelo de dominio jamás construiría, que es justo el caso contra el
    que existe el invariante.
    """
    previo = cuenta.recibo("2026-06")
    assert previo is not None, "la cuenta de demostración debe tener recibo de 2026-06"
    truncado = previo.model_copy(update={"lineas": previo.lineas[:-1]})
    assert sum(linea.monto_cent for linea in truncado.lineas) != truncado.total_cent
    return truncado


@pytest.fixture(scope="module")
def factset_roto(cuenta, recibo_previo_truncado: Recibo, reglas):
    """FactSet construido sobre el recibo truncado: el residual sale del propio motor."""
    actual = cuenta.recibo(PERIODO)
    return construir_factset(actual, [recibo_previo_truncado], cuenta.movimientos, reglas)


@pytest.fixture(scope="module")
def factset_sano(cuenta, reglas):
    """FactSet de control con los datos íntegros: mismo cliente, mismo periodo."""
    from eval.datos import factset_de_cuenta

    return factset_de_cuenta(cuenta, PERIODO, reglas)


# --------------------------------------------------------------------------- #
# El motor detecta el descuadre y no lo esconde
# --------------------------------------------------------------------------- #
class TestDeteccion:
    """El motor **no lanza excepción**: devuelve el FactSet con el descuadre marcado."""

    def test_el_residual_supera_la_tolerancia(self, factset_roto) -> None:
        invariante = factset_roto.invariante
        assert invariante.ok is False
        assert abs(invariante.residual_cent) > 1
        assert invariante.residual_cent == (
            invariante.delta_total_cent - invariante.suma_deltas_cent
        )
        assert debe_derivar(invariante) is True

    def test_el_factset_llega_completo_para_que_el_asesor_pueda_trabajar(
        self, factset_roto
    ) -> None:
        """Un error aquí dejaría al asesor sin los datos: el FactSet se entrega igual."""
        assert factset_roto.lineas, "las líneas deben viajar aunque el invariante falle"
        assert factset_roto.verificar_sha256() is True
        resumen = resumen_de_conciliacion(factset_roto)
        assert resumen["invariante_ok"] is False
        assert resumen["residual_cent"] == factset_roto.invariante.residual_cent

    def test_el_mensaje_de_descuadre_es_para_auditoria_no_para_el_cliente(
        self, factset_roto
    ) -> None:
        mensaje = mensaje_descuadre(factset_roto.invariante)
        assert "descuadre" in mensaje.lower()
        assert str(abs(factset_roto.invariante.residual_cent)) in mensaje

    def test_el_control_sano_concilia_exacto(self, factset_sano) -> None:
        """Sin el truncamiento, el mismo cliente y periodo cierran en cero."""
        assert factset_sano.invariante.ok is True
        assert factset_sano.invariante.residual_cent == 0
        assert debe_derivar(factset_sano.invariante) is False


# --------------------------------------------------------------------------- #
# La política: derivar
# --------------------------------------------------------------------------- #
class TestDerivacion:
    """El invariante roto es una **regla dura**: deriva sin calcular el score."""

    def test_el_umbral_de_incomprension_deriva_por_regla_dura(self, factset_roto, reglas) -> None:
        veredicto = evaluar_incomprension(
            factset_roto, None, "¿por qué me vino más caro este mes?", reglas=reglas
        )
        assert veredicto.derivar is True
        assert veredicto.motivo is MotivoDerivacion.INVARIANTE_ROTO
        assert MotivoDerivacion.INVARIANTE_ROTO.value in veredicto.reglas_disparadas
        assert veredicto.senal_disparadora is not None
        assert "concilia" in veredicto.senal_disparadora

    def test_deriva_aunque_el_cliente_no_pida_nada_raro(self, factset_roto, reglas) -> None:
        """La regla dura no depende de lo que escriba el cliente."""
        for utterance in ("", "gracias", "¿cuánto pago?"):
            veredicto = evaluar_incomprension(factset_roto, None, utterance, reglas=reglas)
            assert veredicto.derivar is True

    def test_el_control_sano_no_deriva(self, factset_sano, reglas) -> None:
        """Contraejemplo obligatorio: si derivara siempre, el test no probaría nada."""
        veredicto = evaluar_incomprension(
            factset_sano, None, "¿por qué me vino más caro este mes?", reglas=reglas
        )
        assert veredicto.derivar is False
        assert veredicto.motivo is None


class TestRespuestaAlCliente:
    """Lo que sale hacia el canal cuando el recibo no concilia."""

    @pytest.fixture(scope="class")
    def respuesta(self, factset_roto):
        """Respuesta generada sobre el FactSet descuadrado."""
        return explicar(factset_roto, modo="mock", utterance="¿por qué me vino más caro?")

    def test_la_respuesta_marca_la_derivacion_con_su_motivo(self, respuesta) -> None:
        derivacion = respuesta.derivacion
        assert derivacion.requerida is True
        assert derivacion.motivo_codigo is MotivoDerivacion.INVARIANTE_ROTO
        assert derivacion.motivo, "el asesor necesita saber por qué le llega el caso"
        assert derivacion.senal_disparadora is not None
        assert "residual_cent" in derivacion.senal_disparadora

    def test_ofrece_hablar_con_un_asesor_como_accion_principal(self, respuesta) -> None:
        acciones = [accion.id for accion in respuesta.acciones]
        assert acciones[0] is AccionSiguiente.DERIVAR_ASESOR
        assert AccionSiguiente.REGISTRAR_CONSULTA in acciones

    def test_avisa_al_cliente_en_lugar_de_dar_una_explicacion_cerrada(self, respuesta) -> None:
        """Hay un aviso crítico: no se presenta el cálculo como si cuadrara."""
        avisos = [bloque for bloque in respuesta.bloques if bloque.tipo == "aviso"]
        criticos = [bloque for bloque in avisos if bloque.severidad == "critico"]
        assert criticos, "debe haber un aviso crítico cuando el recibo no concilia"
        assert "asesor" in criticos[0].texto.lower()

    def test_ni_siquiera_al_derivar_se_escapa_una_cifra_sin_anclar(
        self, respuesta, factset_roto
    ) -> None:
        """El descuadre no relaja el verificador: sigue sin haber cifras inventadas."""
        infractores = extraer_numeros(respuesta.texto) - factset_roto.tokens_permitidos()
        assert infractores == set(), f"Alucinación numérica al derivar: {sorted(infractores)}"
        assert respuesta.gobernanza.verificacion_numerica == VeredictoVerificacion.PASS
        assert respuesta.gobernanza.aserciones_no_ancladas == 0

    def test_el_control_sano_no_marca_derivacion(self, factset_sano) -> None:
        respuesta = explicar(factset_sano, modo="mock", utterance="¿por qué me vino más caro?")
        assert respuesta.derivacion.requerida is False
        assert respuesta.acciones[0].id is not AccionSiguiente.DERIVAR_ASESOR
        assert not [
            bloque
            for bloque in respuesta.bloques
            if bloque.tipo == "aviso" and bloque.severidad == "critico"
        ]


# --------------------------------------------------------------------------- #
# El payload que recibe el asesor
# --------------------------------------------------------------------------- #
def test_el_handoff_lleva_el_contexto_completo(factset_roto, reglas) -> None:
    """La ficha pide *"transferir el contexto de la interacción"*: aquí está.

    Se comprueban los siete campos del payload de hand-off con el mismo constructor que
    usa la evaluación, para que la métrica ``Handoff_completeness`` y este test no puedan
    divergir.
    """
    from eval.metricas import CAMPOS_HANDOFF, completitud_handoff, resumen_para_asesor

    veredicto = evaluar_incomprension(factset_roto, None, "no entiendo mi recibo", reglas=reglas)
    resumen = resumen_para_asesor(factset_roto, veredicto)
    derivacion = veredicto.a_derivacion(context_ref="trace:test", resumen_asesor=resumen)

    presentes, totales = completitud_handoff(derivacion)
    assert totales == len(CAMPOS_HANDOFF) == 7
    assert presentes == totales, "el asesor no puede recibir un contexto incompleto"

    assert str(factset_roto.cuenta_id) in resumen
    assert "residual" in resumen
    assert "ROTO" in resumen, "el resumen para el asesor tiene que decir que no concilia"
