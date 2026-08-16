"""Configuración común de la suite.

Dos garantías que toda la suite da por sentadas y que se fijan aquí:

1. **Nada sale a la red.** ``LLM_MODE=mock`` y ``VERIFICADOR_ESTRICTO=true`` se imponen
   para toda la sesión salvo que el propio test los cambie. Un test que dependa de
   Gemini debe pedirlo explícitamente con la marca ``gemini``, y se omite si no hay
   ``GEMINI_API_KEY``.
2. **El dataset es opcional.** ``data/`` está en ``.gitignore`` por la cláusula de
   confidencialidad de diez años de las bases, así que en un clon limpio no existe. Las
   pruebas que lo necesitan se **omiten con un motivo legible** en vez de fallar; las de
   unidad y de propiedad no lo necesitan y corren siempre.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    """Fija el entorno determinístico de la suite **antes de recolectar nada**.

    ``ENTORNO=dev`` se pone aquí y no en una fixture porque ``apps/api`` resuelve sus
    ajustes una sola vez, al importar la aplicación: si el primer test que la importa la
    construye con otro entorno, el router ``/dev`` queda fuera y todos los tests de
    contrato que piden un token se saltarían en silencio con un 404. Es la diferencia
    entre una suite verde y una suite que no probó nada.
    """
    os.environ.setdefault("LLM_MODE", "mock")
    os.environ.setdefault("VERIFICADOR_ESTRICTO", "true")
    os.environ.setdefault("ENTORNO", "dev")
    # El retriever cae solo a índice en memoria sin DATABASE_URL; se explicita para que
    # ninguna prueba toque una base de datos por accidente.
    os.environ.pop("DATABASE_URL", None)
    # Y estas dos, que llegan del `.env` desde que `apps.api.settings` lo vuelca al
    # entorno del proceso. Se fijan **vacías** en vez de borrarse: al existir la clave,
    # `load_dotenv(override=False)` ya no la repone, así que da igual quién importe qué
    # primero. Sin esto, un `.env` con credenciales de Supabase convertía la suite en una
    # prueba de integración contra la nube —lenta, en red, y contra el dataset real— sin
    # que nadie lo hubiera pedido, y `ORIGEN_RECIBOS=supabase` haría que las pruebas del
    # dataset sintético buscaran cuentas C-DEMO que allí no existen.
    # El E2E real de notas es opt-in: cuando se pide explícitamente conserva el DSN del
    # `.env` y ejercita Supabase a través de HTTP. El resto de la suite mantiene la
    # garantía histórica de cero red accidental.
    if os.environ.get("RUN_SUPABASE_E2E") != "1":
        os.environ["SUPABASE_DB_URL"] = ""
        os.environ["ORIGEN_RECIBOS"] = ""
    # Y el embedder, que es el agujero que quedaba en la garantía nº 1. `LLM_MODE=mock`
    # calla al generador, pero **no** al modelo de embeddings: el índice vectorial seguía
    # llamando a Gemini durante la suite. Eso no era solo lentitud —de 52 s a más de dos
    # minutos— sino un resultado que dependía de una cuota ajena: agotado el cupo diario,
    # el retriever degrada a BM25 puro, devuelve otra evidencia y
    # `test_rehidratacion` empieza a contar 23 items donde esperaba 25. Un test que pasa
    # por la mañana y falla por la tarde sin que nadie toque el código no está probando
    # el código.
    #
    # `setdefault`, no asignación: quien exporte la variable a mano está pidiendo
    # explícitamente ejercitar el embedder de verdad, y eso se respeta.
    #   GEMINI_EMBED_MODEL=gemini-embedding-2 python -m pytest tests/integracion
    os.environ.setdefault("GEMINI_EMBED_MODEL", "")
    # Los *checkpoints* del grafo van a un fichero temporal propio de la sesión, no al
    # `data/checkpoints/turnos.sqlite` del proyecto. Sigue siendo SQLite **en disco**
    # —la suite tiene que ejercitar el almacén de verdad, no uno en memoria—, pero cada
    # ejecución arranca limpia y no acumula el estado de la anterior en el almacén con
    # el que se hace la demostración.
    os.environ.setdefault("CHECKPOINT_PATH", str(_checkpoints_de_la_sesion()))
    # Y la bitácora, por la misma razón y una más. La razón compartida: la suite no debe
    # escribir en `data/auditoria/eventos.jsonl`, que es la evidencia con la que se hace
    # la demostración. La razón propia: esa cadena de hashes es de **un solo escritor**,
    # y dos ejecuciones simultáneas —la suite y un servidor levantado al lado— se pisan
    # los índices y la rompen. Un test que compruebe `cadena_valida` fallaría entonces
    # por lo que hace otro proceso, no por lo que prueba.
    os.environ.setdefault("AUDIT_LOG_PATH", str(_bitacora_de_la_sesion()))


def _checkpoints_de_la_sesion() -> Path:
    """Fichero de *checkpoints* de esta ejecución de la suite, recién vaciado."""
    destino = Path(tempfile.gettempdir()) / "recibo-claro-tests" / "checkpoints.sqlite"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.unlink(missing_ok=True)
    return destino


def _bitacora_de_la_sesion() -> Path:
    """Fichero de auditoría de esta ejecución de la suite, recién vaciado.

    El nombre lleva el PID porque dos ejecuciones de la suite a la vez —algo habitual
    cuando varias personas trabajan sobre el mismo repositorio— compartirían el fichero
    y romperían la cadena de hashes de las dos.
    """
    destino = Path(tempfile.gettempdir()) / "recibo-claro-tests" / f"auditoria-{os.getpid()}.jsonl"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.unlink(missing_ok=True)
    return destino


@pytest.fixture(scope="session")
def raiz_proyecto() -> Path:
    """Raíz del repositorio."""
    return RAIZ


@pytest.fixture(scope="session")
def reglas():
    """Configuración de negocio cargada una sola vez para toda la sesión."""
    from packages.core_domain.reglas import cargar_reglas

    return cargar_reglas()


@pytest.fixture(scope="session")
def ruta_dataset() -> Path:
    """Directorio del dataset sintético (puede no existir)."""
    from eval.datos import ruta_dataset as resolver

    return resolver()


@pytest.fixture(scope="session")
def dataset_disponible(ruta_dataset: Path) -> bool:
    """``True`` si el dataset sintético está generado."""
    return (ruta_dataset / "bills").is_dir() and (ruta_dataset / "ground_truth.csv").is_file()


@pytest.fixture(scope="session")
def exige_dataset(dataset_disponible: bool) -> None:
    """Omite la prueba con un motivo accionable si falta el dataset."""
    if not dataset_disponible:
        pytest.skip(
            "falta el dataset sintético: ejecute "
            "`python -m packages.datagen.generar --seed 20260804 --clientes 300`"
        )


@pytest.fixture(scope="session")
def casos_golden(exige_dataset: None):
    """Los casos golden de ``eval/golden`` ya validados."""
    from eval.datos import cargar_golden

    return cargar_golden()
