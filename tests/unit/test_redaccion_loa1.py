"""La redacción de ``LOA1`` frente a un bloque que no conoce.

El fallo que originó estas pruebas
----------------------------------
``redactar_para_nivel`` enumeraba los bloques que había que **quitar** (``kv``, ``puente``,
``tabla``) y trataba «todo lo demás» como párrafo. Cuando se añadió ``BloqueCiclos`` —una
línea de tiempo hecha de importes, fechas y porcentajes— el bucle le pidió un ``.texto``
que ese bloque no tiene, y ``POST /v1/explicar`` empezó a responder **500 para todo el
canal WhatsApp**. Ninguna prueba lo vio: la de contrato pedía ``200`` y se saltaba sola
cuando no llegaba, y el bloque nuevo solo aparece en el camino con importes.

Lo que se fija aquí es la política, no el caso: en una función cuyo trabajo es *quitar*
datos sensibles, lo que no se reconoce **se descarta**. Perder un bloque es un defecto
visible y reparable; colar sus cifras en un nivel que no puede verlas, no.
"""

from __future__ import annotations

import pytest

from packages.core_domain.enums import NivelAseguramiento
from packages.core_domain.esquemas.respuesta import (
    BloqueAviso,
    BloqueCiclos,
    BloqueKV,
    BloquePuente,
    BloqueTabla,
    BloqueTexto,
    CausaVisual,
    CicloExplicado,
    Derivacion,
    Gobernanza,
    HitoCiclo,
    ItemKV,
    RespuestaCanalAgnostica,
)

pytest.importorskip("fastapi", reason="apps/api no disponible sin fastapi")

from apps.api.security import redactar_para_nivel  # noqa: E402


def _respuesta(*bloques) -> RespuestaCanalAgnostica:
    return RespuestaCanalAgnostica(
        conversation_id="11111111-2222-3333-4444-555555555555",
        trace_id="tr-redaccion",
        bloques=list(bloques),
        acciones=[],
        derivacion=Derivacion(),
        gobernanza=Gobernanza(
            anclado=True,
            verificacion_numerica="PASS",
            aserciones_totales=0,
            aserciones_ancladas=0,
            aserciones_no_ancladas=0,
            confianza=1.0,
            modo="PLANTILLA",
            rules_version="redaccion-test",
            model_version="mock",
            factset_sha256="",
        ),
    )


_CICLOS = BloqueCiclos(
    titulo="Así cambió entre dos ciclos",
    modalidad="ADELANTADA",
    ciclos=[
        CicloExplicado(periodo="2026-06", total_cent=9_990, inicio="2026-06-01"),
        CicloExplicado(periodo="2026-07", total_cent=12_072, actual=True, inicio="2026-07-01"),
    ],
    hitos=[
        HitoCiclo(
            fecha="2026-07-12",
            etiqueta="Cambio de plan",
            tipo="prorrateo",
            periodo="2026-07",
        )
    ],
    causas=[CausaVisual(etiqueta="Fin de descuento", monto_cent=5_308, participacion_bp=3_780)],
)


def test_el_bloque_de_ciclos_no_tumba_la_respuesta() -> None:
    """Era un 500 en todo el canal WhatsApp. Ahora el bloque simplemente no viaja."""
    redactada = redactar_para_nivel(
        _respuesta(BloqueTexto(texto="Su recibo subió."), _CICLOS),
        NivelAseguramiento.LOA1,
    )
    assert [bloque.tipo for bloque in redactada.bloques] == ["aviso", "texto"]


@pytest.mark.parametrize(
    "estructurado",
    [
        BloqueKV(items=[ItemKV(clave="Total", valor="S/ 120.72", monto_cent=12_072)]),
        BloquePuente(barras=[]),
        BloqueTabla(columnas=["Concepto", "Monto"], filas=[["Renta", "S/ 79.90"]]),
        _CICLOS,
    ],
    ids=["kv", "puente", "tabla", "ciclos"],
)
def test_ningun_bloque_estructurado_sobrevive_a_loa1(estructurado) -> None:
    """Los cuatro son importes por construcción: en ``LOA1`` no hay versión reducida."""
    redactada = redactar_para_nivel(_respuesta(estructurado), NivelAseguramiento.LOA1)
    assert all(bloque.tipo in {"texto", "aviso"} for bloque in redactada.bloques)


def test_la_redaccion_no_deja_ni_un_digito() -> None:
    """La garantía del nivel, con el bloque que la rompía presente en la entrada."""
    redactada = redactar_para_nivel(
        _respuesta(
            BloqueTexto(texto="Su recibo subió S/ 20.82 respecto de junio."),
            BloqueAviso(severidad="advertencia", texto="Vence el 2026-07-25."),
            _CICLOS,
        ),
        NivelAseguramiento.LOA1,
    )
    assert not any(caracter.isdigit() for caracter in redactada.texto), redactada.texto


@pytest.mark.parametrize("nivel", [NivelAseguramiento.LOA2, NivelAseguramiento.LOA_ASESOR])
def test_los_niveles_con_montos_reciben_la_respuesta_intacta(nivel) -> None:
    """La redacción es una puerta de nivel, no un filtro que se aplique a todos."""
    original = _respuesta(BloqueTexto(texto="Su recibo subió S/ 20.82."), _CICLOS)
    assert redactar_para_nivel(original, nivel) is original
