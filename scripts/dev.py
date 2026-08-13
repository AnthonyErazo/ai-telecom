#!/usr/bin/env python3
"""Arranque de desarrollo en un comando: dataset + API con recarga + navegador.

Es lo que ejecuta ``make dev``. En una laptop limpia, sin Docker, sin PostgreSQL y sin
red, hace por orden:

1. **Comprueba el dataset** de ``data/sintetico`` y lo genera si falta
   (determinístico: misma semilla, mismos bytes). Sin él, ``/v1/hechos`` daría 404.
2. **Fija los valores por defecto sin infraestructura**: ``ENTORNO=dev``,
   ``LLM_MODE=mock`` y ``MODO_ALMACENAMIENTO=memoria``, salvo que ya vengan del entorno
   (lo que usted exporte manda: esto no pisa una configuración existente).
3. **Busca un puerto libre** si el pedido está ocupado. En Windows, un puerto tomado da
   ``[WinError 10013] Intento de acceso a un socket no permitido``, que no se parece en
   nada a «ese puerto ya está en uso»; desplazarse al siguiente y decirlo ahorra la
   media hora de diagnóstico que ese mensaje suele costar.
4. **Abre el navegador** en ``/ui`` cuando la API responde, con reserva a ``/docs`` si
   esa interfaz todavía no está montada. En segundo plano, para no bloquear el arranque.
5. **Arranca uvicorn con recarga** en primer plano, de modo que ``Ctrl+C`` lo pare como
   si lo hubiera lanzado usted a mano.

Por qué un script de Python y no tres líneas de ``Makefile``: el entorno de trabajo es
Windows con Git Bash, y ``xdg-open``/``open``/``start`` no existen en todas partes.
Aquí se usa :mod:`webbrowser`, que resuelve el navegador en Windows, macOS y Linux, y
todas las rutas se calculan con :mod:`pathlib`.

Uso::

    python scripts/dev.py                     # 127.0.0.1:8000, recarga, abre /ui
    python scripts/dev.py --puerto 8010
    python scripts/dev.py --puerto 8010 --puerto-fijo   # falla si está ocupado
    python scripts/dev.py --sin-navegador     # servidores headless y CI
    python scripts/dev.py --sin-recarga       # un solo proceso, logs más limpios
    python scripts/dev.py --clientes 3        # dataset mínimo (los tres de guion)

Sin ``make`` instalado (Windows no lo trae), este script **es** ``make dev``:
``python scripts/dev.py`` hace exactamente lo mismo.

Códigos de salida::

    0   el servidor terminó de forma ordenada (Ctrl+C incluido)
    2   no se pudo preparar el dataset
    3   no hay puerto libre (o el pedido está ocupado y se exigió --puerto-fijo)
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

__all__ = ["dataset_disponible", "main", "preparar_dataset", "puerto_libre", "resolver_puerto"]

#: Salidas del proceso.
_OK = 0
_SIN_DATASET = 2
_SIN_PUERTO = 3

#: Cuántos puertos consecutivos se prueban antes de rendirse.
_PUERTOS_A_PROBAR = 20

#: Raíz del repositorio (``scripts/dev.py`` → un nivel arriba).
RAIZ = Path(__file__).resolve().parents[1]

#: Semilla y volumetría del dataset publicado. Coinciden con el Makefile y el README.
SEED_POR_DEFECTO = 20260804
CLIENTES_POR_DEFECTO = 300
PERIODO_POR_DEFECTO = "2026-07"

#: Rutas que se abren en el navegador, en orden de preferencia.
RUTAS_INTERFAZ = ("/ui", "/docs")

#: Configuración que hace que todo funcione sin infraestructura. No pisa el entorno.
ENTORNO_SIN_INFRAESTRUCTURA = {
    "ENTORNO": "dev",
    "LLM_MODE": "mock",
    # El interruptor del que depende todo esto: ni una conexión a PostgreSQL.
    # Ojo: solo se impone si NO hay una base configurada. Ver `_modo_almacenamiento`.
    "MODO_ALMACENAMIENTO": "memoria",
}


#: Claves cuyo valor, si existe, significa «este equipo sí tiene una base».
CLAVES_DE_BASE = ("DATABASE_URL", "SUPABASE_DB_URL")


def _valor_en_env(clave: str) -> str:
    """Busca una clave en el entorno y, si no está, en el ``.env`` del proyecto.

    Este guion es **solo biblioteca estándar** a propósito —tiene que correr en un equipo
    recién clonado, sin ``pip install`` de por medio— así que no usa ``python-dotenv``.
    El fichero se lee a mano y con el mínimo indispensable: no interpreta comillas,
    ``export`` ni sustituciones, porque para decidir «¿hay base configurada?» basta con
    saber si la clave tiene algo escrito.
    """
    del_entorno = (os.environ.get(clave) or "").strip()
    if del_entorno:
        return del_entorno
    fichero = RAIZ / ".env"
    if not fichero.is_file():
        return ""
    try:
        lineas = fichero.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for linea in lineas:
        limpia = linea.strip()
        if limpia.startswith("#") or "=" not in limpia:
            continue
        nombre, _, valor = limpia.partition("=")
        if nombre.strip() == clave:
            return valor.strip()
    return ""


def _hay_base_configurada() -> bool:
    """``True`` si el equipo tiene un PostgreSQL declarado al que merezca la pena ir."""
    return any(_valor_en_env(clave) for clave in CLAVES_DE_BASE)


def _avisar(texto: str = "") -> None:
    """Escribe una línea de progreso vaciando el búfer.

    Sin ``flush`` las líneas de este script aparecerían **después** de las de uvicorn,
    que escribe por su cuenta: quien arranca leería «el puerto 8000 está ocupado»
    cuando el servidor ya lleva rato en otro puerto.
    """
    print(texto, flush=True)


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #
def dataset_disponible(raiz_datos: Path) -> bool:
    """``True`` si el dataset sintético está completo para la demo.

    Se exigen las tres piezas que consume la API: los recibos, el catálogo de conceptos
    y el historial de órdenes. Un directorio a medias es peor que uno vacío, porque
    falla más tarde y en otro sitio.
    """
    bills = raiz_datos / "bills"
    if not bills.is_dir() or not any(bills.glob("C-DEMO-*.json")):
        return False
    return (raiz_datos / "catalogo.json").is_file() and (raiz_datos / "ordenes.csv").is_file()


def preparar_dataset(
    raiz_datos: Path,
    *,
    seed: int = SEED_POR_DEFECTO,
    clientes: int = CLIENTES_POR_DEFECTO,
    periodo: str = PERIODO_POR_DEFECTO,
) -> bool:
    """Genera el dataset si falta. Devuelve ``True`` si al terminar está disponible."""
    if dataset_disponible(raiz_datos):
        recibos = len(list((raiz_datos / "bills").glob("*.json")))
        _avisar(f"  dataset      ya está en {raiz_datos} ({recibos} cuentas)")
        return True

    _avisar(f"  dataset      no está en {raiz_datos}: generándolo (semilla {seed})…")
    orden = [
        sys.executable,
        "-m",
        "packages.datagen.generar",
        "--seed",
        str(seed),
        "--clientes",
        str(clientes),
        "--periodo-actual",
        periodo,
        "--salida",
        str(raiz_datos),
    ]
    resultado = subprocess.run(orden, cwd=RAIZ, check=False)
    if resultado.returncode != 0 or not dataset_disponible(raiz_datos):
        print(
            f"\n  No se pudo generar el dataset (código {resultado.returncode}).\n"
            f"  Pruebe a mano:  python -m packages.datagen.generar "
            f"--seed {seed} --clientes {clientes} --salida {raiz_datos}",
            file=sys.stderr,
        )
        return False
    _avisar(f"  dataset      generado en {raiz_datos}")
    return True


# --------------------------------------------------------------------------- #
# Puerto
# --------------------------------------------------------------------------- #
def puerto_libre(host: str, puerto: int) -> bool:
    """``True`` si se puede abrir ese puerto ahora mismo.

    Se comprueba **antes** de lanzar uvicorn y sin ``SO_REUSEADDR``: se quiere fallar
    aquí, con un mensaje en castellano, y no dentro del servidor con el error de socket
    del sistema operativo.
    """
    conector = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        conector.bind((host if host not in {"0.0.0.0", "::"} else "", puerto))
    except OSError:
        return False
    else:
        return True
    finally:
        conector.close()


def resolver_puerto(host: str, puerto: int, *, fijo: bool = False) -> int | None:
    """Devuelve el puerto a usar, desplazándose si el pedido está ocupado.

    ``None`` si no hay ninguno libre en la ventana, o si se exigió ``--puerto-fijo`` y
    ese en concreto no lo está.
    """
    if puerto_libre(host, puerto):
        return puerto
    if fijo:
        return None
    for candidato in range(puerto + 1, puerto + _PUERTOS_A_PROBAR):
        if puerto_libre(host, candidato):
            _avisar(f"  puerto       el {puerto} está ocupado; se usa el {candidato}")
            return candidato
    return None


# --------------------------------------------------------------------------- #
# Navegador
# --------------------------------------------------------------------------- #
def _responde(url: str, *, timeout: float = 2.0) -> bool:
    """``True`` si la URL contesta algo que no sea 404 o un fallo de transporte."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as respuesta:
            return 200 <= respuesta.status < 400
    except urllib.error.HTTPError as error:
        return error.code != 404
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def _abrir_cuando_este_lista(base: str, *, espera_s: float = 45.0) -> None:
    """Espera a ``/salud`` y abre la primera ruta de interfaz que exista.

    ``/ui`` puede no estar montada todavía —la interfaz web es una pieza aparte—, así
    que se comprueba antes de abrirla y, si no está, se abre ``/docs``, que siempre
    existe. Abrir una pestaña en un 404 es una forma rápida de que alguien crea que la
    API no arrancó.
    """
    limite = time.monotonic() + espera_s
    while time.monotonic() < limite:
        if _responde(f"{base}/salud"):
            break
        time.sleep(0.5)
    else:
        _avisar(f"  navegador    la API no respondió en {espera_s:.0f} s; no se abre nada")
        return

    for ruta in RUTAS_INTERFAZ:
        if _responde(f"{base}{ruta}"):
            _avisar(f"  navegador    abriendo {base}{ruta}")
            webbrowser.open(f"{base}{ruta}")
            return
    _avisar(f"  navegador    ni {' ni '.join(RUTAS_INTERFAZ)} respondieron; abra {base}/docs")


