"""El paquete del asesor se construye **desde la bitácora**, y su brief se verifica.

Estas pruebas fabrican una bitácora encadenada de verdad —``RegistroAuditoria`` sobre un
fichero temporal— en vez de simular los eventos, porque lo que se está probando es
justamente que el paquete no dependa de ningún estado paralelo: si la única entrada es
un JSONL sellado por hash, no hay forma de que se cuele un dato que nadie auditó.

Lo que se comprueba, en orden de importancia:

1. Que cada cifra del brief está anclada (la tesis del proyecto, aplicada al texto que
   lee el asesor y no solo al que lee el cliente).
2. Que una cifra inventada en el brief **se detecta**: la prueba adversaria, sin la cual
   el punto 1 solo demuestra que el verificador dice que sí.
3. Que lo que no se pudo confirmar aparece nombrado y no se pierde.
4. Que las cifras que escribió el cliente no se cuentan como afirmaciones del sistema.
5. Que el paquete cruza turnos: la explicación de un turno y la derivación del siguiente
   son el mismo caso.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.core_domain.enums import (
    EtapaAuditoria,
    MotivoDerivacion,
    NivelAseguramiento,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.paquete_asesor import MotivoIncertidumbre
from packages.governance.auditoria import RegistroAuditoria, verificar_cadena
from packages.governance.paquete_asesor import (
    construir_paquete_asesor,
    redactar_brief,
    tokens_del_paquete,
    traza_de_context_ref,
    verificar_brief,
)

CUENTA = "C-PRUEBA-77"
CONVERSACION = "6f7d1e5c-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
# Bitácora de laboratorio
# --------------------------------------------------------------------------- #
def _linea(
    concepto: str,
    nombre: str,
    previo: int,
    actual: int,
    *,
    causa: str | None = "CAMBIO_PLAN",
    confianza: float = 0.95,
) -> dict[str, object]:
    """Una línea de delta con la forma exacta que escribe ``payload_facts_built``."""
    return {
        "concepto_id": concepto,
        "nombre_comercial": nombre,
        "clase": "SUBIO" if actual > previo else "BAJO",
        "monto_previo_cent": previo,
        "monto_actual_cent": actual,
        "delta_cent": actual - previo,
        "causa": causa,
        "causa_oficial": None,
        "confianza": confianza,
        "atribuida": causa is not None,
    }


def _escribir_turno_explicacion(
    registro: RegistroAuditoria,
    trace_id: str,
    *,
    con_linea_sin_causa: bool = False,
    utterance: str = "por que subio mi recibo",
) -> None:
    """Escribe en la bitácora un turno de explicación completo y verificado."""
    contexto = {"cuenta_ref": CUENTA, "nivel": NivelAseguramiento.LOA2, "actor": CUENTA}
    registro.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "POST /v1/explicar",
            "periodo": "2026-07",
            "canal": "APP",
            "nivel": "LOA2",
            "verbosidad": "NORMAL",
            "utterance": utterance,
            "conversation_id": CONVERSACION,
        },
        **contexto,
    )
    lineas = [_linea("RENTA_PLAN", "Renta del plan", 10000, 13000)]
    if con_linea_sin_causa:
        lineas.append(
            _linea("CARGO_RARO", "Cargo no identificado", 0, 500, causa=None, confianza=0.30)
        )
    delta = sum(int(linea["delta_cent"]) for linea in lineas)  # type: ignore[arg-type]
    registro.emitir(
        EtapaAuditoria.FACTS_BUILT,
        trace_id,
        {
            "factset_sha256": "a" * 64,
            "delta_total_cent": delta,
            "total_previo_cent": 10000,
            "total_actual_cent": 10000 + delta,
            "deuda_anterior_cent": 0,
            "total_a_pagar_cent": 10000 + delta,
            "periodo_actual": "2026-07",
            "periodo_previo": "2026-06",
            "fecha_vencimiento": "2026-08-13",
            "modalidad_renta": "ADELANTADA",
            "residual_cent": 0,
            "invariante_ok": True,
            "confianza_global": 0.9,
            "lineas": len(lineas),
            "lineas_delta": lineas,
            "causas_detalle": [
                {
                    "etiqueta_cliente": "cambio de plan",
                    "causa": "CAMBIO_PLAN",
                    "monto_cent": 3000,
                    "participacion_bp": 10000 if not con_linea_sin_causa else 8571,
                    "confianza": 0.95,
                    "movimientos": [77],
                }
            ],
        },
        **contexto,
    )
    registro.emitir(
        EtapaAuditoria.INVARIANTE,
        trace_id,
        {"ok": True, "residual_cent": 0, "suma_deltas_cent": delta, "delta_total_cent": delta},
        **contexto,
    )
    registro.emitir(
        EtapaAuditoria.VERIFY,
        trace_id,
        {
            "veredicto": "PASS",
            "aserciones_totales": 2,
            "aserciones_ancladas": 2,
            "aserciones_derivadas": 0,
            "aserciones_no_ancladas": 0,
            "aserciones": [
                {
                    "texto_original": "S/ 30.00",
                    "token_normalizado": "cent:3000",
                    "estado": "ANCLADA",
                    "fuente": "linea:RENTA_PLAN.delta_cent",
                },
                {
                    "texto_original": "S/ 130.00",
                    "token_normalizado": "cent:13000",
                    "estado": "ANCLADA",
                    "fuente": "factset:total_actual_cent",
                },
            ],
        },
        **contexto,
    )
    registro.emitir(
        EtapaAuditoria.CITATIONS,
        trace_id,
        {"citas": [], "fact_ids": ["linea:RENTA_PLAN.delta_cent", "factset:total_actual_cent"]},
        **contexto,
    )
    registro.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {
            "bloques": 3,
            "acciones": 2,
            "modo": "LLM",
            "derivada": False,
            "texto_entregado": "Su recibo subió S/ 30.00 por el cambio de plan.",
        },
        **contexto,
    )
    registro.cerrar_turno(trace_id, cuenta_ref=CUENTA)


def _escribir_turno_derivacion(registro: RegistroAuditoria, trace_id: str, ref: str) -> None:
    """Escribe el turno en el que el cliente pide una persona."""
    contexto = {"cuenta_ref": CUENTA, "nivel": NivelAseguramiento.LOA2, "actor": CUENTA}
    registro.emitir(
        EtapaAuditoria.REQUEST,
        trace_id,
        {
            "endpoint": "POST /v1/derivacion",
            "periodo": "2026-07",
            "canal": "WHATSAPP",
            "nivel": "LOA2",
            "verbosidad": "NO_APLICA",
            "utterance": "quiero hablar con una persona",
            "conversation_id": CONVERSACION,
        },
        **contexto,
    )
    registro.emitir(
        EtapaAuditoria.ROUTE,
        trace_id,
        {
            "derivar": True,
            "motivo_codigo": str(MotivoDerivacion.PETICION_HUMANO),
            "modo": "DERIVACION_EXPLICITA",
            "context_ref": ref,
            "senal_disparadora": "peticion_explicita:PETICION_HUMANO",
        },
        **contexto,
    )
    registro.emitir(
        EtapaAuditoria.RESPONSE,
        trace_id,
        {"bloques": 0, "acciones": 1, "modo": "DERIVACION", "derivada": True, "context_ref": ref},
        **contexto,
    )
    registro.cerrar_turno(trace_id, cuenta_ref=CUENTA)


@pytest.fixture
def bitacora(tmp_path: Path) -> RegistroAuditoria:
    """Bitácora encadenada de un caso completo: explicación y luego derivación."""
    registro = RegistroAuditoria(tmp_path / "eventos.jsonl", actor="prueba", sincronizar=False)
    _escribir_turno_explicacion(registro, "tr-explica0001")
    _escribir_turno_derivacion(registro, "tr-deriva00001", "ctx-prueba0001")
    return registro


def _paquete(registro: RegistroAuditoria, trace_id: str, ref: str | None = None):
    """Atajo: construye el paquete con la cadena ya verificada, como hace el endpoint."""
    valida, roto = verificar_cadena(registro.ruta)
    return construir_paquete_asesor(
        registro.leer(),
        trace_id=trace_id,
        context_ref=ref,
        cadena_valida=valida,
        indice_roto=roto,
    )


# --------------------------------------------------------------------------- #
# 1. El brief no inventa cifras
# --------------------------------------------------------------------------- #
def test_el_brief_del_paquete_no_tiene_ni_una_cifra_sin_anclar(bitacora) -> None:
    """La tesis del proyecto, aplicada al texto que lee el asesor."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")

    assert paquete.verificacion_brief is not None
    assert paquete.verificacion_brief.veredicto is VeredictoVerificacion.PASS, (
        f"cifras sin anclar en el brief: {paquete.verificacion_brief.no_ancladas}\n"
        f"{paquete.brief}"
    )
    assert paquete.verificacion_brief.no_ancladas == []
    assert paquete.apto_para_entregar


