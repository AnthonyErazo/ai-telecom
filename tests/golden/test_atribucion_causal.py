"""La narrativa causal, fijada de extremo a extremo (defecto de atribución causal).

Sobre C-DEMO-01 conviven dos causas de signo contrario y el sistema tenía las tres
líneas agregadas en una sola::

    DESCUENTO_PROMOCIONAL (desapareció)   +49.90   ->  CAMBIO_PLAN
    RENTA_PLAN_MOVIL      (plan más barato) −20.00 ->  CAMBIO_PLAN
    AJUSTE_RETROACTIVO_RENTA (nuevo)      −12.26   ->  CAMBIO_PLAN
    ================================================================
    "cambio de plan  +17.64"  ->  «su recibo subió porque cambió de plan»

El invariante cerraba en 0 y la verificación numérica daba PASS: el defecto **no era
aritmético**. Lo que subió el recibo fue el fin del descuento (+49.90); el cambio de
plan, por sí solo, se lo bajó S/ 32.26. Esta prueba fija las cuatro propiedades que
convierten esa explicación en verdadera, y lo hace sobre el texto que lee el cliente:

1. La desaparición del descuento se atribuye a ``FIN_DESCUENTO``.
2. Las causas agregadas quedan separadas por signo, ordenadas por impacto absoluto.
3. El texto nombra el fin del descuento como causa del aumento **y** reconoce el ahorro.
4. Nada de lo anterior relaja el anclaje: invariante 0 y verificación PASS.

La segunda parte del módulo repite la comprobación sobre un par de recibos construidos
en el propio test, en renta VENCIDA y con el descuento prorrateado en vez de
desaparecido, para que la garantía no dependa del dataset generado.
"""

from __future__ import annotations

from datetime import date

import pytest

