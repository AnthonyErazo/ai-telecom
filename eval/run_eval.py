"""``make eval`` — ejecuta la suite golden e imprime la tabla de métricas oficiales.

Uso::

    python -m eval.run_eval                      # tabla en terminal, modo LLM_MODE
    python -m eval.run_eval --modo mock          # forzar el proveedor determinístico
    python -m eval.run_eval --markdown           # tabla lista para el documento ejecutivo
    python -m eval.run_eval --json informe.json  # volcado completo, caso por caso
    python -m eval.run_eval --casos G01,G32      # subconjunto, para depurar

Códigos de salida: ``0`` métricas cumplidas · ``1`` alguna métrica incumplida ·
``2`` dataset o casos golden ausentes · ``3`` error de configuración.

La **advertencia de circularidad** se imprime siempre, arriba y abajo, y también viaja
dentro del JSON. No es adorno: sin ella, un lector externo interpretaría un 100 % como
una promesa de desempeño en producción, y estas cifras no dicen eso.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from eval import __version__
from eval.datos import DatasetAusente, cargar_golden
from eval.metricas import (
    ADVERTENCIA_CIRCULARIDAD,
    CAMPOS_HANDOFF,
    InformeEvaluacion,
    ObservacionCaso,
    agregar,
    ejecutar_suite,
)
from packages.core_domain.reglas import cargar_reglas

__all__ = ["construir_argumentos", "main", "tabla_markdown", "tabla_terminal"]

ANCHO = 78
_VERDE = "\033[32m"
_ROJO = "\033[31m"
_AMARILLO = "\033[33m"
_NEGRITA = "\033[1m"
_FIN = "\033[0m"


def _color_activo(flujo: TextIO) -> bool:
    """Respeta ``NO_COLOR`` y desactiva el color si la salida no es una terminal."""
    if os.getenv("NO_COLOR") is not None:
        return False
    return bool(getattr(flujo, "isatty", lambda: False)())


def _pintar(texto: str, color: str, *, activo: bool) -> str:
    """Envuelve el texto en un código ANSI solo si el color está activo."""
    return f"{color}{texto}{_FIN}" if activo else texto


def _pct(valor: float | None) -> str:
    """Formatea una proporción como porcentaje con dos decimales."""
    return "n/d" if valor is None else f"{valor * 100:.2f} %"


# --------------------------------------------------------------------------- #
# Tabla de terminal
# --------------------------------------------------------------------------- #
def _marco(titulo: str) -> list[str]:
    """Cabecera de sección enmarcada."""
    return [f"┌{'─' * (ANCHO - 2)}┐", f"│ {titulo.ljust(ANCHO - 4)} │", f"└{'─' * (ANCHO - 2)}┘"]


def _fila(etiqueta: str, valor: str, nota: str = "") -> str:
    """Fila de dos columnas con una nota opcional a la derecha."""
    cuerpo = f"  {etiqueta.ljust(42)} {valor.rjust(12)}"
    return f"{cuerpo}   {nota}" if nota else cuerpo


def tabla_terminal(
    informe: InformeEvaluacion, *, color: bool = False, detalle: bool = False
) -> str:
    """Construye la tabla legible que se pega en el documento ejecutivo.

    Estructura fija: advertencia de circularidad, las tres métricas oficiales en el
    orden de la ficha, las métricas de apoyo y el veredicto. El detalle por caso solo
    aparece con ``--detalle`` o cuando algo falla, para que la salida normal quepa en
    una pantalla.
    """
    recuperacion = informe.recuperacion
    alucinacion = informe.alucinacion
    handoff = informe.handoff
    apoyo = informe.apoyo
    lineas: list[str] = []

    lineas.append("╔" + "═" * (ANCHO - 2) + "╗")
    lineas.append(
        "║" + "  RECIBO CLARO · EVALUACIÓN DE LAS MÉTRICAS OFICIALES".ljust(ANCHO - 2) + "║"
    )
    lineas.append(
        "║"
        + f"  Desafío 1 · Hackathon AI Telecom 2026 · protocolo v{__version__}".ljust(ANCHO - 2)
        + "║"
    )
    lineas.append("╚" + "═" * (ANCHO - 2) + "╝")
    lineas.append("")
    lineas.append(_pintar(_bloque_advertencia(), _AMARILLO, activo=color))
    lineas.append("")
    lineas.append(
        f"  casos golden: {len(informe.observaciones)}   ·   proveedor: {informe.modo}   ·   "
        f"reglas: {informe.rules_version}   ·   {informe.duracion_ms} ms"
    )
    lineas.append("")

    # -- 1. Precisión de Recuperación ------------------------------------- #
    lineas.extend(_marco("1. PRECISIÓN DE RECUPERACIÓN (Retrieval Accuracy)"))
    lineas.append(
        _fila(
            "(C) strict answer accuracy  ← TITULAR",
            _pct(recuperacion.strict),
            f"{recuperacion.casos_exactos}/{recuperacion.casos_totales} respuestas exactas",
        )
    )
    lineas.append(
        _fila(
            "(A) field-level exact match · micro",
            _pct(recuperacion.ra_field_micro),
            f"{recuperacion.campos_correctos}/{recuperacion.campos_totales} campos",
        )
    )
    lineas.append(
        _fila(
            "(A) field-level exact match · macro",
            _pct(recuperacion.ra_field_macro),
            "promedio por escenario",
        )
    )
    lineas.append(
        _fila(
            "(B) Recall@1 doc-level (concepto_id)",
            _pct(recuperacion.recall_at_1),
            f"{recuperacion.recall_at_1_evaluados} casos evaluables",
        )
    )
    lineas.append("")
    lineas.append("  desglose macro por escenario (céntimos enteros, coincidencia exacta):")
    for escenario, valor in recuperacion.ra_field_por_escenario.items():
        casos = recuperacion.casos_por_escenario[escenario]
        lineas.append(f"    {escenario.ljust(30)} {_pct(valor).rjust(9)}   ({casos} casos)")
    lineas.append("")

    # -- 2. Tasa de Alucinación ------------------------------------------- #
    lineas.extend(_marco("2. TASA DE ALUCINACIÓN (cero invenciones financieras)"))
    estado_ta = _pintar(
        "CUMPLE" if alucinacion.compromiso_cumplido else "INCUMPLE",
        _VERDE if alucinacion.compromiso_cumplido else _ROJO,
        activo=color,
    )
    lineas.append(
        _fila(
            "TA_respuesta  ← COMPROMETIDA EN 0",
            _pct(alucinacion.ta_respuesta),
            f"{estado_ta} · {alucinacion.respuestas_con_alucinacion}/"
            f"{alucinacion.respuestas_totales} respuestas",
        )
    )
    lineas.append(
        _fila(
            "TA_asercion",
            _pct(alucinacion.ta_asercion),
            f"{alucinacion.aserciones_no_ancladas}/{alucinacion.aserciones_totales} cifras",
        )
    )
    lineas.append(
        _fila(
            "afirmaciones numéricas auditadas",
            str(alucinacion.aserciones_totales),
            "todas ancladas o derivadas del FactSet",
        )
    )
    lineas.append(
        _fila(
            "fragmentos prohibidos en el texto",
            str(alucinacion.respuestas_con_fragmento_prohibido),
            "casos adversariales de inyección",
        )
    )
    veredictos = " · ".join(f"{clave} {valor}" for clave, valor in alucinacion.veredictos.items())
    lineas.append(f"  veredictos del verificador: {veredictos}")
    for caso, fragmento in alucinacion.fragmentos_detectados:
        lineas.append(_pintar(f"    ! {caso}: apareció «{fragmento}»", _ROJO, activo=color))
    lineas.append("")

    # -- 3. Precisión del Hand-off ---------------------------------------- #
    lineas.extend(_marco("3. PRECISIÓN DEL HAND-OFF (umbrales de incomprensión)"))
    lineas.append(
        _fila(
            "Recall_handoff  ← PRIMARIA",
            _pct(handoff.recall),
            f"{handoff.verdaderos_positivos} de "
            f"{handoff.verdaderos_positivos + handoff.falsos_negativos} derivaciones debidas "
            "(el FN es el daño grave)",
        )
    )
    lineas.append(_fila("Precision_handoff", _pct(handoff.precision), ""))
    lineas.append(_fila("F2 (recall pesa el doble)", _pct(handoff.f2), ""))
    lineas.append(
        _fila(
            "Tasa de atrapamiento (FP / FP+VN)",
            _pct(handoff.tasa_atrapamiento),
            "conversaciones sanas escaladas de más",
        )
    )
    mediana = handoff.mediana_turnos_hasta_derivar
    lineas.append(
        _fila(
            "Mediana de turnos hasta derivar",
            "n/d" if mediana is None else f"{mediana:.1f}",
            f"turnos observados: {list(handoff.turnos_observados) or 'ninguno'}",
        )
    )
    lineas.append(
        _fila(
            "Handoff_completeness (7 campos)",
            _pct(handoff.completitud),
            f"{handoff.campos_presentes}/{handoff.campos_esperados} campos informados",
        )
    )
    lineas.append(
        f"  matriz  VP {handoff.verdaderos_positivos} · FP {handoff.falsos_positivos} · "
        f"VN {handoff.verdaderos_negativos} · FN {handoff.falsos_negativos}"
        f"   (exactitud {_pct(handoff.exactitud)})"
    )
    lineas.append(f"  payload del hand-off: {', '.join(CAMPOS_HANDOFF)}")
    for caso in handoff.casos_no_derivados_que_debian:
        lineas.append(_pintar(f"    ! FALSO NEGATIVO: {caso} no derivó", _ROJO, activo=color))
    for caso in handoff.casos_derivados_de_mas:
        lineas.append(
            _pintar(f"    · falso positivo: {caso} derivó de más", _AMARILLO, activo=color)
        )
    lineas.append("")

    # -- Métricas de apoyo ------------------------------------------------ #
    lineas.extend(_marco("MÉTRICAS DE APOYO"))
    lineas.append(
        _fila(
            "residual_medio_cent",
            f"{apoyo.residual_medio_cent:.2f}",
            f"máximo {apoyo.residual_maximo_cent} c · tolerancia ±1 c",
        )
    )
    lineas.append(
        _fila(
            "precision_causa_raiz",
            _pct(apoyo.precision_causa_raiz),
            f"{apoyo.causas_acertadas}/{apoyo.causas_evaluadas} conceptos atribuidos",
        )
    )
    lineas.append(
        _fila(
            "tasa_fallback (a plantilla)",
            _pct(apoyo.tasa_fallback),
            " · ".join(f"{clave} {valor}" for clave, valor in apoyo.modos.items()),
        )
    )
    lineas.append(
        _fila(
            "latencia por caso (mediana / p95)",
            f"{apoyo.latencia_mediana_ms:.0f} / {apoyo.latencia_p95_ms:.0f} ms",
            "pipeline completo, sin red",
        )
    )
    lineas.append("")

    # -- Detalle ---------------------------------------------------------- #
    if detalle or recuperacion.campos_fallidos:
        lineas.extend(_marco("DETALLE POR CASO"))
        lineas.append(
            "  caso                              RA_field  R@1  cifras  no ancl  deriva  res"
        )
        for observacion in informe.observaciones:
            lineas.append(_linea_detalle(observacion))
        lineas.append("")
    if recuperacion.campos_fallidos:
        lineas.append("  campos que no coincidieron (esperado → obtenido, en céntimos):")
        for caso, campo, esperado, obtenido in recuperacion.campos_fallidos:
            lineas.append(
                _pintar(f"    ! {caso} · {campo}: {esperado} → {obtenido}", _ROJO, activo=color)
            )
        lineas.append("")

    # -- Veredicto -------------------------------------------------------- #
    aprobado = informe.aprobado
    etiqueta = "APROBADA" if aprobado else "NO APROBADA"
    lineas.append(
        _pintar(
            f"{_NEGRITA if color else ''}  EVALUACIÓN {etiqueta}",
            _VERDE if aprobado else _ROJO,
            activo=color,
        )
    )
    lineas.append(
        "  criterio: TA_respuesta = 0 · sin fragmentos prohibidos · sin falsos negativos "
        "de hand-off\n            · invariante exacto en todos los casos · strict answer "
        "accuracy = 100 %\n            · precision_causa_raiz ≥ "
        f"{InformeEvaluacion.UMBRAL_CAUSA_RAIZ:.0%} — la aritmética exacta no basta si la "
        "causa que se cuenta es falsa"
    )
    lineas.append("")
    lineas.append(_pintar(_bloque_advertencia(compacto=True), _AMARILLO, activo=color))
    return "\n".join(lineas)


def _linea_detalle(observacion: ObservacionCaso) -> str:
    """Una línea compacta por caso para la tabla de detalle."""
    recall = {True: " ok", False: " NO", None: "  -"}[observacion.recall_at_1]
    deriva = ("sí" if observacion.derivo else "no") + (
        "" if observacion.derivo == observacion.debe_derivar else " !"
    )
    return (
        f"  {observacion.caso_id[:32].ljust(32)} "
        f"{observacion.ra_field * 100:6.1f}%  {recall}  "
        f"{observacion.aserciones_totales:6d}  {observacion.aserciones_no_ancladas:7d}  "
        f"{deriva.ljust(6)}  {observacion.residual_cent:3d}"
    )


def _bloque_advertencia(*, compacto: bool = False) -> str:
    """Advertencia de circularidad enmarcada; la versión compacta va al pie."""
    if compacto:
        return (
            "  ┌ RECORDATORIO ────────────────────────────────────────────────────────┐\n"
            "  │ Ground truth y sistema comparten autor. Estas cifras validan la      │\n"
            "  │ MECÁNICA DEL MOTOR; no predicen el desempeño sobre datos reales de   │\n"
            "  │ Movistar. Ver la advertencia completa al inicio de la salida.        │\n"
            "  └──────────────────────────────────────────────────────────────────────┘"
        )
    borde = "  " + "!" * (ANCHO - 4)
    cuerpo = "\n".join(f"  {linea}" for linea in ADVERTENCIA_CIRCULARIDAD.splitlines())
    return f"{borde}\n{cuerpo}\n{borde}"


# --------------------------------------------------------------------------- #
# Tabla Markdown
# --------------------------------------------------------------------------- #
def tabla_markdown(informe: InformeEvaluacion) -> str:
    """Las mismas cifras en Markdown, para pegar en el documento ejecutivo (PDF)."""
    recuperacion = informe.recuperacion
    alucinacion = informe.alucinacion
    handoff = informe.handoff
    apoyo = informe.apoyo
    filas = [
        (
            "**Precisión de Recuperación — strict answer accuracy (titular)**",
            _pct(recuperacion.strict),
            f"{recuperacion.casos_exactos}/{recuperacion.casos_totales} respuestas con todos "
            "los campos exactos al céntimo",
        ),
        (
            "Precisión de Recuperación — field-level micro",
            _pct(recuperacion.ra_field_micro),
            f"{recuperacion.campos_correctos}/{recuperacion.campos_totales} campos",
        ),
        (
            "Precisión de Recuperación — field-level macro por escenario",
            _pct(recuperacion.ra_field_macro),
            f"{len(recuperacion.ra_field_por_escenario)} escenarios",
        ),
        (
            "Precisión de Recuperación — Recall@1 doc-level",
            _pct(recuperacion.recall_at_1),
            f"{recuperacion.recall_at_1_evaluados} casos evaluables",
        ),
        (
            "**Tasa de Alucinación — TA_respuesta (comprometida = 0)**",
            _pct(alucinacion.ta_respuesta),
            f"{alucinacion.respuestas_con_alucinacion}/{alucinacion.respuestas_totales} respuestas",
        ),
        (
            "Tasa de Alucinación — TA_asercion",
            _pct(alucinacion.ta_asercion),
            f"{alucinacion.aserciones_no_ancladas}/{alucinacion.aserciones_totales} cifras "
            "auditadas una a una",
        ),
        (
            "**Precisión del Hand-off — Recall (primaria)**",
            _pct(handoff.recall),
            f"VP {handoff.verdaderos_positivos} · FN {handoff.falsos_negativos}",
        ),
        (
            "Precisión del Hand-off — Precision",
            _pct(handoff.precision),
            f"FP {handoff.falsos_positivos} · VN {handoff.verdaderos_negativos}",
        ),
        ("Precisión del Hand-off — F2", _pct(handoff.f2), "β = 2: el falso negativo pesa el doble"),
        (
            "Precisión del Hand-off — tasa de atrapamiento",
            _pct(handoff.tasa_atrapamiento),
            "conversaciones sanas escaladas de más",
        ),
        (
            "Precisión del Hand-off — mediana de turnos hasta derivar",
            "n/d"
            if handoff.mediana_turnos_hasta_derivar is None
            else f"{handoff.mediana_turnos_hasta_derivar:.1f}",
            "cliente que repite la misma consulta",
        ),
        (
            "Precisión del Hand-off — completitud del payload",
            _pct(handoff.completitud),
            f"{len(CAMPOS_HANDOFF)} campos de contexto por derivación",
        ),
        (
            "residual_medio_cent",
            f"{apoyo.residual_medio_cent:.2f}",
            f"máximo {apoyo.residual_maximo_cent} céntimos",
        ),
        (
            "precision_causa_raiz",
            _pct(apoyo.precision_causa_raiz),
            f"{apoyo.causas_acertadas}/{apoyo.causas_evaluadas} conceptos",
        ),
        (
            "tasa_fallback",
            _pct(apoyo.tasa_fallback),
            "respuestas resueltas con la plantilla determinística",
        ),
    ]
    salida = [
        f"### Métricas oficiales — {len(informe.observaciones)} casos golden "
        f"(proveedor `{informe.modo}`, reglas `{informe.rules_version}`)",
        "",
        "| Métrica | Valor | Detalle |",
        "|---|---:|---|",
    ]
    salida.extend(f"| {nombre} | {valor} | {nota} |" for nombre, valor, nota in filas)
    salida.append("")
    salida.append(
        "> **Advertencia de circularidad.** "
        + " ".join(linea.strip() for linea in ADVERTENCIA_CIRCULARIDAD.splitlines()[1:])
    )
    return "\n".join(salida)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def construir_argumentos() -> argparse.ArgumentParser:
    """Analizador de argumentos de ``python -m eval.run_eval``."""
    analizador = argparse.ArgumentParser(
        prog="eval.run_eval",
        description="Ejecuta la suite golden y publica las tres métricas oficiales.",
    )
    analizador.add_argument("--modo", default=None, help="mock | gemini (por defecto, LLM_MODE)")
    analizador.add_argument("--golden", default=None, help="directorio de casos golden")
    analizador.add_argument("--dataset", default=None, help="directorio del dataset sintético")
    analizador.add_argument(
        "--casos", default=None, help="lista de caso_id separados por coma (subconjunto)"
    )
    analizador.add_argument(
        "--sin-retriever",
        action="store_true",
        help="omite el RAG: la capa (B) queda sin medir. Solo para depurar el motor.",
    )
    analizador.add_argument("--detalle", action="store_true", help="imprime la tabla por caso")
    analizador.add_argument("--markdown", action="store_true", help="salida en Markdown")
    analizador.add_argument(
        "--json", dest="json_salida", default=None, help="escribe el informe completo en un JSON"
    )
    analizador.add_argument(
        "--silencioso", action="store_true", help="no imprime el progreso caso a caso"
    )
    return analizador


def _progreso(indice: int, total: int, observacion: ObservacionCaso) -> None:
    """Traza de progreso: una línea por caso, útil cuando el proveedor es remoto."""
    marca = "ok" if observacion.exacta and not observacion.alucinada else "!!"
    print(
        f"  [{indice:>3}/{total}] {marca} {observacion.caso_id.ljust(34)} "
        f"{observacion.aserciones_totales:>3} cifras · "
        f"{observacion.aserciones_no_ancladas} sin anclar · {observacion.latencia_ms} ms",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = construir_argumentos().parse_args(argv)

    try:
        solo = [
            identificador.strip()
            for identificador in (argumentos.casos or "").split(",")
            if identificador.strip()
        ]
        casos = cargar_golden(argumentos.golden, solo=solo or None)
        reglas = cargar_reglas()
    except DatasetAusente as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print(f"ERROR DE CONFIGURACIÓN: {error}", file=sys.stderr)
        return 3

    if not casos:
        print("ERROR: la selección de casos quedó vacía", file=sys.stderr)
        return 2

    modo = argumentos.modo or os.getenv("LLM_MODE", "mock")
    arranque = time.perf_counter()
    try:
        observaciones = ejecutar_suite(
            casos,
            modo=argumentos.modo,
            reglas=reglas,
            usar_retriever=not argumentos.sin_retriever,
            ruta_dataset=argumentos.dataset,
            al_terminar_caso=None if argumentos.silencioso else _progreso,
        )
    except DatasetAusente as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    informe = agregar(
        observaciones,
        modo=modo,
        rules_version=reglas.rules_version,
        duracion_ms=int((time.perf_counter() - arranque) * 1000),
        parametros={
            "golden": str(argumentos.golden or "eval/golden"),
            "dataset": str(argumentos.dataset or "data/sintetico"),
            "retriever": not argumentos.sin_retriever,
            "protocolo": __version__,
        },
    )

    if argumentos.markdown:
        print(tabla_markdown(informe))
    else:
        print(tabla_terminal(informe, color=_color_activo(sys.stdout), detalle=argumentos.detalle))

    if argumentos.json_salida:
        destino = Path(argumentos.json_salida)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(
            json.dumps(informe.a_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\ninforme JSON escrito en {destino}", file=sys.stderr)

    return 0 if informe.aprobado else 1


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    raise SystemExit(main())
