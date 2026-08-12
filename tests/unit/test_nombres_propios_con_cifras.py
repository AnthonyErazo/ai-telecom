"""Nombres comerciales que llevan una cifra dentro, y que no son afirmaciones.

El catálogo real del facturador bautiza productos con su precio incluido: la cuenta
100032914 del dataset del desafío factura un concepto llamado literalmente
``"RV Plan Mi Movistar S/55.9 VII"``. Citarlo es lo correcto —es el nombre que el cliente
tiene impreso en su recibo—, pero el extractor de aserciones leía ``S/55.9`` como un
importe afirmado sobre el recibo, no lo encontraba en ``ALLOWED`` y bloqueaba la
respuesta. Resultado: el asistente derivaba a un asesor un recibo conciliado al céntimo
por haber llamado a las cosas por su nombre.

La regla que lo resuelve es **posicional y no amplía ``ALLOWED``**: los caracteres que
caen dentro de una cita literal y completa de un nombre propio del FactSet forman parte
de ese nombre. Estas pruebas fijan las dos mitades del trato, y la segunda importa más
que la primera: lo que se arregla no puede convertirse en una vía para colar cifras.
"""

from __future__ import annotations

import pytest

from packages.core_domain.enums import (
    FamiliaConcepto,
    ModalidadRenta,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.factset import FactSet, Invariante, LineaDelta
from packages.llm_layer.verificador import construir_permitidos, verificar

#: El nombre real, tal cual llega de ``v_concepto_real`` en Supabase.
NOMBRE_CON_PRECIO = "RV Plan Mi Movistar S/55.9 VII"


@pytest.fixture
def factset() -> FactSet:
    """Un recibo con un concepto cuyo nombre comercial contiene un importe."""
    linea = LineaDelta(
        concepto_id="RC_PLANRE508",
        nombre_comercial=NOMBRE_CON_PRECIO,
        clase=LineaDelta.clasificar(4_315, 5_000),
        monto_actual_cent=4_315,
        monto_previo_cent=5_000,
        delta_cent=-685,
        confianza=0.30,
        familia=FamiliaConcepto.RECURRENTE,
        evidencia=["cat:RC_PLANRE508", "linea:1"],
    )
    return FactSet(
        factset_id="99999999-8888-7777-6666-555555555555",
        cuenta_id="C-PRUEBA",
        modalidad_renta=ModalidadRenta.VENCIDA,
        periodo_actual="2026-05",
        periodo_previo="2026-04",
        dias_ciclo=30,
        total_actual_cent=4_315,
        total_previo_cent=5_000,
        delta_total_cent=-685,
        lineas=[linea],
        invariante=Invariante(
            ok=True, residual_cent=0, suma_deltas_cent=-685, delta_total_cent=-685
        ),
        confianza_global=0.30,
        rules_version="prueba",
    )


def test_el_nombre_completo_del_producto_no_bloquea(factset: FactSet) -> None:
    """Citar el nombre entero del concepto pasa la verificación.

    Es el caso que hacía derivar la cuenta 100032914 con verbosidad DETALLE, donde la
    plantilla lista las líneas por su nombre.
    """
    resultado = verificar(
        f"Lo que se movió en su recibo: {NOMBRE_CON_PRECIO} le baja S/ 6.85.",
        factset,
        permitidos=construir_permitidos(factset),
    )
    assert resultado.veredicto is VeredictoVerificacion.PASS
    assert resultado.no_ancladas == 0


def test_la_cifra_amparada_se_cita_con_su_hecho(factset: FactSet) -> None:
    """No basta con no bloquear: la cifra queda citada con el hecho que la ampara.

    Una cifra que pasa sin fuente sería un agujero en la auditoría, que es justamente lo
    que este proyecto promete que no existe.
    """
    resultado = verificar(
        f"El concepto {NOMBRE_CON_PRECIO} cambió.",
        factset,
        permitidos=construir_permitidos(factset),
    )
    fuentes = {cita.fact_id for cita in resultado.citas}
    assert "texto:linea:RC_PLANRE508.nombre_comercial" in fuentes


@pytest.mark.parametrize(
    ("texto", "motivo"),
    [
        ("Su plan cuesta S/55.9 al mes.", "la cifra suelta no está amparada por nada"),
        ("Su Plan Mi Movistar S/55.9 cambió.", "el nombre citado a medias no ampara"),
        ("Le cobraron S/55.9 de más este mes.", "afirmación monetaria sin respaldo"),
    ],
)
def test_la_misma_cifra_fuera_del_nombre_sigue_bloqueando(
    factset: FactSet, texto: str, motivo: str
) -> None:
    """La protección es posicional, no un permiso para el token.

    ``S/55.9`` no entra en ``ALLOWED``: solo deja de ser una afirmación cuando aparece
    dentro de la cita literal y completa del nombre. En cualquier otro sitio del texto
    sigue siendo una cifra inventada y bloquea la respuesta, que es exactamente lo que
    debe pasar.
    """
    resultado = verificar(texto, factset, permitidos=construir_permitidos(factset))
    assert resultado.veredicto is VeredictoVerificacion.FAIL, motivo
    assert "S/55.9" in " ".join(resultado.infractores)


def test_el_conjunto_permitido_no_crece(factset: FactSet) -> None:
    """La prueba de que no se ha relajado el conjunto: el token no está en ``ALLOWED``.

    Si este test empezara a fallar, alguien habría convertido la regla posicional en un
    permiso global y la tesis del proyecto —cada cifra anclada en el FactSet— perdería
    su garantía más fuerte.
    """
    permitidos = construir_permitidos(factset)
    assert "cent:5590" not in permitidos
