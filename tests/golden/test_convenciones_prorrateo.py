"""La suite golden bajo las DOS convenciones de prorrateo.

``convencion_prorrateo`` es uno de los dos parámetros marcados **[POR VALIDAR]** en
``rules.yaml``: ``actual`` reparte sobre los días reales del ciclo (28/29/30/31) y
``30_360`` fuerza meses de 30 días. Movistar no ha confirmado cuál usa, así que el
sistema tiene que comportarse bien con las dos y hay que poder demostrarlo.

No es un campo del caso golden a propósito: es **política global**, no un atributo del
recibo, y meterla en el YAML habría dado a entender que un mismo operador factura con dos
convenciones a la vez. Se cubre ejecutando la misma suite con la variable de entorno,
sin tocar ni un caso::

    python -m eval.run_eval --modo mock
    CONVENCION_PRORRATEO=30_360 python -m eval.run_eval --modo mock

Lo que estos tests fijan es lo que **no puede** cambiar al cambiar de convención:

* ni un céntimo de la respuesta, porque los importes salen del recibo emitido y no de un
  recálculo; la convención solo interviene al **verificar** el prorrateo, no al facturarlo;
* el invariante sigue cerrando exacto;
* ninguna cifra escapa del FactSet.

Y lo que sí cambia, que es lo interesante: con ``30_360`` sobre un ciclo de 31 días el
motor recalcula el prorrateo y **no reproduce** el importe facturado, así que topa la
confianza de esa línea en ``tope_prorrateo_inconsistente`` (0,50). Es el comportamiento
correcto —declarar menos certeza cuando la explicación no cuadra con el recibo— y la
propiedad que se comprueba es la dirección: cambiar de convención nunca puede **subir**
la confianza declarada.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from packages.core_domain.enums import VeredictoVerificacion

pytestmark = pytest.mark.golden

#: Escenarios donde el prorrateo hace trabajo de verdad: la renta se parte en tramos.
ESCENARIOS_PRORRATEADOS = frozenset({"CAMBIO_PLAN_MEDIO_CICLO", "CORTE_RECONEXION"})

#: Tope de casos, para que el test no dure más que el resto de la suite golden junta.
MAXIMO_CASOS = 24


def _casos_prorrateados() -> list:
    """Subconjunto estratificado: los casos donde la convención puede notarse."""
    try:
        from eval.datos import cargar_golden
    except Exception:  # pragma: no cover - sin dataset
        return []
    try:
        casos = cargar_golden()
    except Exception:  # pragma: no cover - sin dataset
        return []
    elegibles = [
        caso
        for caso in casos
        if set(caso.escenarios) & ESCENARIOS_PRORRATEADOS and not caso.debe_derivar
    ]
    # Uno de cada k, para no medir 60 veces lo mismo y aun así cubrir las dos modalidades.
    paso = max(1, len(elegibles) // MAXIMO_CASOS)
    return elegibles[::paso][:MAXIMO_CASOS]


CASOS = _casos_prorrateados()


@pytest.fixture()
def reglas_30_360(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Reglas con la convención 30/360, restaurando la caché al salir.

    ``cargar_reglas`` cachea por ``(ruta, COBRO_EN_SUSPENSION, CONVENCION_PRORRATEO)``, así
    que basta con la variable de entorno; lo que **no** basta es olvidarse de limpiar la
    caché, porque el resto de la suite se quedaría con la convención cambiada.
    """
    from packages.core_domain.reglas import cargar_reglas, limpiar_cache_reglas

    monkeypatch.setenv("CONVENCION_PRORRATEO", "30_360")
    limpiar_cache_reglas()
    try:
        yield cargar_reglas()
    finally:
        monkeypatch.delenv("CONVENCION_PRORRATEO", raising=False)
        limpiar_cache_reglas()


@pytest.fixture(scope="module", autouse=True)
def _hay_casos() -> None:
    """Omite el módulo si falta el dataset."""
    if not CASOS:
        pytest.skip("faltan los casos golden o el dataset sintético")