def test_una_cifra_inventada_en_el_brief_se_detecta(bitacora) -> None:
    """Prueba adversaria: sin esto, el PASS anterior solo probaría que el verificador dice que sí."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")
    permitidos = tokens_del_paquete(paquete)

    envenenado = paquete.brief + "\nEXTRA         se le devolverán S/ 87.31 el próximo mes"
    veredicto = verificar_brief(envenenado, permitidos)

    assert veredicto.veredicto is VeredictoVerificacion.FAIL
    assert "cent:8731" in veredicto.no_ancladas


def test_las_cifras_que_escribio_el_cliente_no_se_juzgan(bitacora) -> None:
    """El sistema no afirma lo que el cliente preguntó: lo cita, y así queda declarado."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")
    paquete.consulta_cliente = "me cobraron 999 soles de más"
    brief, cita = redactar_brief(paquete)

    veredicto = verificar_brief(brief, tokens_del_paquete(paquete), cita_cliente=cita)

    assert veredicto.veredicto is VeredictoVerificacion.PASS
    assert "num:999" in veredicto.citadas_del_cliente, (
        "la cifra del cliente debe quedar listada aparte, ni anclada ni bloqueada"
    )


# --------------------------------------------------------------------------- #
# 2. El contenido viene de la bitácora y no de otro sitio
# --------------------------------------------------------------------------- #
def test_el_paquete_trae_el_delta_y_las_lineas_que_lo_componen(bitacora) -> None:
    """El asesor recibe el desglose, no solo el total."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")

    assert paquete.delta_total_cent == 3000
    assert paquete.total_previo_cent == 10000
    assert paquete.total_actual_cent == 13000
    assert [linea.concepto_id for linea in paquete.lineas] == ["RENTA_PLAN"]
    assert paquete.lineas[0].delta_cent == 3000
    assert paquete.causas[0].etiqueta_cliente == "cambio de plan"


def test_el_paquete_cruza_los_turnos_del_mismo_caso(bitacora) -> None:
    """La explicación fue en un turno y la derivación en el siguiente: es el mismo caso.

    Es el fallo que este diseño evita: un paquete que mirase solo el turno de derivación
    diría que al cliente no se le explicó nada, y el asesor le repetiría lo ya dicho.
    """
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")

    assert paquete.ya_explicado.hubo_explicacion
    assert paquete.ya_explicado.texto == "Su recibo subió S/ 30.00 por el cambio de plan."
    assert paquete.ya_explicado.veredicto == "PASS"
    assert [cifra.token for cifra in paquete.ya_explicado.cifras] == ["cent:3000", "cent:13000"]
    assert paquete.motivo_codigo == str(MotivoDerivacion.PETICION_HUMANO)
    assert "la explicación del recibo ya se le dio" in paquete.accion_pendiente
    assert len(paquete.evidencia.trazas) == 2


def test_el_canal_sale_de_la_bitacora(bitacora) -> None:
    """El paquete es el mismo para los tres canales, pero sabe por cuál entró el caso."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")
    assert paquete.canal == "WHATSAPP"


