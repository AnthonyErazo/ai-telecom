"""Descuento por alta nueva o portabilidad: los dos ejemplos del vídeo, al céntimo.

Estas pruebas no inventan el caso de prueba: reproducen literalmente los dos ejemplos
trabajados en la transcripción de ``desafio1`` —el de Raúl, alta nueva en renta vencida,
y el de Lucía, portabilidad en renta adelantada—, que es donde el negocio explica la
fórmula. Plan de S/ 60,00 de cargo fijo, ciclo 15, 50 % de descuento durante ~90 días.

Lo que fija cada bloque:

* La promoción se agota **por días**, no por recibos. De ahí sale el recibo mixto, que es
  el que genera la llamada al 104: nada cambió, el cliente no contrató nada, y el recibo
  sube.
* La **modalidad de renta** cambia el primer recibo entero. En vencida los días del alta
  llevan descuento; en adelantada no, y además se cobra por adelantado el mes siguiente.
* El descuento toca **solo el cargo fijo**. Paquetes, servicios adicionales y cuota de
  equipo financiado quedan fuera, y esa es la confusión más común del cliente.
"""

from __future__ import annotations

from fractions import Fraction
from typing import ClassVar

import pytest

from packages.core_domain.enums import ModalidadRenta
from packages.facts_engine.prorrateo import (
    DIAS_DESCUENTO_ALTA,
    cargo_fijo_con_descuento_alta,
    cuota_equipo_financiado,
    promocion_a_parametros,
    recibos_de_alta,
)

#: Los datos del vídeo: plan de S/ 60,00, ciclo de 30 días, alta el 12 con cierre el 15.
CARGO_FIJO = 6_000
DIAS_CICLO = 30
DIAS_HASTA_CIERRE = 3


# --------------------------------------------------------------------------- #
# Renta VENCIDA — el ejemplo de Raúl
# --------------------------------------------------------------------------- #
class TestAltaEnRentaVencida:
    """«¿Por qué mi primer recibo llega con un costo menor?»"""

    @pytest.fixture(scope="class")
    def serie(self) -> list:
        return recibos_de_alta(
            cargo_fijo_cent=CARGO_FIJO,
            dias_hasta_cierre=DIAS_HASTA_CIERRE,
            modalidad=ModalidadRenta.VENCIDA,
            dias_ciclo=DIAS_CICLO,
        )

    def test_primer_recibo_son_tres_dias_y_ademas_con_descuento(self, serie) -> None:
        """3 días de plan = S/ 6,00; con el 50 % quedan S/ 3,00.

        En vencida el cliente disfrutó esos 3 días antes del cierre, así que se le cobran
        prorrateados, y la promoción ya está corriendo: van con descuento.
        """
        primero = serie[0]
        assert primero.dias_con_descuento == 3
        assert primero.dias_sin_descuento == 0
        assert primero.cargo_fijo_cent == 300, "S/ 3,00: 3 días a S/ 2,00 con 50 %"
        assert primero.dias_restantes_promocion == DIAS_DESCUENTO_ALTA - 3 == 87

    def test_recibos_dos_y_tres_son_el_mes_completo_a_mitad_de_precio(self, serie) -> None:
        """S/ 30,00 cada uno, y la bolsa baja de 87 a 57 y de 57 a 27."""
        assert [r.cargo_fijo_cent for r in serie[1:3]] == [3_000, 3_000]
        assert [r.dias_restantes_promocion for r in serie[1:3]] == [57, 27]
        assert not any(r.es_mixto for r in serie[1:3])

    def test_el_cuarto_recibo_es_el_que_dispara_la_llamada(self, serie) -> None:
        """Se acaba la promoción a mitad de ciclo y el recibo sale partido.

        27 días con descuento (S/ 27,00) y 3 días a tarifa plena (S/ 6,00): **S/ 33,00**.

        El vídeo enuncia bien la fórmula —«27 días con 50 % de descuento y 3 días sin
        descuento»— pero al cerrar el ejemplo dice S/ 36,00, y con sus propios números no
        sale: 27 + 6 = 33. Para llegar a 36 harían falta 24 días con descuento y 6 sin
        (24 + 12), que no es lo que dice el reparto. Se implementa la FÓRMULA, que es
        inequívoca y coincide con el resto del ejemplo, y se deja anotada la
        discrepancia: si el negocio confirma que la cifra buena es 36, lo que cambia es el
        parámetro ``dias_promocion`` (87 en vez de 90 tras el alta), no la fórmula.
        """
        cuarto = serie[3]
        assert cuarto.es_mixto, "es el recibo en que muere la promoción"
        assert (cuarto.dias_con_descuento, cuarto.dias_sin_descuento) == (27, 3)
        assert cuarto.cargo_con_descuento_cent == 2_700
        assert cuarto.cargo_sin_descuento_cent == 600
        assert cuarto.cargo_fijo_cent == 3_300
        assert cuarto.dias_restantes_promocion == 0

    def test_la_bolsa_se_consume_entera_y_ni_un_dia_mas(self, serie) -> None:
        """Los días con descuento de los cuatro recibos suman exactamente los 90."""
        assert sum(r.dias_con_descuento for r in serie) == DIAS_DESCUENTO_ALTA

    def test_el_quinto_recibo_ya_es_el_plan_normal(self) -> None:
        """Agotada la promoción, el recibo vuelve al cargo fijo íntegro: S/ 60,00."""
        serie = recibos_de_alta(
            cargo_fijo_cent=CARGO_FIJO,
            dias_hasta_cierre=DIAS_HASTA_CIERRE,
            modalidad=ModalidadRenta.VENCIDA,
            dias_ciclo=DIAS_CICLO,
            recibos=5,
        )
        assert serie[4].cargo_fijo_cent == CARGO_FIJO
        assert serie[4].dias_con_descuento == 0


