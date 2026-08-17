"""El enlace a la App Mi Movistar: cuándo se manda y cuándo estorba.

Por qué esta prueba existe
--------------------------
La respuesta de WhatsApp le dice al cliente *«ingrese a la App Mi Movistar»* en dos
sitios distintos: el aviso fijo de ``LOA1`` y los turnos conversacionales de ``PAGAR`` y
``CONSUMO``. En un canal de texto, esa instrucción sin un enlace es un callejón sin
salida: el cliente no sabe qué app es ni de dónde bajarla.

Lo que se fija aquí son los tres límites del comportamiento:

1. **Solo en WhatsApp.** En la App el enlace sobra (el cliente ya está dentro) y en la
   consola del asesor sería ruido.
2. **Solo si el texto habla de la App.** Un enlace de descarga pegado a cualquier
   respuesta es publicidad, no ayuda.
3. **Sin un solo dígito.** El enlace entra en textos de ``LOA1``, cuya garantía es que no
   contienen ninguno. De ahí que se omita el ``&pli=1`` de la URL que copia el navegador.
"""

from __future__ import annotations

import pytest

from packages.core_domain.enums import Canal

pytest.importorskip("fastapi", reason="apps/api no disponible sin fastapi")

from apps.api.routers.explicar import enlazar_app_si_procede  # noqa: E402
from apps.api.security import AVISO_LOA1, URL_APP_MI_MOVISTAR  # noqa: E402


# --------------------------------------------------------------------------- #
# La URL en sí
# --------------------------------------------------------------------------- #
def test_la_ficha_apunta_al_paquete_de_la_app_mi_movistar() -> None:
    """``tdp.app.col`` es el paquete de la App Mi Movistar en Google Play."""
    assert URL_APP_MI_MOVISTAR.startswith("https://play.google.com/store/apps/details?")
    assert "id=tdp.app.col" in URL_APP_MI_MOVISTAR
    # Español de Perú: el cliente es B2C peruano y la ficha se le abre en su idioma.
    assert "hl=es_PE" in URL_APP_MI_MOVISTAR


def test_la_ficha_no_lleva_ni_un_digito() -> None:
    """La condición que permite meterla en un texto ``LOA1``.

    Si alguien vuelve a pegar la URL completa del navegador —con ``&pli=1``— esta prueba
    se pone roja **antes** de que lo haga ``scripts/probar_e2e.py`` paso 13, y con un
    mensaje que dice exactamente qué sobra.
    """
    digitos = [caracter for caracter in URL_APP_MI_MOVISTAR if caracter.isdigit()]
    assert digitos == [], (
        f"la URL lleva {digitos}: en LOA1 no puede viajar ningún dígito. "
        "El parámetro &pli=1 es una pista de sesión de Google Play y no cambia el destino: "
        "se omite a propósito."
    )


def test_el_aviso_de_loa1_lleva_la_ficha() -> None:
    """El aviso es la única salida que ``LOA1`` ofrece: tiene que ser accionable."""
    assert URL_APP_MI_MOVISTAR in AVISO_LOA1
    assert not any(caracter.isdigit() for caracter in AVISO_LOA1)


# --------------------------------------------------------------------------- #
# Los turnos conversacionales
# --------------------------------------------------------------------------- #
_PIDE_LA_APP = (
    "Puede pagarlo desde la App Mi Movistar, con el código de pago que aparece en su recibo."
)


def test_en_whatsapp_se_adjunta_la_ficha() -> None:
    resultado = enlazar_app_si_procede(_PIDE_LA_APP, Canal.WHATSAPP)
    assert URL_APP_MI_MOVISTAR in resultado
    assert resultado.startswith(_PIDE_LA_APP), "el enlace se añade, no reescribe la respuesta"


@pytest.mark.parametrize("canal", [Canal.APP, Canal.BOT, Canal.ASESOR])
def test_fuera_de_whatsapp_el_texto_no_se_toca(canal: Canal) -> None:
    """Decirle «descargue la App» a alguien que está dentro de la App es absurdo."""
    assert enlazar_app_si_procede(_PIDE_LA_APP, canal) == _PIDE_LA_APP


def test_sin_mencion_a_la_app_no_se_adjunta_nada() -> None:
    """El enlace responde a lo que la respuesta ofrece, no a la ocasión de anunciarlo."""
    texto = "Entiendo. Eso lo tiene que ver un asesor y ya lo estoy derivando."
    assert enlazar_app_si_procede(texto, Canal.WHATSAPP) == texto


def test_no_se_duplica_si_el_texto_ya_lo_trae() -> None:
    """El aviso de ``LOA1`` ya lo lleva; volver a pegarlo daría dos enlaces seguidos."""
    ya_enlazado = f"{_PIDE_LA_APP} Aquí la encuentra: {URL_APP_MI_MOVISTAR}"
    assert enlazar_app_si_procede(ya_enlazado, Canal.WHATSAPP) == ya_enlazado


def test_el_enlace_no_introduce_digitos_en_el_turno() -> None:
    """La misma garantía que en ``LOA1``, comprobada sobre el resultado final."""
    resultado = enlazar_app_si_procede(_PIDE_LA_APP, Canal.WHATSAPP)
    assert not any(caracter.isdigit() for caracter in resultado)


@pytest.mark.parametrize(
    "texto",
    [
        "El consumo lo tiene en la app.",
        "Eso lo encuentra en la aplicación de Movistar.",
        "Descárguelo desde la APP Mi Movistar.",
    ],
)
def test_reconoce_la_mencion_escriba_el_modelo_como_escriba(texto: str) -> None:
    """El texto lo redacta el modelo dentro del guion: la frase exacta no se conoce."""
    assert URL_APP_MI_MOVISTAR in enlazar_app_si_procede(texto, Canal.WHATSAPP)