def test_la_referencia_del_expediente_se_resuelve_en_la_bitacora(bitacora) -> None:
    """``context_ref`` → ``trace_id`` sin tocar la memoria del proceso."""
    assert traza_de_context_ref(bitacora.leer(), "ctx-prueba0001") == "tr-deriva00001"
    assert traza_de_context_ref(bitacora.leer(), "ctx-inexistente") is None


def test_consultar_el_paquete_no_envenena_la_busqueda_de_la_referencia(bitacora) -> None:
    """La bitácora sigue creciendo mientras el asesor trabaja, y eso no puede afectarle.

    Regresión de un fallo real: el propio acceso al expediente se auditaba nombrando la
    referencia, y la **segunda** consulta encontraba el evento de la primera en vez del
    turno que la acuñó. Devolvía un paquete vacío, con lo que el asesor perdía el caso
    justo al recargar la pantalla.
    """
    primero = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")

    bitacora.emitir(
        EtapaAuditoria.ROUTE,
        "tr-consulta001",
        {"etapa": "asesor", "evento": "PAQUETE_ENTREGADO", "context_ref": "ctx-prueba0001"},
        cuenta_ref=CUENTA,
        nivel=NivelAseguramiento.LOA_ASESOR,
        acting_on_behalf_of=CUENTA,
    )

    assert traza_de_context_ref(bitacora.leer(), "ctx-prueba0001") == "tr-deriva00001"
    segundo = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")
    assert segundo.delta_total_cent == primero.delta_total_cent
    assert segundo.ya_explicado.hubo_explicacion


