"""La prosa para clientes muestra soles; los céntimos quedan en el contrato interno."""

from packages.core_domain.enums import AccionSiguiente
from packages.llm_layer.generador import formatear_centimos_en_prosa
from packages.llm_layer.providers.base import CausaExplicadaLLM, ExplicacionLLM


def test_convierte_centimos_del_llm_a_soles_sin_tocar_el_valor_estructurado() -> None:
    explicacion = ExplicacionLLM(
        resumen="Este mes tuvo un ajuste de 20 céntimos.",
        causas=[
            CausaExplicadaLLM(
                concepto_id="NOTA_CREDITO",
                frase="La nota de crédito fue de -900 céntimos; antes fue -2180 centimos.",
                monto_cent_citado=-900,
            )
        ],
        siguiente_paso=AccionSiguiente.VER_DETALLE,
        cifras_usadas=[-900],
    )

    resultado = formatear_centimos_en_prosa(explicacion)

    assert resultado.resumen == "Este mes tuvo un ajuste de S/ 0.20."
    assert resultado.causas[0].frase == (
        "La nota de crédito fue de -S/ 9.00; antes fue -S/ 21.80."
    )
    assert resultado.causas[0].monto_cent_citado == -900
    assert resultado.cifras_usadas == [-900]