# --------------------------------------------------------------------------- #
# Servidor
# --------------------------------------------------------------------------- #
def _entorno_preparado() -> dict[str, str]:
    """Copia del entorno con los valores sin infraestructura ya puestos.

    ``MODO_ALMACENAMIENTO=memoria`` se salta cuando el equipo **sí** tiene una base
    declarada. La promesa de este guion es *«arranca aunque no tengas nada»*, no
    *«ignora lo que tengas»*, y la diferencia se pagaba cara: sin base, el índice
    vectorial vive en el proceso, no encuentra los vectores ya calculados y le pide al
    proveedor el corpus entero **en cada arranque** —cientos de documentos, uno por
    petición— contra una cuota diaria. Quien tiene Supabase configurado y ejecuta esto
    esperaba un arranque rápido, no quedarse sin cuota de embeddings a media tarde.
    """
    entorno = dict(os.environ)
    for clave, valor in ENTORNO_SIN_INFRAESTRUCTURA.items():
        if clave == "MODO_ALMACENAMIENTO" and _hay_base_configurada():
            continue
        entorno.setdefault(clave, valor)
    return entorno


def _analizador() -> argparse.ArgumentParser:
    """Interfaz de línea de comandos."""
    analizador = argparse.ArgumentParser(
        prog="python scripts/dev.py",
        description=(
            "Arranca recibo-claro en local: genera el dataset si falta, levanta uvicorn "
            "con recarga y abre la interfaz. Sin Docker y sin PostgreSQL."
        ),
    )
    analizador.add_argument("--host", default="127.0.0.1", help="interfaz de escucha")
    analizador.add_argument("--puerto", type=int, default=8000, help="puerto (por defecto 8000)")
    analizador.add_argument(
        "--seed", type=int, default=SEED_POR_DEFECTO, help="semilla del dataset"
    )
    analizador.add_argument(
        "--clientes", type=int, default=CLIENTES_POR_DEFECTO, help="cuentas del dataset"
    )
    analizador.add_argument("--periodo", default=PERIODO_POR_DEFECTO, help="periodo M0 (YYYY-MM)")
    analizador.add_argument(
        "--puerto-fijo",
        action="store_true",
        help="no busca otro puerto si el pedido está ocupado: falla",
    )
    analizador.add_argument("--sin-navegador", action="store_true", help="no abre ninguna pestaña")
    analizador.add_argument(
        "--sin-recarga", action="store_true", help="arranca uvicorn sin --reload"
    )
    return analizador


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de ``make dev``."""
    args = _analizador().parse_args(argv)
    entorno = _entorno_preparado()
    raiz_datos = Path(entorno.get("DATOS_SINTETICOS") or "data/sintetico")
    if not raiz_datos.is_absolute():
        raiz_datos = RAIZ / raiz_datos

    _avisar("recibo-claro · arranque local (sin Docker, sin PostgreSQL)")
    _avisar(f"  almacenamiento MODO_ALMACENAMIENTO={entorno['MODO_ALMACENAMIENTO']}")
    if not preparar_dataset(
        raiz_datos, seed=args.seed, clientes=args.clientes, periodo=args.periodo
    ):
        return _SIN_DATASET

    puerto = resolver_puerto(args.host, args.puerto, fijo=args.puerto_fijo)
    if puerto is None:
        ventana = (
            "."
            if args.puerto_fijo
            else f" y no hay ninguno libre hasta el {args.puerto + _PUERTOS_A_PROBAR - 1}."
        )
        print(
            f"\n  El puerto {args.puerto} de {args.host} está ocupado{ventana}\n"
            f"  Elija otro:  python scripts/dev.py --puerto 8010\n"
            f"  O mire quién lo tiene (Windows):  netstat -ano | findstr :{args.puerto}",
            file=sys.stderr,
        )
        return _SIN_PUERTO

    base = f"http://{'127.0.0.1' if args.host in {'0.0.0.0', '::'} else args.host}:{puerto}"
    _avisar(f"  API          {base}/docs")
    _avisar(f"  interfaz     {base}/ui")
    _avisar(f"  comprobar    python scripts/probar_e2e.py --api {base}")
    _avisar()

    if not args.sin_navegador:
        threading.Thread(target=_abrir_cuando_este_lista, args=(base,), daemon=True).start()

    orden = [
        sys.executable,
        "-m",
        "uvicorn",
        "apps.api.main:app",
        "--host",
        args.host,
        "--port",
        str(puerto),
    ]
    if not args.sin_recarga:
        # Se vigilan solo las carpetas de código: `data/` cambia al escribir la bitácora
        # de auditoría en cada turno y reiniciaría el servidor a mitad de la demo.
        orden += ["--reload", "--reload-dir", "apps", "--reload-dir", "packages"]
    try:
        return subprocess.run(orden, cwd=RAIZ, env=entorno, check=False).returncode
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C es una salida normal
        _avisar("\nrecibo-claro detenido")
        return _OK


if __name__ == "__main__":
    raise SystemExit(main())
