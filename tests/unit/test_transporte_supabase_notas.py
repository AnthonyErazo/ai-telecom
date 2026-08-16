"""Integración determinística de CRD/DSC en el contrato de recibos y movimientos."""

from __future__ import annotations

from datetime import date, datetime

from apps.api.acl import AdaptadorAmdocs, AdaptadorBrainyBill
from apps.api.transporte_supabase import TransporteSupabase, _agrupar_notas
from packages.core_domain.enums import TipoMovimiento
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.reglas import cargar_reglas
from packages.facts_engine.motor import construir_factset, movimientos_del_ciclo


def _fila_nota(
    identificador: int,
    tipo: str,
    monto: str,
    *,
    codigo: str = "FRIRPL_003",
    descripcion: str = "Movistar Internet",
) -> tuple[object, ...]:
    return (
        identificador,
        "20260531",
        tipo,
        codigo,
        monto,
        "2026-05-31 00:00:00.0000000",
        "2026-05-05 00:00:00.0000000",
        "2026-05-07 00:00:00.0000000",
        descripcion,
        "SERVICIO-1",
    )


def test_crd_y_dsc_se_agrupan_por_tipo_con_signo_y_evidencia(caplog) -> None:
    notas = _agrupar_notas(
        [
            _fila_nota(10, "CRD", "-7.20871"),
            _fila_nota(11, "CRD", "-1.127", codigo="RC_PLANRE594"),
            _fila_nota(12, "DSC", "0.656129", codigo="FRIRDE_209"),
            _fila_nota(13, "CRD", "1.00"),  # signo contradictorio: se rechaza
        ]
    )["20260531"]

    credito = next(nota for nota in notas if nota["concepto_id"] == "NOTA_CREDITO")
    debito = next(nota for nota in notas if nota["concepto_id"] == "NOTA_DEBITO")

    assert credito["monto_cent"] == -834
    assert credito["cantidad"] == 2
    assert credito["nota_ids"] == [10, 11]
    assert credito["movimiento_id"] == -10
    assert debito["monto_cent"] == 66
    assert debito["movimiento_id"] == -12
    assert "signo incompatible" in caplog.text


def test_las_notas_entran_en_el_total_y_en_lineas_canonicas() -> None:
    notas = _agrupar_notas([_fila_nota(10, "CRD", "-7.20871"), _fila_nota(12, "DSC", "0.656129")])[
        "20260531"
    ]
    transporte = object.__new__(TransporteSupabase)
    filas_cargo = [
        (
            "20260531",
            "RECIBO-1",
            31,
            "RC_PLANRE594",
            "Plan Porta S/39.9",
            "39.90",
            "CARGO FIJO",
            "CARGO FIJO MOVIL",
            "20260612",
            None,
        )
    ]

    bruto = transporte._recibo("CUENTA-1", "20260531", filas_cargo, "ADELANTADA", notas=notas)
    recibo = AdaptadorBrainyBill(transporte).a_recibo(
        bruto, {"moneda": "PEN", "segmento": "MASIVO"}
    )

    assert recibo.total_cent == 3990 - 721 + 66
    assert recibo.total_cent == sum(linea.monto_cent for linea in recibo.lineas)
    por_concepto = {linea.concepto_id: linea for linea in recibo.lineas}
    assert por_concepto["NOTA_CREDITO"].movimiento_id == -10
    assert por_concepto["NOTA_CREDITO"].meta["nota_ids"] == [10]
    assert por_concepto["NOTA_DEBITO"].movimiento_id == -12


class _Resultado:
    def __init__(self, filas: list[tuple[object, ...]]) -> None:
        self._filas = filas

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._filas


class _ConexionOrdenes:
    def execute(self, consulta: str, parametros: tuple[object, ...]) -> _Resultado:
        assert "FROM orden_servicio" in consulta
        assert parametros == ("CUENTA-1",)
        return _Resultado([])


