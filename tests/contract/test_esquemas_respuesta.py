"""Contrato de las respuestas: cada una valida contra su esquema (sección 11).

Se comprueban tres niveles, de menor a mayor exigencia:

1. **Estructural** — el JSON de salida vuelve a validar contra su propio modelo y contra
   el JSON Schema que ese modelo publica. Es lo que garantiza que un consumidor (la App,
   el Bot Lucía, WhatsApp) pueda generar tipos desde el esquema y no se le rompan.
2. **Cerrado** — ``extra="forbid"`` en toda la familia: un campo no declarado **falla
   ruidosamente**. Un contrato que ignora en silencio lo que no entiende no es un
   contrato.
3. **Semántico** — las invariantes de gobernanza que la base de datos replica como
   ``CHECK`` en ``001_core.sql``: ``verificacion_numerica = 'PASS'`` exige
   ``aserciones_no_ancladas = 0``, y ``anclado = (no_ancladas == 0)``. Si Python y
   PostgreSQL discreparan, habría respuestas que la API entrega y la base rechaza.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.core_domain.enums import ModoGeneracion, NivelAseguramiento, VeredictoVerificacion
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import (
    Accion,
    Derivacion,
    Gobernanza,
    ItemEvidencia,
    PeticionDerivacion,
    PeticionExplicacion,
    RespuestaCanalAgnostica,
    RespuestaError,
)

pytestmark = pytest.mark.contrato


def _validar_json_schema(instancia: Any, esquema: dict[str, Any]) -> None:
    """Valida con ``jsonschema`` si está disponible; si no, omite ese refuerzo."""
    jsonschema = pytest.importorskip(
        "jsonschema", reason="jsonschema no instalado: la validación Pydantic ya corrió"
    )
    jsonschema.validate(instance=instancia, schema=esquema)


@pytest.fixture(scope="module")
def respuestas(exige_dataset: None) -> list[RespuestaCanalAgnostica]:
    """Una respuesta real por cada caso golden: el contrato se prueba con datos, no con maquetas."""
    from eval.datos import cargar_golden, factset_de_caso
    from packages.llm_layer import explicar

    salida: list[RespuestaCanalAgnostica] = []
    for caso in cargar_golden():
        factset = factset_de_caso(caso)
        salida.append(
            explicar(
                factset,
                modo="mock",
                verbosidad=caso.verbosidad,
                utterance=caso.utterance,
                canal=caso.canal,
            )
        )
    return salida


# --------------------------------------------------------------------------- #
# Nivel 1: estructural
# --------------------------------------------------------------------------- #
class TestEstructura:
    """Ida y vuelta por JSON sin pérdida y validación contra el esquema publicado."""

    def test_toda_respuesta_hace_ida_y_vuelta_por_json(self, respuestas) -> None:
        for respuesta in respuestas:
            crudo = respuesta.model_dump_json()
            vuelta = RespuestaCanalAgnostica.model_validate_json(crudo)
            assert vuelta.model_dump(mode="json") == respuesta.model_dump(mode="json")

    def test_toda_respuesta_valida_contra_su_json_schema(self, respuestas) -> None:
        esquema = RespuestaCanalAgnostica.model_json_schema()
        for respuesta in respuestas:
            _validar_json_schema(json.loads(respuesta.model_dump_json()), esquema)

    def test_el_esquema_publica_los_campos_del_contrato(self) -> None:
        """Los nombres de la sección 3.4 no pueden cambiar sin romper a los consumidores."""
        esquema = RespuestaCanalAgnostica.model_json_schema()
        esperados = {
            "conversation_id",
            "trace_id",
            "bloques",
            "acciones",
            "derivacion",
            "gobernanza",
            "telemetria",
        }
        assert esperados <= set(esquema["properties"])
        assert set(esquema.get("required", [])) >= {"conversation_id", "trace_id", "gobernanza"}

    def test_los_bloques_llevan_discriminador_de_tipo(self, respuestas) -> None:
        """El canal decide cómo pintar cada bloque leyendo ``tipo``."""
        tipos_validos = {"texto", "kv", "puente", "tabla", "aviso", "ciclos"}
        vistos: set[str] = set()
        for respuesta in respuestas:
            for bloque in respuesta.bloques:
                assert bloque.tipo in tipos_validos
                vistos.add(bloque.tipo)
        assert {"texto", "kv"} <= vistos, "la suite debería ejercitar al menos texto y kv"

    def test_la_explicacion_visual_nunca_supera_dos_ciclos(self, respuestas) -> None:
        visuales = [
            bloque
            for respuesta in respuestas
            for bloque in respuesta.bloques
            if bloque.tipo == "ciclos"
        ]
        assert visuales, "los casos de variación deben ejercitar el componente de ciclos"
        assert all(1 <= len(bloque.ciclos) <= 2 for bloque in visuales)
        assert all(sum(ciclo.actual for ciclo in bloque.ciclos) == 1 for bloque in visuales)

    def test_el_factset_hace_ida_y_vuelta_y_conserva_su_sello(self, exige_dataset: None) -> None:
        from eval.datos import cargar_cuenta, factset_de_cuenta

        factset = factset_de_cuenta(cargar_cuenta("C-DEMO-01"))
        vuelta = FactSet.model_validate_json(factset.model_dump_json())

        assert vuelta.sha256 == factset.sha256
        assert vuelta.verificar_sha256() is True
        _validar_json_schema(json.loads(factset.model_dump_json()), FactSet.model_json_schema())


# --------------------------------------------------------------------------- #
# Nivel 2: contrato cerrado
# --------------------------------------------------------------------------- #
class TestContratoCerrado:
    """``extra="forbid"``: lo que no está declarado no entra."""

    @pytest.mark.parametrize(
        "modelo,base",
        [
            (
                RespuestaCanalAgnostica,
                {
                    "conversation_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                    "trace_id": "tr-0001",
                    "gobernanza": {
                        "anclado": True,
                        "verificacion_numerica": "PASS",
                        "modo": "PLANTILLA",
                        "rules_version": "1.0.0",
                        "model_version": "mock",
                        "factset_sha256": "0" * 64,
                    },
                },
            ),
            (PeticionExplicacion, {"utterance": "¿por qué me vino más caro?"}),
            (PeticionDerivacion, {"conversation_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"}),
            (RespuestaError, {"codigo": "INVARIANTE_FALLIDO", "detalle": "no concilia"}),
            (Derivacion, {"requerida": True}),
        ],
    )
    def test_un_campo_no_declarado_falla_ruidosamente(
        self, modelo: type, base: dict[str, Any]
    ) -> None:
        modelo.model_validate(base)  # el caso base es válido
        with pytest.raises(ValueError):
            modelo.model_validate({**base, "campo_inventado": 1})

    def test_el_utterance_tiene_tope_de_longitud(self) -> None:
        """Un mensaje de dos mil caracteres es el vector obvio de inyección por volumen."""
        PeticionExplicacion(utterance="a" * 2_000)
        with pytest.raises(ValueError):
            PeticionExplicacion(utterance="a" * 2_001)

    def test_las_enumeraciones_del_contrato_estan_completas(self) -> None:
        assert {nivel.value for nivel in NivelAseguramiento} == {
            "LOA0",
            "LOA1",
            "LOA2",
            "LOA_ASESOR",
        }
        assert {modo.value for modo in ModoGeneracion} == {"LLM", "LLM_REINTENTO", "PLANTILLA"}
        assert {veredicto.value for veredicto in VeredictoVerificacion} == {
            "PASS",
            "FAIL",
            "NO_APLICA",
        }


# --------------------------------------------------------------------------- #
# Nivel 3: invariantes de gobernanza (las mismas que la base de datos)
# --------------------------------------------------------------------------- #
class TestGobernanza:
    """``001_core.sql`` replica estas reglas como ``CHECK``: no pueden divergir."""

    def test_pass_exige_cero_aserciones_no_ancladas(self, respuestas) -> None:
        for respuesta in respuestas:
            gobernanza = respuesta.gobernanza
            if gobernanza.verificacion_numerica == VeredictoVerificacion.PASS:
                assert gobernanza.aserciones_no_ancladas == 0

    def test_anclado_equivale_a_no_ancladas_cero(self, respuestas) -> None:
        for respuesta in respuestas:
            gobernanza = respuesta.gobernanza
            assert gobernanza.anclado is (gobernanza.aserciones_no_ancladas == 0)

    def test_las_aserciones_suman(self, respuestas) -> None:
        for respuesta in respuestas:
            gobernanza = respuesta.gobernanza
            assert (
                gobernanza.aserciones_ancladas + gobernanza.aserciones_no_ancladas
                == gobernanza.aserciones_totales
            )

    def test_toda_respuesta_declara_su_trazabilidad(self, respuestas) -> None:
        """Sin ``factset_sha256`` no se puede reconstruir qué vio el modelo."""
        for respuesta in respuestas:
            gobernanza = respuesta.gobernanza
            assert len(gobernanza.factset_sha256) == 64
            assert gobernanza.rules_version
            assert gobernanza.model_version
            assert respuesta.trace_id

    def test_una_derivacion_declara_siempre_su_motivo(self, respuestas) -> None:
        """``explicacion`` en la base exige motivo y resumen si ``derivada``."""
        for respuesta in respuestas:
            if respuesta.derivacion.requerida:
                assert respuesta.derivacion.motivo_codigo is not None
                assert respuesta.derivacion.motivo

    def test_las_citas_apuntan_dentro_del_texto(self, respuestas) -> None:
        """Cada cita es un span ``[inicio, fin)`` sobre el texto realmente entregado."""
        for respuesta in respuestas:
            texto = respuesta.texto
            for cita in respuesta.gobernanza.citas:
                assert 0 <= cita.inicio < cita.fin <= len(texto)
                assert cita.fact_id

    def test_toda_respuesta_ofrece_una_salida_al_cliente(self, respuestas) -> None:
        """La ficha pide *"recomendación de siguientes acciones"*: nunca un callejón sin salida."""
        for respuesta in respuestas:
            assert respuesta.acciones
            for accion in respuesta.acciones:
                assert isinstance(accion, Accion)
                assert accion.etiqueta
                assert accion.riesgo in {"INFORMATIVA", "REVERSIBLE"}

    def test_la_telemetria_lleva_la_sonda_de_silencio(self, respuestas) -> None:
        """*"Tasa de silencio post-explicación"* (ficha B.9): la sonda se abre siempre."""
        for respuesta in respuestas:
            assert respuesta.telemetria.get("silence_probe_id")


# --------------------------------------------------------------------------- #
# Evidencia
# --------------------------------------------------------------------------- #
def test_item_evidencia_respeta_su_contrato() -> None:
    """``GET /v1/evidencia/{id}`` devuelve ``tipo, ref_id, snippet`` (sección 9)."""
    item = ItemEvidencia(tipo="cat", ref_id="cat:RENTA_PLAN_MOVIL", snippet="El cargo mensual…")
    esquema = ItemEvidencia.model_json_schema()

    assert {"tipo", "ref_id", "snippet"} <= set(esquema["properties"])
    _validar_json_schema(json.loads(item.model_dump_json()), esquema)
    with pytest.raises(ValueError):
        ItemEvidencia.model_validate({"tipo": "cat", "ref_id": "x", "snippet": "y", "extra": True})


def test_gobernanza_rechaza_un_veredicto_inventado() -> None:
    with pytest.raises(ValueError):
        Gobernanza(
            anclado=True,
            verificacion_numerica="CASI",  # type: ignore[arg-type]
            modo=ModoGeneracion.PLANTILLA,
            rules_version="1.0.0",
            model_version="mock",
            factset_sha256="0" * 64,
        )