from packages.core_domain.enums import (
    ClaseDelta,
    EstadoServicio,
    FamiliaConcepto,
    ModalidadRenta,
    TipoMovimiento,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.esquemas.recibo import LineaRecibo, Recibo
from packages.facts_engine.motor import construir_factset
from packages.llm_layer import explicar

pytestmark = pytest.mark.golden

CUENTA_DEMO = "C-DEMO-01"
PERIODO_DEMO = "2026-07"


@pytest.fixture(scope="module")
def factset_demo(request: pytest.FixtureRequest):
    """FactSet de C-DEMO-01, el recibo insignia del desafío."""
    if not request.getfixturevalue("dataset_disponible"):
        pytest.skip(
            "falta el dataset sintético: ejecute "
            "`python -m packages.datagen.generar --seed 20260804 --clientes 300`"
        )
    from eval.datos import cargar_cuenta, factset_de_cuenta

    return factset_de_cuenta(cargar_cuenta(CUENTA_DEMO), PERIODO_DEMO)


class TestAtribucionSobreElReciboInsignia:
    """C-DEMO-01: cambio de plan que mata la promoción atada al plan anterior."""

    def test_el_descuento_que_desaparece_se_atribuye_al_fin_de_descuento(
        self, factset_demo
    ) -> None:
        descuento = factset_demo.linea("DESCUENTO_PROMOCIONAL")

        assert descuento is not None, "el recibo insignia tiene que perder su descuento"
        assert descuento.clase is ClaseDelta.DESAPARECIDO
        assert descuento.delta_cent > 0, "perder un descuento SUBE el recibo"
        assert descuento.causa is TipoMovimiento.FIN_DESCUENTO, (
            "la desaparición de una promoción no la explica el cambio de plan"
        )

    def test_el_cambio_de_plan_conserva_su_causa_y_su_signo(self, factset_demo) -> None:
        """El caso inverso dentro del mismo recibo: la renta sí la explica el cambio."""
        renta = factset_demo.linea("RENTA_PLAN_MOVIL")
        ajuste = factset_demo.linea("AJUSTE_RETROACTIVO_RENTA")

        assert renta is not None and ajuste is not None
        assert renta.causa is TipoMovimiento.CAMBIO_PLAN
        assert ajuste.causa is TipoMovimiento.CAMBIO_PLAN
        assert renta.delta_cent + ajuste.delta_cent < 0, (
            "el cambio de plan por sí solo BAJA el recibo: ese es el punto del caso"
        )

    def test_las_causas_agregadas_separan_los_signos_y_ordenan_por_impacto(
        self, factset_demo
    ) -> None:
        causas = factset_demo.causas_agregadas

        assert len(causas) >= 2, "agregar todo en una sola causa es el defecto"
        suben = [causa for causa in causas if causa.monto_cent > 0]
        bajan = [causa for causa in causas if causa.monto_cent < 0]
        assert suben and bajan, "tiene que haber al menos dos entradas de signo distinto"

        impactos = [abs(causa.monto_cent) for causa in causas]
        assert impactos == sorted(impactos, reverse=True), "orden por impacto absoluto"

        primera = causas[0]
        assert primera.causa is TipoMovimiento.FIN_DESCUENTO
        assert primera.monto_cent > 0, "la primera causa debe explicar la subida"
        assert sum(causa.monto_cent for causa in causas) == factset_demo.delta_total_cent

    def test_el_invariante_sigue_cerrando_en_cero(self, factset_demo) -> None:
        assert factset_demo.invariante.ok is True
        assert factset_demo.invariante.residual_cent == 0

    @pytest.mark.parametrize("verbosidad", ["CORTO", "DETALLE"])
    def test_el_texto_nombra_el_fin_del_descuento_y_reconoce_el_ahorro(
        self, factset_demo, verbosidad: str
    ) -> None:
        """La comprobación que de verdad importa: lo que lee el cliente."""
        respuesta = explicar(
            factset_demo,
            modo="mock",
            verbosidad=verbosidad,
            utterance="cambié a un plan más barato y me cobran más, ¿por qué?",
        )
        texto = respuesta.texto.lower()

        assert "descuento" in texto, "el aumento se explica por el fin del descuento"
        assert "ahorr" in texto, "el ahorro del cambio de plan tiene que reconocerse"
        assert "cambió de plan a mitad de mes" not in texto, (
            "es la frase de la explicación engañosa que este defecto producía"
        )
        assert respuesta.gobernanza.verificacion_numerica == VeredictoVerificacion.PASS
        assert respuesta.gobernanza.aserciones_no_ancladas == 0


# --------------------------------------------------------------------------- #
# La misma garantía sin depender del dataset generado
# --------------------------------------------------------------------------- #
def _linea(
    linea_id: int,
    concepto_id: str,
    nombre: str,
    familia: FamiliaConcepto,
    monto_cent: int,
    *,
    afecto_igv: bool = True,
) -> LineaRecibo:
    """Línea mínima de recibo, sin tramos: aquí solo se mide la atribución."""
    return LineaRecibo(
        linea_id=linea_id,
        concepto_id=concepto_id,
        nombre_comercial=nombre,
        familia=familia,
        monto_cent=monto_cent,
        periodo="2026-07",
        afecto_igv=afecto_igv,
    )


def _recibo(periodo: str, lineas: list[LineaRecibo], inicio: date, fin: date) -> Recibo:
    """Recibo VENCIDA de un ciclo mensual completo."""
    return Recibo(
        recibo_id=f"R-C-PRUEBA-{periodo}",
        cuenta_id="C-PRUEBA",
        periodo=periodo,
        modalidad_renta=ModalidadRenta.VENCIDA,
        ciclo_inicio=inicio,
        ciclo_fin=fin,
        dias_ciclo=(fin - inicio).days,
        fecha_emision=fin,
        fecha_vencimiento=fin,
        lineas=lineas,
        total_cent=sum(linea.monto_cent for linea in lineas),
        estado_servicio=EstadoServicio.ACTIVO,
    )


def test_descuento_prorrateado_en_vencida_no_hereda_el_cambio_de_plan(reglas) -> None:
    """Renta VENCIDA, descuento que se encoge y un solo movimiento: el cambio de plan.

    Es el mismo defecto en la otra modalidad y con la otra clase de variación. Sin la
    preferencia por concepto, el descuento heredaría ``CAMBIO_PLAN`` por ser el único
    movimiento de la ventana.
    """
    inicio, fin = date(2026, 7, 1), date(2026, 8, 1)
    previo = _recibo(
        "2026-06",
        [
            _linea(1, "RENTA_PLAN_MOVIL", "Plan móvil", FamiliaConcepto.RECURRENTE, 9_990),
            _linea(2, "DESCUENTO_PROMOCIONAL", "Descuento", FamiliaConcepto.CREDITO, -3_000),
        ],
        date(2026, 6, 1),
        inicio,
    )
    actual = _recibo(
        "2026-07",
        [
            _linea(1, "RENTA_PLAN_MOVIL", "Plan móvil", FamiliaConcepto.RECURRENTE, 8_500),
            _linea(2, "DESCUENTO_PROMOCIONAL", "Descuento", FamiliaConcepto.CREDITO, -1_200),
        ],
        inicio,
        fin,
    )
    movimientos = [
        MovementEvent(
            movimiento_id=77,
            cuenta_id="C-PRUEBA",
            tipo=TipoMovimiento.CAMBIO_PLAN,
            ocurrido_en=date(2026, 7, 12),
            detalle={
                "plan_anterior": "Plan Movil Ilimitado",
                "plan_nuevo": "Plan Movil Max 50GB",
                "tarifa_anterior_cent": 9_990,
                "tarifa_nueva_cent": 7_990,
            },
        )
    ]

    factset = construir_factset(actual, [previo], movimientos, reglas)
    descuento = factset.linea("DESCUENTO_PROMOCIONAL")
    renta = factset.linea("RENTA_PLAN_MOVIL")

    assert descuento is not None and renta is not None
    assert descuento.clase is ClaseDelta.SUBIO
    assert descuento.causa is TipoMovimiento.FIN_DESCUENTO
    assert renta.causa is TipoMovimiento.CAMBIO_PLAN
    assert factset.invariante.ok is True
    assert factset.invariante.residual_cent == 0

    causas = {causa.causa for causa in factset.causas_agregadas}
    assert {TipoMovimiento.FIN_DESCUENTO, TipoMovimiento.CAMBIO_PLAN} <= causas


# --------------------------------------------------------------------------- #
# El signo de cada línea, en la frase que la nombra
# --------------------------------------------------------------------------- #
# Segunda mentira de la misma familia, hallada al revisar el texto de la demo: la cifra
# estaba anclada y el verificador daba PASS, pero la frase contaba lo contrario de lo que
# pasó. La macro ``lista_lineas`` ramificaba solo por CLASE:
#
#     NUEVO        -> "aparece por {delta}"          (da igual el signo)
#     DESAPARECIDO -> "ya no se le cobra, eran ..."  (da igual el signo)
#
# En C-DEMO-01 eso producía «Ajuste del mes anterior aparece por S/ 12.26» de un abono
# **a favor** del cliente de S/ 12.26, y «Descuento por permanencia ya no se le cobra» de
# un descuento, que no se cobraba: se aplicaba. Ahora la macro ramifica también por
# signo, y esto lo fija.
class TestElSignoDeCadaLineaEnElTexto:
    """Una cifra anclada con la frase al revés sigue siendo una explicación falsa."""

    @pytest.fixture(scope="class")
    def texto_detalle(self, factset_demo) -> str:
        """El bloque DETALLE es el único que enumera línea por línea."""
        return explicar(
            factset_demo,
            modo="mock",
            verbosidad="DETALLE",
            utterance="explíqueme el detalle línea por línea",
        ).texto

    def test_un_abono_nuevo_no_se_anuncia_como_un_cargo(
        self, factset_demo, texto_detalle: str
    ) -> None:
        ajuste = factset_demo.linea("AJUSTE_RETROACTIVO_RENTA")
        assert ajuste is not None
        assert ajuste.clase is ClaseDelta.NUEVO and ajuste.delta_cent < 0, (
            "la premisa del test: línea nueva y a favor del cliente"
        )
        assert "Ajuste del mes anterior aparece a su favor por S/ 12.26" in texto_detalle
        assert "Ajuste del mes anterior aparece por" not in texto_detalle, (
            "el ajuste retroactivo de C-DEMO-01 es un abono de −S/ 12.26: decir que "
            "«aparece por S/ 12.26» lo presenta como un cargo nuevo"
        )

    def test_un_descuento_que_vence_no_se_deja_de_cobrar_se_deja_de_aplicar(
        self, texto_detalle: str
    ) -> None:
        assert "Descuento por permanencia ya no se le aplica, eran S/ 49.90" in texto_detalle
        assert "Descuento por permanencia ya no se le cobra" not in texto_detalle, (
            "un descuento no se cobraba: se aplicaba, y por eso al vencer el recibo SUBE"
        )

    def test_la_verificacion_numerica_no_se_relaja(self, factset_demo) -> None:
        respuesta = explicar(
            factset_demo,
            modo="mock",
            verbosidad="DETALLE",
            utterance="explíqueme el detalle línea por línea",
        )
        assert respuesta.gobernanza.verificacion_numerica == VeredictoVerificacion.PASS
        assert respuesta.gobernanza.aserciones_no_ancladas == 0