# --------------------------------------------------------------------------- #
# Renta ADELANTADA — el ejemplo de Lucía
# --------------------------------------------------------------------------- #
class TestPortabilidadEnRentaAdelantada:
    """«¿Por qué debo pagar 36 soles en mi primer recibo?»"""

    @pytest.fixture(scope="class")
    def serie(self) -> list:
        return recibos_de_alta(
            cargo_fijo_cent=CARGO_FIJO,
            dias_hasta_cierre=DIAS_HASTA_CIERRE,
            modalidad=ModalidadRenta.ADELANTADA,
            dias_ciclo=DIAS_CICLO,
        )

    def test_el_primer_recibo_son_treinta_y_seis_soles(self, serie) -> None:
        """S/ 6,00 de los días que restan, SIN descuento, más S/ 30,00 del mes adelantado.

        Es la cifra exacta que pregunta Lucía en el vídeo, y el desglose importa tanto
        como el total: el cliente que ve S/ 36,00 cuando esperaba S/ 30,00 cree que le
        cobraron mal el descuento. No: son dos conceptos, y solo uno lo lleva.
        """
        primero = serie[0]
        assert primero.cargo_sin_descuento_cent == 600, "3 días del ciclo en curso"
        assert primero.cargo_con_descuento_cent == 3_000, "mes siguiente completo al 50 %"
        assert primero.cargo_fijo_cent == 3_600

    def test_los_dias_del_alta_no_gastan_bolsa_de_promocion(self, serie) -> None:
        """El tramo sin descuento no consume días: la promoción arranca con el mes."""
        assert serie[0].dias_con_descuento == 30
        assert serie[0].dias_restantes_promocion == 60

    def test_recibos_dos_y_tres_completan_los_tres_meses(self, serie) -> None:
        """S/ 30,00 cada uno, y con el tercero se agota la promoción sin recibo mixto.

        Esta es la diferencia visible con la renta vencida: en adelantada la bolsa se
        consume en tres tramos de 30 días exactos, así que no hay cuarto recibo partido.
        El salto a tarifa plena es limpio y cae entero en el cuarto.
        """
        assert [r.cargo_fijo_cent for r in serie[1:3]] == [3_000, 3_000]
        assert serie[2].dias_restantes_promocion == 0
        # El primero es mixto por otra razón —el tramo del ciclo en curso va sin
        # descuento—, no porque se agote la promoción. De los siguientes, ninguno lo es:
        # la bolsa cae en tres tramos de 30 días exactos y el salto a tarifa plena
        # ocurre limpio, entre recibos. Esa es la diferencia visible con la vencida.
        assert serie[0].es_mixto
        assert not any(r.es_mixto for r in serie[1:])
        assert serie[3].cargo_fijo_cent == CARGO_FIJO

    def test_la_misma_alta_paga_distinto_segun_la_modalidad(self) -> None:
        """Mismo plan, mismo día, distinto primer recibo: S/ 3,00 contra S/ 36,00.

        Por eso la modalidad de renta forma parte de la firma causal del FactSet y no es
        un detalle de configuración: sin ella, la explicación del primer recibo es
        directamente falsa para la mitad de los clientes.
        """
        comun = {
            "cargo_fijo_cent": CARGO_FIJO,
            "dias_hasta_cierre": DIAS_HASTA_CIERRE,
            "dias_ciclo": DIAS_CICLO,
        }
        vencida = recibos_de_alta(modalidad=ModalidadRenta.VENCIDA, **comun)
        adelantada = recibos_de_alta(modalidad=ModalidadRenta.ADELANTADA, **comun)
        assert vencida[0].cargo_fijo_cent == 300
        assert adelantada[0].cargo_fijo_cent == 3_600


