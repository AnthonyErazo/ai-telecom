"""Regla innegociable nº 1: **no hay coma flotante en la aritmética del dinero**.

La especificación pide literalmente *"un test hace grep de ``float(`` en
``packages/facts_engine/`` y falla la build"*. Eso está aquí, y además se refuerza con
un análisis de AST, porque un grep de texto se engaña solo: no distingue una anotación
de tipo de una llamada, ni ve un ``Decimal`` monetario escrito de otra forma.

Qué se permite y por qué:

* ``float`` como **anotación** de confianzas, puntajes y umbrales (``0.98``, ``0.65``,
  ``tau_alto``). No son dinero: son probabilidades y pesos, y ahí la coma flotante es el
  tipo correcto.
* ``Decimal`` y ``Fraction`` como **tipo de entrada de una tasa de interés**
  (``cronograma_frances(..., tasa=Decimal("0.02"))``). La tasa no es un importe; el
  módulo la convierte a ``Fraction`` exacta y todo lo que sale de ahí es ``int``.

Qué NO se permite, y hace fallar la build:

* Cualquier llamada a ``float(...)`` o a ``Decimal(...)`` dentro de ``facts_engine``.
* Cualquier identificador monetario (sufijo ``_cent``) anotado como ``float``.
* Cualquier literal de coma flotante operando con un identificador monetario.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PAQUETE = Path(__file__).resolve().parents[2] / "packages" / "facts_engine"

#: Sufijo que marca un identificador monetario en todo el proyecto.
SUFIJO_MONETARIO = "_cent"

#: Constructores prohibidos dentro del paquete del motor.
CONSTRUCTORES_PROHIBIDOS = frozenset({"float", "Decimal"})

#: Grep literal de la especificación (sección 11), aplicado sobre código sin comentarios.
PATRON_GREP = re.compile(r"\bfloat\s*\(")

#: Excepciones autorizadas, con su justificación. Debe estar **vacío**: existe para que
#: cualquier excepción futura tenga que escribirse aquí, con nombre y motivo, y no se
#: cuele en una línea suelta de un módulo.
EXCEPCIONES_AUTORIZADAS: dict[str, str] = {}


def modulos() -> list[Path]:
    """Todos los módulos del motor determinístico."""
    return sorted(PAQUETE.glob("*.py"))


def test_el_paquete_existe_y_tiene_modulos() -> None:
    """Guardia del guardián: si el paquete se mueve, este test no puede pasar en vacío."""
    encontrados = modulos()
    assert PAQUETE.is_dir(), f"no existe {PAQUETE}"
    assert len(encontrados) >= 7, f"se esperaban al menos 7 módulos, hay {len(encontrados)}"


def _sin_comentarios(codigo: str) -> list[tuple[int, str]]:
    """Líneas de código con su número, descartando comentarios de almohadilla."""
    salida: list[tuple[int, str]] = []
    for numero, linea in enumerate(codigo.splitlines(), start=1):
        sin_comentario = linea.split("#", 1)[0]
        if sin_comentario.strip():
            salida.append((numero, sin_comentario))
    return salida


def _cadenas_del_modulo(arbol: ast.AST) -> set[int]:
    """Números de línea ocupados por literales de texto (docstrings incluidos)."""
    lineas: set[int] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            inicio = nodo.lineno
            fin = nodo.end_lineno or inicio
            lineas.update(range(inicio, fin + 1))
    return lineas


# --------------------------------------------------------------------------- #
# El grep literal de la especificación
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("modulo", modulos(), ids=lambda ruta: ruta.name)
def test_no_hay_llamadas_a_float(modulo: Path) -> None:
    """Grep de ``float(`` sobre el código real, ignorando comentarios y docstrings."""
    codigo = modulo.read_text(encoding="utf-8")
    lineas_de_texto = _cadenas_del_modulo(ast.parse(codigo, filename=str(modulo)))

    infractoras = [
        f"{modulo.name}:{numero}: {linea.strip()}"
        for numero, linea in _sin_comentarios(codigo)
        if PATRON_GREP.search(linea) and numero not in lineas_de_texto
    ]
    assert not infractoras, "coma flotante en la lógica monetaria del motor:\n  " + "\n  ".join(
        infractoras
    )


# --------------------------------------------------------------------------- #
# Refuerzo por AST
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("modulo", modulos(), ids=lambda ruta: ruta.name)
def test_no_se_construye_ningun_float_ni_decimal(modulo: Path) -> None:
    """Ninguna llamada a ``float(...)`` ni a ``Decimal(...)`` en el motor.

    ``Decimal`` puede **aparecer** como tipo aceptado en la firma de la tasa de interés,
    pero el motor no lo construye: convierte a ``Fraction`` y devuelve ``int``.
    """
    arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
    infractoras: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        objetivo = nodo.func
        nombre = (
            objetivo.id
            if isinstance(objetivo, ast.Name)
            else objetivo.attr
            if isinstance(objetivo, ast.Attribute)
            else ""
        )
        if nombre in CONSTRUCTORES_PROHIBIDOS:
            clave = f"{modulo.name}:{nodo.lineno}:{nombre}"
            if clave not in EXCEPCIONES_AUTORIZADAS:
                infractoras.append(f"{clave} — llamada a {nombre}()")

    assert not infractoras, "construcción de tipos no enteros en el motor:\n  " + "\n  ".join(
        infractoras
    )


@pytest.mark.parametrize("modulo", modulos(), ids=lambda ruta: ruta.name)
def test_ningun_identificador_monetario_esta_anotado_como_float(modulo: Path) -> None:
    """Todo lo que termina en ``_cent`` es ``int``, en argumentos, retornos y campos."""
    arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
    infractoras: list[str] = []

    def es_float(anotacion: ast.expr | None) -> bool:
        """``True`` si la anotación menciona ``float`` en cualquier posición."""
        if anotacion is None:
            return False
        return any(
            isinstance(nodo, ast.Name) and nodo.id == "float" for nodo in ast.walk(anotacion)
        )

    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.AnnAssign) and isinstance(nodo.target, ast.Name):
            if nodo.target.id.endswith(SUFIJO_MONETARIO) and es_float(nodo.annotation):
                infractoras.append(f"{modulo.name}:{nodo.lineno}: {nodo.target.id}: float")
        elif isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
            argumentos = [
                *nodo.args.posonlyargs,
                *nodo.args.args,
                *nodo.args.kwonlyargs,
            ]
            for argumento in argumentos:
                if argumento.arg.endswith(SUFIJO_MONETARIO) and es_float(argumento.annotation):
                    infractoras.append(
                        f"{modulo.name}:{nodo.lineno}: {nodo.name}({argumento.arg}: float)"
                    )
            if nodo.name.endswith(SUFIJO_MONETARIO) and es_float(nodo.returns):
                infractoras.append(f"{modulo.name}:{nodo.lineno}: {nodo.name}() -> float")

    assert not infractoras, "identificadores monetarios anotados como float:\n  " + "\n  ".join(
        infractoras
    )


@pytest.mark.parametrize("modulo", modulos(), ids=lambda ruta: ruta.name)
def test_ningun_literal_flotante_opera_con_un_importe(modulo: Path) -> None:
    """``monto_cent * 0.18`` es exactamente el error que este test existe para atrapar."""
    arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
    infractoras: list[str] = []

    def es_monetario(nodo: ast.expr) -> bool:
        """``True`` si la expresión nombra un identificador con sufijo monetario."""
        for hijo in ast.walk(nodo):
            if isinstance(hijo, ast.Name) and hijo.id.endswith(SUFIJO_MONETARIO):
                return True
            if isinstance(hijo, ast.Attribute) and hijo.attr.endswith(SUFIJO_MONETARIO):
                return True
        return False

    def es_literal_flotante(nodo: ast.expr) -> bool:
        """``True`` si es un literal de coma flotante (``0.18``, ``1e3``)."""
        return isinstance(nodo, ast.Constant) and isinstance(nodo.value, float)

    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.BinOp):
            continue
        lados = (nodo.left, nodo.right)
        if any(es_literal_flotante(lado) for lado in lados) and any(
            es_monetario(lado) for lado in lados
        ):
            infractoras.append(f"{modulo.name}:{nodo.lineno}: literal float sobre un importe")

    assert not infractoras, "aritmética de coma flotante sobre importes:\n  " + "\n  ".join(
        infractoras
    )


def test_las_excepciones_estan_vacias() -> None:
    """Si alguien añade una excepción, que sea una decisión visible y no un descuido."""
    assert EXCEPCIONES_AUTORIZADAS == {}, (
        "hay excepciones autorizadas a la regla del céntimo entero: "
        f"{EXCEPCIONES_AUTORIZADAS}. Revíselas antes de dar la build por buena."
    )


def test_el_detector_reconoce_una_violacion_real(tmp_path: Path) -> None:
    """Prueba del propio detector: sin esto, un test que nunca falla no prueba nada."""
    modulo = tmp_path / "modulo_con_float.py"
    modulo.write_text(
        "def calcular(monto_cent: int) -> int:\n"
        "    total_cent = float(monto_cent) * 0.18\n"
        "    return int(total_cent)\n",
        encoding="utf-8",
    )
    codigo = modulo.read_text(encoding="utf-8")

    assert any(PATRON_GREP.search(linea) for _numero, linea in _sin_comentarios(codigo))
    arbol = ast.parse(codigo)
    llamadas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id in CONSTRUCTORES_PROHIBIDOS
    ]
    assert llamadas, "el detector de AST debería ver la llamada a float()"
