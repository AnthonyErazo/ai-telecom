#!/usr/bin/env python3
"""Prueba de humo de ``recibo-claro`` — el guardián de ``make demo``.

Recorre el camino completo de un turno real contra una API ya levantada:

1. ``GET  /salud``                    ¿está en pie y con qué configuración?
2. ``POST /dev/token``                token LOA2 del cliente de guion
3. ``POST /v1/explicar``              la explicación verificada
4. ``GET  /v1/auditoria``             el log encadenado del mismo turno

Y **falla ruidosamente** si la explicación no viene con
``gobernanza.verificacion_numerica == "PASS"`` y ``aserciones_no_ancladas == 0``.

Por qué esa condición y no otra: la ficha del Desafío 1 no pide "pocas
alucinaciones", pide *"Tasa de Alucinación: cero invenciones financieras
comprobables mediante logs de la terminal"*. Un despliegue que arranca pero
entrega una cifra sin respaldo en el ``FactSet`` está roto aunque devuelva 200,
y esta prueba es la que lo convierte en un fallo de build en lugar de en una
sorpresa delante del jurado.

Solo usa la biblioteca estándar: corre igual dentro del contenedor (que no
lleva ``curl`` ni ``jq``) que en el equipo de quien desarrolla.

Uso::

    python docker/smoke.py                                   # http://127.0.0.1:8000
    python docker/smoke.py --api http://127.0.0.1:8000 -v
    python docker/smoke.py --cuenta C-DEMO-02 --verbosidad DETALLE

Códigos de salida::

    0   la explicación está verificada y anclada
    1   la verificación numérica NO pasó  (fallo de la garantía del producto)
    2   la API no respondió o respondió un error de transporte
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

__all__ = ["ejecutar", "main"]

#: Salidas del proceso.
_OK = 0
_ALUCINACION = 1
_TRANSPORTE = 2

#: Ancho del marco de los banners.
_ANCHO = 78


# --------------------------------------------------------------------------- #
# Utilidades de presentación
# --------------------------------------------------------------------------- #
def _banner(titulo: str, simbolo: str = "=") -> str:
    """Marco de una línea para separar fases en la salida del CI."""
    return f"{simbolo * _ANCHO}\n{titulo}\n{simbolo * _ANCHO}"


def _paso(numero: int, texto: str) -> None:
    """Escribe el encabezado de un paso."""
    print(f"\n[{numero}/4] {texto}", flush=True)


# --------------------------------------------------------------------------- #
# Transporte
# --------------------------------------------------------------------------- #
def _peticion(
    url: str,
    *,
    metodo: str = "GET",
    cuerpo: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    """Hace una petición HTTP y devuelve ``(estado, json_decodificado)``.

    Un error HTTP no lanza: se devuelve con su cuerpo para poder enseñar el
    ``codigo`` estable que publica la API en vez de una traza de urllib.
    """
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    peticion = urllib.request.Request(url, data=datos, method=metodo)
    peticion.add_header("Accept", "application/json")
    peticion.add_header("User-Agent", "recibo-claro-smoke/1.0")
    if datos is not None:
        peticion.add_header("Content-Type", "application/json")
    if token:
        peticion.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            crudo = respuesta.read().decode("utf-8")
            return respuesta.status, (json.loads(crudo) if crudo else None)
    except urllib.error.HTTPError as error:
        crudo = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(crudo)
        except ValueError:
            return error.code, {"detalle": crudo[:400]}


def _esperar_api(base: str, *, intentos: int, espera_s: float) -> dict[str, Any]:
    """Sondea ``/salud`` hasta que responda o se agoten los intentos."""
    ultimo = ""
    for intento in range(1, intentos + 1):
        try:
            estado, cuerpo = _peticion(f"{base}/salud", timeout=5.0)
            if estado == 200 and isinstance(cuerpo, dict):
                return cuerpo
            ultimo = f"HTTP {estado}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            ultimo = str(error)
        if intento < intentos:
            print(f"      … esperando a la API ({intento}/{intentos}): {ultimo}", flush=True)
            time.sleep(espera_s)
    raise SystemExit(_fallo_transporte(base, ultimo))


def _fallo_transporte(base: str, motivo: str) -> int:
    """Imprime el fallo de conectividad y devuelve su código de salida."""
    print(_banner("SMOKE FALLIDO · la API no respondió", "!"), file=sys.stderr)
    print(f"  destino : {base}/salud", file=sys.stderr)
    print(f"  motivo  : {motivo}", file=sys.stderr)
    print("  revise  : docker compose ps · make logs", file=sys.stderr)
    return _TRANSPORTE


# --------------------------------------------------------------------------- #
# Prueba
# --------------------------------------------------------------------------- #
def ejecutar(
    base: str,
    *,
    cuenta: str = "C-DEMO-01",
    periodo: str | None = "2026-07",
    verbosidad: str = "CORTO",
    canal: str = "APP",
    utterance: str = "¿por qué me vino más caro este mes si cambié a un plan más barato?",
    intentos: int = 30,
    espera_s: float = 2.0,
    verboso: bool = False,
) -> int:
    """Ejecuta el recorrido completo y devuelve el código de salida."""
    print(_banner(f"SMOKE recibo-claro · {cuenta} · {base}"))

    # --- 1. Salud ---------------------------------------------------------- #
    _paso(1, "GET /salud")
    salud = _esperar_api(base, intentos=intentos, espera_s=espera_s)
    print(
        f"      ok · entorno={salud.get('entorno')} · llm={salud.get('llm_mode')}"
        f" · reglas={salud.get('rules_version')}"
        f" · verificador_estricto={salud.get('verificador_estricto')}"
    )

    # --- 2. Token ---------------------------------------------------------- #
    _paso(2, f"POST /dev/token  (LOA2, {cuenta})")
    estado, cuerpo = _peticion(
        f"{base}/dev/token",
        metodo="POST",
        cuerpo={"cuenta_id": cuenta, "nivel": "LOA2", "canal": canal},
    )
    if estado != 200 or not isinstance(cuerpo, dict) or not cuerpo.get("access_token"):
        print(_banner("SMOKE FALLIDO · no se pudo emitir el token", "!"), file=sys.stderr)
        print(f"  HTTP {estado}: {json.dumps(cuerpo, ensure_ascii=False)[:400]}", file=sys.stderr)
        print(
            "  `/dev/token` solo existe con ENTORNO=dev. En otro entorno el token\n"
            "  lo emite el IdP de Movistar y esta prueba debe recibirlo por --token.",
            file=sys.stderr,
        )
        return _TRANSPORTE
    token = str(cuerpo["access_token"])
    print(
        f"      ok · sub={cuerpo.get('claims', {}).get('sub')} · acr={cuerpo.get('claims', {}).get('acr')}"
    )

    # --- 3. Explicación ---------------------------------------------------- #
    _paso(3, "POST /v1/explicar")
    peticion: dict[str, Any] = {"verbosidad": verbosidad, "canal": canal, "utterance": utterance}
    if periodo:
        peticion["periodo"] = periodo
    estado, respuesta = _peticion(
        f"{base}/v1/explicar", metodo="POST", cuerpo=peticion, token=token, timeout=45.0
    )
    if estado != 200 or not isinstance(respuesta, dict):
        print(
            _banner("SMOKE FALLIDO · /v1/explicar no devolvió una explicación", "!"),
            file=sys.stderr,
        )
        print(
            f"  HTTP {estado}: {json.dumps(respuesta, ensure_ascii=False)[:600]}", file=sys.stderr
        )
        return _TRANSPORTE

    gobernanza = respuesta.get("gobernanza") or {}
    telemetria = respuesta.get("telemetria") or {}
    veredicto = gobernanza.get("verificacion_numerica")
    no_ancladas = gobernanza.get("aserciones_no_ancladas")
    trace_id = telemetria.get("explicacion_id") or respuesta.get("trace_id")

    print(
        f"      veredicto={veredicto} · totales={gobernanza.get('aserciones_totales')}"
        f" · ancladas={gobernanza.get('aserciones_ancladas')}"
        f" · no ancladas={no_ancladas}"
    )
    print(
        f"      modo={gobernanza.get('modo')} · modelo={gobernanza.get('model_version')}"
        f" · factset={str(gobernanza.get('factset_sha256'))[:12]} · trace={trace_id}"
    )
    if verboso:
        for bloque in respuesta.get("bloques") or []:
            if bloque.get("tipo") in {"texto", "aviso"} and bloque.get("texto"):
                print(f"      | {bloque['texto']}")

    # --- 4. Auditoría ------------------------------------------------------ #
    _paso(4, "GET /v1/auditoria  (la prueba en el log)")
    estado, bitacora = _peticion(
        f"{base}/v1/auditoria?trace_id={trace_id}&incluir_eventos=false", token=token
    )
    if estado == 200 and isinstance(bitacora, dict):
        for linea in bitacora.get("terminal") or []:
            print(f"      {linea}")
        print(f"      cadena_valida={bitacora.get('cadena_valida')}")
    else:
        print(f"      aviso: la bitácora no está disponible (HTTP {estado})")

    # --- Veredicto --------------------------------------------------------- #
    if veredicto == "PASS" and no_ancladas == 0:
        print()
        print(_banner("SMOKE OK · explicación verificada, cero cifras sin anclar"))
        return _OK

    print(file=sys.stderr)
    print(_banner("SMOKE FALLIDO · VERIFICACIÓN NUMÉRICA NO SUPERADA", "!"), file=sys.stderr)
    print(f"  verificacion_numerica  : {veredicto!r}  (se exige 'PASS')", file=sys.stderr)
    print(f"  aserciones_no_ancladas : {no_ancladas!r}  (se exige 0)", file=sys.stderr)
    print(f"  trace_id               : {trace_id}", file=sys.stderr)
    print(
        "\n  Una cifra sin respaldo en el FactSet llegó a la respuesta, o el\n"
        "  verificador no pudo anclarla. Esto NO es un aviso: es el compromiso\n"
        "  del producto (TA_respuesta = 0) incumplido.\n"
        f"\n  Diagnóstico:  curl '{base}/v1/auditoria?trace_id={trace_id}&etapas=VERIFY'\n",
        file=sys.stderr,
    )
    return _ALUCINACION


def _analizador() -> argparse.ArgumentParser:
    """Interfaz de línea de comandos."""
    analizador = argparse.ArgumentParser(
        prog="python docker/smoke.py",
        description="Prueba de humo: explica el recibo de un cliente de guion y exige PASS.",
    )
    analizador.add_argument("--api", default="http://127.0.0.1:8000", help="URL base de la API")
    analizador.add_argument("--cuenta", default="C-DEMO-01", help="cuenta de guion a explicar")
    analizador.add_argument("--periodo", default="2026-07", help="periodo YYYY-MM ('' = el último)")
    analizador.add_argument("--verbosidad", default="CORTO", choices=("CORTO", "DETALLE"))
    analizador.add_argument("--canal", default="APP", choices=("APP", "BOT", "WHATSAPP", "ASESOR"))
    analizador.add_argument("--utterance", default=None, help="pregunta del cliente")
    analizador.add_argument("--intentos", type=int, default=30, help="sondeos de /salud")
    analizador.add_argument("--espera", type=float, default=2.0, help="segundos entre sondeos")
    analizador.add_argument(
        "-v", "--verbose", action="store_true", help="imprime el texto entregado"
    )
    return analizador


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada. Devuelve 0, 1 o 2 según el resultado."""
    args = _analizador().parse_args(argv)
    extra = {"utterance": args.utterance} if args.utterance else {}
    try:
        return ejecutar(
            args.api.rstrip("/"),
            cuenta=args.cuenta,
            periodo=args.periodo or None,
            verbosidad=args.verbosidad,
            canal=args.canal,
            intentos=args.intentos,
            espera_s=args.espera,
            verboso=args.verbose,
            **extra,
        )
    except SystemExit as salida:  # _esperar_api ya imprimió el diagnóstico
        return int(salida.code or _TRANSPORTE)


if __name__ == "__main__":
    raise SystemExit(main())