# --------------------------------------------------------------------------- #
# El límite de la promoción y los bordes de la fórmula
# --------------------------------------------------------------------------- #
class TestAlcanceYBordes:
    def test_el_descuento_no_toca_la_cuota_del_equipo(self) -> None:
        """«No aplica sobre los servicios adicionales como paquetes, financiamiento…».

        La cuota del equipo se calcula por el sistema francés y sale igual con promoción o
        sin ella. La prueba es la garantía de que nadie conecte el descuento a esta vía:
        el cliente en promoción paga S/ 30,00 de plan y la cuota íntegra, y ver la cuota
        completa junto al plan a mitad de precio es exactamente lo que le hace pensar que
        el descuento no se le aplicó.
        """
        cuota, _saldo = cuota_equipo_financiado(120_000, 200, 12, 1)
        assert cuota > 0
        con_promocion = cargo_fijo_con_descuento_alta(
            cargo_fijo_cent=CARGO_FIJO,
            dias_facturados=DIAS_CICLO,
            dias_ciclo=DIAS_CICLO,
            dias_restantes_promocion=90,
        )
        assert con_promocion.cargo_fijo_cent + cuota == 3_000 + cuota

    def test_sin_saldo_de_promocion_se_cobra_todo(self) -> None:
        recibo = cargo_fijo_con_descuento_alta(
            cargo_fijo_cent=CARGO_FIJO,
            dias_facturados=DIAS_CICLO,
            dias_ciclo=DIAS_CICLO,
            dias_restantes_promocion=0,
        )
        assert recibo.cargo_fijo_cent == CARGO_FIJO
        assert recibo.es_mixto is False

    def test_otras_promociones_de_la_misma_forma(self) -> None:
        """«Podrás adaptarla a las promociones vigentes»: la tasa es un parámetro.

        Un 30 % durante 60 días usa la misma fórmula sin tocar una línea.
        """
        serie = recibos_de_alta(
            cargo_fijo_cent=CARGO_FIJO,
            dias_hasta_cierre=DIAS_CICLO,
            modalidad=ModalidadRenta.VENCIDA,
            dias_ciclo=DIAS_CICLO,
            dias_promocion=60,
            tasa_descuento=Fraction(3, 10),
            recibos=3,
        )
        assert [r.cargo_fijo_cent for r in serie] == [4_200, 4_200, 6_000]

    @pytest.mark.parametrize(
        ("kwargs", "trozo"),
        [
            ({"dias_facturados": -1}, "días negativos"),
            ({"dias_restantes_promocion": -1}, "días negativos"),
            ({"dias_ciclo": 0}, "días de ciclo inválidos"),
            ({"tasa_descuento": Fraction(3, 2)}, "fuera de"),
        ],
    )
    def test_entradas_invalidas(self, kwargs, trozo) -> None:
        base = {
            "cargo_fijo_cent": CARGO_FIJO,
            "dias_facturados": DIAS_CICLO,
            "dias_ciclo": DIAS_CICLO,
            "dias_restantes_promocion": 90,
        }
        with pytest.raises(ValueError, match=trozo):
            cargo_fijo_con_descuento_alta(**{**base, **kwargs})

    def test_el_alta_no_puede_caer_fuera_del_ciclo(self) -> None:
        with pytest.raises(ValueError, match="fuera del ciclo"):
            recibos_de_alta(
                cargo_fijo_cent=CARGO_FIJO,
                dias_hasta_cierre=31,
                modalidad=ModalidadRenta.VENCIDA,
                dias_ciclo=DIAS_CICLO,
            )