def test_sin_eventos_no_hay_paquete(bitacora) -> None:
    """Un traspaso sin bitácora sería la derivación a ciegas que el proyecto elimina."""
    with pytest.raises(ValueError, match="no hay eventos auditados"):
        construir_paquete_asesor(bitacora.leer(), trace_id="tr-que-no-existe")


# --------------------------------------------------------------------------- #
# 3. Lo que NO se pudo confirmar
# --------------------------------------------------------------------------- #
def test_una_linea_sin_causa_llega_al_asesor_declarada_como_tal(tmp_path: Path) -> None:
    """El motor ve el cuánto y no el porqué: el asesor tiene que saberlo antes de hablar."""
    registro = RegistroAuditoria(tmp_path / "eventos.jsonl", actor="prueba", sincronizar=False)
    _escribir_turno_explicacion(registro, "tr-explica0002", con_linea_sin_causa=True)
    paquete = _paquete(registro, "tr-explica0002")

    codigos = {item.codigo for item in paquete.incertidumbres}
    assert MotivoIncertidumbre.LINEA_SIN_ATRIBUIR in codigos
    sin_atribuir = next(
        item
        for item in paquete.incertidumbres
        if item.codigo == MotivoIncertidumbre.LINEA_SIN_ATRIBUIR
    )
    assert sin_atribuir.impacto_cent == 500
    assert "NO CONFIRMADO" in paquete.brief
    assert paquete.verificacion_brief is not None
    assert paquete.verificacion_brief.veredicto is VeredictoVerificacion.PASS


def test_un_turno_sin_explicacion_lo_dice(tmp_path: Path) -> None:
    """Si el asesor empieza la conversación en vez de retomarla, el paquete lo declara."""
    registro = RegistroAuditoria(tmp_path / "eventos.jsonl", actor="prueba", sincronizar=False)
    _escribir_turno_derivacion(registro, "tr-solo-deriva", "ctx-solo0001")
    paquete = _paquete(registro, "tr-solo-deriva", "ctx-solo0001")

    codigos = {item.codigo for item in paquete.incertidumbres}
    assert MotivoIncertidumbre.SIN_EXPLICACION_ENTREGADA in codigos
    assert MotivoIncertidumbre.SIN_HECHOS in codigos
    assert not paquete.ya_explicado.hubo_explicacion


def test_la_cadena_rota_bloquea_la_entrega(bitacora) -> None:
    """Un paquete cuya evidencia no valida no puede usarse como evidencia."""
    paquete = construir_paquete_asesor(
        bitacora.leer(),
        trace_id="tr-deriva00001",
        context_ref="ctx-prueba0001",
        cadena_valida=False,
        indice_roto=4,
    )

    assert not paquete.apto_para_entregar
    assert MotivoIncertidumbre.CADENA_ROTA in {item.codigo for item in paquete.incertidumbres}
    assert "CADENA ROTA" in paquete.brief


# --------------------------------------------------------------------------- #
# 4. Forma del paquete para los transportes
# --------------------------------------------------------------------------- #
def test_el_texto_plano_lleva_el_brief_y_lo_no_confirmado(bitacora) -> None:
    """Es lo único que viaja por un canal de solo texto, y no puede perder lo importante."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")
    texto = paquete.a_texto_plano()

    assert paquete.brief in texto
    for item in paquete.incertidumbres:
        assert item.detalle in texto


def test_la_notificacion_no_lleva_importes(bitacora) -> None:
    """El aviso viaja por sistemas que no son nuestros: ahí no sale ni un sol."""
    paquete = _paquete(bitacora, "tr-deriva00001", "ctx-prueba0001")
    aviso = paquete.resumen_para_notificacion()

    assert "S/" not in str(aviso)
    assert aviso["context_ref"] == "ctx-prueba0001"
    assert aviso["trace_id"] == "tr-deriva00001"
