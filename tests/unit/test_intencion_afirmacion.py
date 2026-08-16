"""Un «sí» pelado es aceptar lo que el asistente acaba de ofrecer.

Sale de un fallo real visto en WhatsApp: el asistente preguntaba «¿le gustaría que
revisemos juntos su recibo?», el cliente contestaba «Sí», y como «sí» no casaba ningún
patrón caía en FUERA_DE_DOMINIO. El resultado eran dos respuestas igual de malas: repetir
la misma oferta —el cliente diciendo que sí en bucle— o contestar que el tema le pillaba
fuera de base, después de haber sido el asistente quien preguntó.

Es el fallo más caro posible porque ocurre justo cuando el cliente ya aceptó.
"""

from __future__ import annotations

import pytest

from packages.facts_engine.intencion import Intencion, clasificar_intencion


@pytest.mark.parametrize(
    "frase",
    ["Sí", "si", "Si.", "sip", "claro", "ok", "Dale", "ya", "por favor", "Revisemos",
     "veamos", "adelante", "de acuerdo", "sí por favor", "obvio", "¡Sí!"],
)
def test_una_afirmacion_pide_la_explicacion(frase: str) -> None:
    """Aceptar la oferta lleva a explicar el recibo, no a fuera de dominio.

    Es seguro mapearlo así porque en este producto solo se ofrece una cosa: revisar el
    recibo. Un «sí» aquí no puede querer decir otra cosa.
    """
    resultado = clasificar_intencion(frase)
    assert resultado.intencion is Intencion.EXPLICAR_RECIBO
    assert resultado.explica_recibo is True


@pytest.mark.parametrize(
    ("frase", "esperada"),
    [
        # La afirmación es la frase ENTERA o no es afirmación: si el cliente añade algo,
        # ese algo manda. Si no, «sí, quiero dar de baja» se explicaría como un recibo.
        ("sí, quiero dar de baja el servicio", Intencion.REGULATORIA),
        ("sí, páseme con un asesor", Intencion.PEDIR_HUMANO),
        # Documentado en el módulo: «ya pagué/cancelé MI RECIBO» no es la intención
        # de pagar ni una baja, es alguien que ya pagó y viene a preguntar por el
        # importe. Se comprueba aquí para que la regla de afirmación no lo cambie.
        ("ya pagué mi recibo", Intencion.EXPLICAR_RECIBO),
        ("hola", Intencion.SALUDO),
    ],
)
def test_lo_que_acompana_al_si_manda(frase: str, esperada: Intencion) -> None:
    assert clasificar_intencion(frase).intencion is esperada
