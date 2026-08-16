"""Una diferencia entre notas no se presenta como el valor de la nota actual."""

from packages.core_domain.enums import AccionSiguiente, CausaOficial, TipoMovimiento
from packages.core_domain.esquemas.factset import LineaDelta
from packages.core_domain.reglas import cargar_reglas
from packages.facts_engine.motor import agregar_causas
from packages.llm_layer.generador import fijar_narrativa_de_notas
from packages.llm_layer.plantillas import construir_datos, renderizar_explicacion
from packages.llm_layer.providers.base import CausaExplicadaLLM, ExplicacionLLM


def test_credito_menor_explica_actual_previo_y_diferencia() -> None:
    datos = _datos_credito_menor()

    explicacion = renderizar_explicacion(datos)
    texto = " ".join([explicacion.resumen, *explicacion.frases()])

    assert "S/ 9.00 de abono" in texto
    assert "S/ 21.80" in texto
    assert "S/ 12.80 menos de abono" in texto
    assert "le descuenta S/ 12.80" not in texto
    assert explicacion.causas[0].monto_cent_citado == 1280


def test_corrige_semantica_erronea_de_un_llm_aunque_la_cifra_este_anclada() -> None:
    erronea = ExplicacionLLM(
        resumen="Su recibo subió por una nota de crédito.",
        causas=[
            CausaExplicadaLLM(
                concepto_id="NOTA_CREDITO",
                frase="La nota de crédito actual le descuenta S/ 12.80.",
                monto_cent_citado=1280,
            )
        ],
        siguiente_paso=AccionSiguiente.VER_DETALLE,
        cifras_usadas=[1280],
    )

    corregida = fijar_narrativa_de_notas(erronea, _datos_credito_menor())
    texto = " ".join([corregida.resumen, *corregida.frases()])

    assert "nota de crédito actual le descuenta S/ 12.80" not in texto
    assert "S/ 9.00 de abono" in texto
    assert "S/ 21.80" in texto
    assert "S/ 12.80 menos de abono" in texto


def test_credito_y_debito_con_variacion_positiva_conservan_su_tipo() -> None:
    lineas = [
        LineaDelta(
            concepto_id="NOTA_CREDITO",
            nombre_comercial="Nota de crédito",
            clase=LineaDelta.clasificar(-900, -2180),
            monto_actual_cent=-900,
            monto_previo_cent=-2180,
            delta_cent=1280,
            causa=TipoMovimiento.NOTA_CREDITO,
            causa_oficial=CausaOficial.NOTAS_CREDITO_DEBITO,
            confianza=1,
        ),
        LineaDelta(
            concepto_id="NOTA_DEBITO",
            nombre_comercial="Nota de débito",
            clase=LineaDelta.clasificar(700, 200),
            monto_actual_cent=700,
            monto_previo_cent=200,
            delta_cent=500,
            causa=TipoMovimiento.NOTA_DEBITO,
            causa_oficial=CausaOficial.NOTAS_CREDITO_DEBITO,
            confianza=1,
        ),
    ]

    causas = agregar_causas(lineas, cargar_reglas())
    etiquetas = {causa.causa: causa.etiqueta_cliente for causa in causas}

    assert etiquetas[TipoMovimiento.NOTA_CREDITO] == "nota de crédito (menor abono)"
    assert etiquetas[TipoMovimiento.NOTA_DEBITO] == "nota de débito (mayor cargo)"


def _datos_credito_menor():
    return construir_datos(
        {
            "periodo_actual": "2026-07",
            "periodo_previo": "2026-06",
            "modalidad_renta": "ADELANTADA",
            "dias_ciclo": 30,
            "total_actual_cent": 3547,
            "total_previo_cent": 2267,
            "delta_total_cent": 1280,
            "deuda_anterior_cent": 0,
            "lineas": [
                {
                    "concepto_id": "NOTA_CREDITO",
                    "nombre_comercial": "Nota de crédito",
                    "clase": "SUBIO",
                    "delta_cent": 1280,
                    "monto_actual_cent": -900,
                    "monto_previo_cent": -2180,
                    "causa": "NOTA_CREDITO",
                    "causa_confirmada": True,
                    "exige_causa": True,
                }
            ],
            "causas_agregadas": [
                {
                    "causa": "NOTA_CREDITO",
                    "etiqueta_cliente": "nota de crédito",
                    "monto_cent": 1280,
                }
            ],
        }
    )