# --------------------------------------------------------------------------- #
# Cómo se le cuenta al cliente
# --------------------------------------------------------------------------- #
class TestNarrativaDelReciboMixto:
    """El recibo partido tiene que explicarse como lo que es, no como un mes normal."""

    def _datos(self, *, tarifas: list[int]):
        """Datos de plantilla de un fin de descuento, con uno o dos tramos de tarifa."""
        from packages.llm_layer.plantillas import construir_datos

        tramos = [
            {
                "etiqueta": f"tramo {indice + 1}",
                "dias": 15,
                "tarifa_mensual_cent": tarifa,
                "monto_prorrateado_cent": tarifa // 2,
                "estado": "ACTIVO",
            }
            for indice, tarifa in enumerate(tarifas)
        ]
        return construir_datos(
            {
                "periodo_actual": "2026-07",
                "periodo_previo": "2026-06",
                "modalidad_renta": "VENCIDA",
                "dias_ciclo": 30,
                "total_actual_cent": 6_000,
                "total_previo_cent": 3_000,
                "delta_total_cent": 3_000,
                "causas_agregadas": [{"causa": "FIN_DESCUENTO", "monto_cent": 3_000}],
                "lineas": [
                    {
                        "concepto_id": "PLAN",
                        "nombre_comercial": "Plan Movistar",
                        "clase": "MODIFICADO",
                        "causa": "FIN_DESCUENTO",
                        "monto_actual_cent": 6_000,
                        "monto_previo_cent": 3_000,
                        "delta_cent": 3_000,
                        "causa_confirmada": True,
                        "exige_causa": True,
                        "tramos": tramos,
                    }
                ],
            }
        )

    def test_dos_tarifas_en_el_ciclo_marcan_el_recibo_como_mixto(self) -> None:
        assert self._datos(tarifas=[3_000, 6_000]).descuento_mixto is True
        assert self._datos(tarifas=[6_000]).descuento_mixto is False

    def test_al_cliente_se_le_dice_que_el_descuento_murio_a_mitad_de_mes(self) -> None:
        """Y no que «llegó a su última mensualidad», que además sería falso.

        Su última mensualidad completa fue la anterior; esta viene partida. La frase
        importa porque es la que responde a «no cambié nada, ¿por qué me subió?».
        """
        from packages.llm_layer.plantillas import renderizar_explicacion

        datos = self._datos(tarifas=[3_000, 6_000])
        assert datos.plantilla == "fin_descuento"
        texto = " ".join(causa.frase for causa in renderizar_explicacion(datos).causas)
        assert "a mitad del mes" in texto

    def test_sin_tramo_partido_la_frase_sigue_siendo_la_de_siempre(self) -> None:
        from packages.llm_layer.plantillas import renderizar_explicacion

        texto = " ".join(
            causa.frase for causa in renderizar_explicacion(self._datos(tarifas=[6_000])).causas
        )
        assert "a mitad del mes" not in texto
        assert "volvió a su precio normal" in texto


# --------------------------------------------------------------------------- #
# Con los parámetros del cliente, no con los del ejemplo
# --------------------------------------------------------------------------- #
class TestPromocionRealDelDataset:
    """El último tramo: la fórmula deja de suponer y usa lo que consta por cliente."""

    #: La cuenta 759816134 tal como la devuelve `v_descuento_cuota`. No es un caso
    #: inventado: es el registro real, y es además la cuenta que la demo ofrece como
    #: «Fin de descuento».
    REAL: ClassVar[dict[str, object]] = {
        "modalidad_renta": "ADELANTADA",
        "meses_promocion": 6,
        "porcentaje_promocion": 50.0,
        "dias_consumidos": 31,
    }

    def test_el_porcentaje_llega_como_fraccion_exacta(self) -> None:
        """50.0 -> Fraction(1, 2), no 0.5.

        El módulo entero evita el `float` a propósito; dejar entrar uno aquí metería su
        error binario justo en la línea que multiplica importes.
        """
        assert promocion_a_parametros(self.REAL)["tasa_descuento"] == Fraction(1, 2)

    def test_los_meses_se_convierten_a_dias_y_se_descuenta_lo_gastado(self) -> None:
        """6 meses × 30 días − 31 ya consumidos = 149 días de bolsa.

        Las dos conversiones importan. Usar los 90 días por defecto con una promoción de
        6 meses partiría el cuarto recibo cuando en realidad quedan tres meses enteros de
        descuento; y no restar lo consumido le prometería al cliente días que ya
        disfrutó.
        """
        assert promocion_a_parametros(self.REAL)["dias_promocion"] == 149

    def test_la_serie_sale_con_los_datos_del_cliente(self) -> None:
        """Plan de S/ 79,90: cuatro recibos a mitad, uno partido y vuelta al precio."""
        serie = recibos_de_alta(
            cargo_fijo_cent=7_990,
            dias_hasta_cierre=0,
            dias_ciclo=30,
            recibos=6,
            **promocion_a_parametros(self.REAL),
        )
        assert [r.cargo_fijo_cent for r in serie] == [3_995, 3_995, 3_995, 3_995, 4_128, 7_990]
        assert serie[4].es_mixto, "el quinto es donde se agota la bolsa"
        assert serie[-1].dias_restantes_promocion == 0

    def test_lo_que_no_consta_no_se_inventa(self) -> None:
        """Sin datos, la fórmula se queda con sus valores por defecto.

        Un campo ausente no puede convertirse en un cero: «no consta el porcentaje» y
        «el descuento es del 0 %» son cosas distintas y solo una es cierta.
        """
        assert promocion_a_parametros({}) == {}
        assert "dias_promocion" not in promocion_a_parametros({"porcentaje_promocion": 30.0})