def test_la_nota_tambien_se_proyecta_como_movimiento_causal(monkeypatch) -> None:
    notas = _agrupar_notas([_fila_nota(10, "CRD", "-7.20871")])
    transporte = object.__new__(TransporteSupabase)
    transporte._conexion = _ConexionOrdenes()
    monkeypatch.setattr(transporte, "_notas", lambda _cuenta: notas)

    crudo = transporte._ordenes("CUENTA-1")
    movimientos = AdaptadorAmdocs(transporte).a_movimientos(crudo, "CUENTA-1")

    assert len(movimientos) == 1
    movimiento = movimientos[0]
    assert movimiento.movimiento_id == -10
    assert movimiento.tipo is TipoMovimiento.NOTA_CREDITO
    assert movimiento.detalle_tipado().monto_cent == -721
    assert movimiento.canal == "FACTURACION"


def test_una_nota_efectiva_en_el_cierre_pertenece_a_ese_recibo() -> None:
    movimiento = MovementEvent(
        movimiento_id=-10,
        cuenta_id="CUENTA-1",
        tipo=TipoMovimiento.NOTA_CREDITO,
        ocurrido_en=datetime(2026, 5, 31),
        detalle={"documento": "supabase:notas:10", "monto_cent": -721, "motivo": "Ajuste"},
    )
    orden_ordinaria = MovementEvent(
        movimiento_id=20,
        cuenta_id="CUENTA-1",
        tipo=TipoMovimiento.CAMBIO_PLAN,
        ocurrido_en=datetime(2026, 5, 31),
    )

    ventana = movimientos_del_ciclo(
        [movimiento, orden_ordinaria], "CUENTA-1", date(2026, 5, 1), date(2026, 5, 31)
    )

    assert [evento.movimiento_id for evento in ventana] == [-10]


def test_el_vinculo_directo_a_la_nota_gana_sobre_otra_nota_en_la_ventana() -> None:
    transporte = object.__new__(TransporteSupabase)
    notas = _agrupar_notas([_fila_nota(10, "CRD", "-7.20871")])["20260531"]
    cargo_actual = [
        (
            "20260531",
            "RECIBO-ACTUAL",
            31,
            "RC_PLANRE594",
            "Plan Porta S/39.9",
            "39.90",
            "CARGO FIJO",
            "CARGO FIJO MOVIL",
            "20260612",
            None,
        )
    ]
    cargo_previo = [
        (
            "20260430",
            "RECIBO-PREVIO",
            30,
            "RC_PLANRE594",
            "Plan Porta S/39.9",
            "39.90",
            "CARGO FIJO",
            "CARGO FIJO MOVIL",
            "20260512",
            None,
        )
    ]
    adaptador = AdaptadorBrainyBill(transporte)
    actual = adaptador.a_recibo(
        transporte._recibo("CUENTA-1", "20260531", cargo_actual, "ADELANTADA", notas=notas)
    )
    previo = adaptador.a_recibo(
        transporte._recibo("CUENTA-1", "20260430", cargo_previo, "ADELANTADA")
    )
    detalle = {"documento": "nota", "monto_cent": -721, "motivo": "Ajuste"}
    movimientos = [
        MovementEvent(
            movimiento_id=-9,
            cuenta_id="CUENTA-1",
            tipo=TipoMovimiento.NOTA_CREDITO,
            ocurrido_en=datetime(2026, 5, 1),
            detalle=detalle,
        ),
        MovementEvent(
            movimiento_id=-10,
            cuenta_id="CUENTA-1",
            tipo=TipoMovimiento.NOTA_CREDITO,
            ocurrido_en=datetime(2026, 5, 31),
            detalle=detalle,
        ),
    ]

    factset = construir_factset(actual, [previo], movimientos, cargar_reglas())
    nota = next(linea for linea in factset.lineas if linea.concepto_id == "NOTA_CREDITO")

    assert nota.movimiento_id == -10
    assert nota.causa is TipoMovimiento.NOTA_CREDITO
    assert nota.confianza == 0.98
    assert factset.invariante.ok is True