def test_la_convencion_del_entorno_llega_a_las_reglas(reglas_30_360) -> None:
    """Sin esto, todo lo demás pasaría por no estar midiendo nada."""
    from packages.core_domain.enums import ConvencionProrrateo

    assert reglas_30_360.politica.convencion_prorrateo is ConvencionProrrateo.TREINTA_360
    assert reglas_30_360.dias_ciclo_efectivos(31) == 30
    assert reglas_30_360.dias_ciclo_efectivos(28) == 30


def test_las_cifras_no_cambian_con_la_convencion(reglas_30_360) -> None:
    """Totales, delta e invariante, idénticos con 30/360.

    Es la propiedad que un lector externo necesita: la convención es un parámetro de
    **verificación**, no de facturación. El recibo ya viene emitido; el motor lo explica.
    """
    from eval.datos import cargar_cuenta, factset_de_cuenta

    for caso in CASOS:
        cuenta = cargar_cuenta(caso.cuenta_id)
        factset = factset_de_cuenta(cuenta, caso.periodo, reglas_30_360)
        assert factset.total_actual_cent == caso.total_esperado_cent, caso.caso_id
        assert factset.delta_total_cent == caso.delta_esperado_cent, caso.caso_id
        assert factset.invariante.ok is True, caso.caso_id
        assert factset.invariante.residual_cent == 0, caso.caso_id


def test_sin_alucinaciones_tambien_con_la_otra_convencion(reglas_30_360) -> None:
    """TA_respuesta = 0 con 30/360. La garantía es estructural, no depende de la política."""
    from eval.datos import cargar_cuenta, factset_de_cuenta
    from packages.llm_layer import explicar, extraer_numeros

    for caso in CASOS:
        cuenta = cargar_cuenta(caso.cuenta_id)
        factset = factset_de_cuenta(cuenta, caso.periodo, reglas_30_360)
        respuesta = explicar(
            factset,
            modo="mock",
            verbosidad=caso.verbosidad,
            utterance=caso.utterance,
            canal=caso.canal,
        )
        infractores = extraer_numeros(respuesta.texto) - factset.tokens_permitidos()
        assert infractores == set(), f"{caso.caso_id}: {sorted(infractores)}"
        assert respuesta.gobernanza.verificacion_numerica == VeredictoVerificacion.PASS
        assert respuesta.gobernanza.aserciones_no_ancladas == 0


def test_la_convencion_ajena_nunca_sube_la_confianza(reglas_30_360) -> None:
    """Con 30/360 sobre ciclos de 31 días, la confianza baja o se queda igual: nunca sube.

    Es el comportamiento que se quiere. Si el recálculo del prorrateo no reproduce el
    importe facturado, el motor no puede seguir afirmando con la misma seguridad *por
    qué* ese importe es el que es; lo que hace es topar la confianza de esa línea, no
    cambiar la cifra. Un sistema que mantuviera la confianza intacta estaría prometiendo
    una certeza que no tiene.
    """
    from eval.datos import cargar_cuenta, factset_de_cuenta
    from packages.core_domain.reglas import cargar_reglas, limpiar_cache_reglas

    limpiar_cache_reglas()
    reglas_actual = cargar_reglas(aplicar_entorno=False)

    bajaron = 0
    for caso in CASOS:
        cuenta = cargar_cuenta(caso.cuenta_id)
        con_actual = factset_de_cuenta(cuenta, caso.periodo, reglas_actual)
        con_30_360 = factset_de_cuenta(cuenta, caso.periodo, reglas_30_360)
        assert con_30_360.confianza_global <= con_actual.confianza_global + 1e-9, caso.caso_id
        bajaron += con_30_360.confianza_global < con_actual.confianza_global
    assert bajaron > 0, (
        "con 30/360 no bajó la confianza en ningún caso prorrateado: o el subconjunto no "
        "tiene tramos o la verificación del prorrateo dejó de aplicarse"
    )
