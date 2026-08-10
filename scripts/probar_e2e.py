#!/usr/bin/env python3
"""Verificador end-to-end de ``recibo-claro`` contra una API ya levantada.

Es el guardián de ``make probar``. Recorre el camino completo de la demo y responde a
una sola pregunta: *¿esto funciona de verdad, en esta máquina, ahora mismo?*

    token → hechos → explicar (CORTO y DETALLE) → evidencia → derivación
          → auditoría → cadena de hashes → modo adversario → niveles → errores

Cada paso imprime **PASA** o **FALLA** con lo que comprobó, y el proceso sale con
código distinto de cero si algo falla: sirve igual para una demo delante del jurado que
para un CI.

Qué NO necesita
---------------
* **Ni PostgreSQL ni Docker.** No abre una sola conexión: habla HTTP con la API, y la
  API funciona con ``MODO_ALMACENAMIENTO=memoria``/``auto`` leyendo ``data/sintetico``.
* **Ni dependencias.** Solo la biblioteca estándar (``urllib``), como ``docker/smoke.py``:
  corre dentro del contenedor, en Git Bash y en un PowerShell recién instalado.
* **Ni red.** Todo va contra ``127.0.0.1``.

Qué se considera un fallo
-------------------------
No basta con recibir ``200``. Se exige lo que promete el producto:

* ``verificacion_numerica == "PASS"`` y ``aserciones_no_ancladas == 0`` en las dos
  verbosidades — la métrica oficial es *cero invenciones financieras*;
* el invariante del FactSet conciliado (``residual_cent == 0``);
* la cadena de hashes de la bitácora íntegra;
* que el **modo adversario** haga ``FAIL`` con una cifra inventada: un ``PASS`` sin caso
  negativo no prueba nada;
* que ``LOA1`` no vea un solo dígito y que ``LOA0`` no pueda leer los hechos.

Uso::

    python scripts/probar_e2e.py                       # http://127.0.0.1:8000
    python scripts/probar_e2e.py --api http://127.0.0.1:8000 --cuenta C-DEMO-02
    python scripts/probar_e2e.py --json                # informe para CI
    python scripts/probar_e2e.py --esperar 60          # espera a que la API arranque

Códigos de salida::

    0   todos los pasos pasaron
    1   algún paso falló (el informe dice cuál y por qué)
    2   la API no respondió: no se pudo probar nada
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Informe", "Paso", "ejecutar", "main"]

#: Salidas del proceso.
_OK = 0
_FALLO = 1
_SIN_API = 2

#: Ancho de los marcos del informe.
_ANCHO = 78

#: Cuenta y periodo del guion. Coinciden con el dataset determinístico (seed 20260804).
CUENTA_POR_DEFECTO = "C-DEMO-01"
PERIODO_POR_DEFECTO = "2026-07"

#: Céntimos que inventa el modo adversario. El verificador debe cazarlos.
DELTA_ALUCINACION = 731


# --------------------------------------------------------------------------- #
# Resultado de un paso
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Paso:
    """Un paso del recorrido y su veredicto."""

    nombre: str
    ok: bool
    detalle: str
    datos: dict[str, Any] = field(default_factory=dict)

    @property
    def etiqueta(self) -> str:
        """``PASA`` o ``FALLA``, que es lo que se lee en la terminal."""
        return "PASA " if self.ok else "FALLA"

    def linea(self) -> str:
        """Fila del informe: veredicto, nombre y evidencia."""
        return f"  [{self.etiqueta}] {self.nombre:<34} {self.detalle}"


@dataclass(slots=True)
class Informe:
    """Recorrido completo: los pasos en orden y el veredicto agregado."""

    api: str
    cuenta: str
    periodo: str
    pasos: list[Paso] = field(default_factory=list)
    inicio: float = field(default_factory=time.perf_counter)
    #: En modo ``--json`` no se imprime nada al vuelo: la salida debe ser JSON puro.
    silencioso: bool = False

    def anotar(self, nombre: str, ok: bool, detalle: str, **datos: Any) -> Paso:
        """Registra un paso, lo imprime al vuelo y lo devuelve."""
        paso = Paso(nombre=nombre, ok=ok, detalle=detalle, datos=datos)
        self.pasos.append(paso)
        if not self.silencioso:
            print(paso.linea(), flush=True)
        return paso

    @property
    def fallidos(self) -> list[Paso]:
        """Pasos que no pasaron, en orden de aparición."""
        return [paso for paso in self.pasos if not paso.ok]

    @property
    def ok(self) -> bool:
        """``True`` si todos los pasos pasaron."""
        return not self.fallidos

    def a_json(self) -> dict[str, Any]:
        """Informe serializable, para CI."""
        return {
            "api": self.api,
            "cuenta": self.cuenta,
            "periodo": self.periodo,
            "ok": self.ok,
            "total": len(self.pasos),
            "pasan": len(self.pasos) - len(self.fallidos),
            "fallan": len(self.fallidos),
            "duracion_s": round(time.perf_counter() - self.inicio, 2),
            "pasos": [
                {"nombre": p.nombre, "ok": p.ok, "detalle": p.detalle, **p.datos}
                for p in self.pasos
            ],
        }


# --------------------------------------------------------------------------- #
# Transporte
# --------------------------------------------------------------------------- #
class ErrorTransporte(RuntimeError):
    """La API no respondió: no es un fallo de producto, es que no hay API."""


class ErrorPaso(RuntimeError):
    """Un paso imprescindible devolvió algo con lo que no se puede seguir.

    Se distingue de :class:`ErrorTransporte` a propósito: aquí la API **sí** contestó,
    así que esto es un fallo del producto (código de salida 1) y no de la conectividad
    (código 2). Un 404 de cuenta inexistente no debe leerse como «la API está caída».
    """


def peticion(
    url: str,
    *,
    metodo: str = "GET",
    cuerpo: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 45.0,
) -> tuple[int, Any, dict[str, str]]:
    """Hace una petición y devuelve ``(estado, json, cabeceras)``.

    Un error HTTP **no lanza**: se devuelve con su cuerpo, porque la API publica códigos
    estables (``NIVEL_INSUFICIENTE``, ``CUENTA_NO_ENCONTRADA``…) y varios pasos de esta
    prueba consisten precisamente en comprobar que el error correcto llega.
    """
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    solicitud = urllib.request.Request(url, data=datos, method=metodo)
    solicitud.add_header("Accept", "application/json")
    solicitud.add_header("User-Agent", "recibo-claro-probar-e2e/1.0")
    if datos is not None:
        solicitud.add_header("Content-Type", "application/json")
    if token:
        solicitud.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(solicitud, timeout=timeout) as respuesta:
            crudo = respuesta.read().decode("utf-8")
            cabeceras = {clave.lower(): valor for clave, valor in respuesta.headers.items()}
            return respuesta.status, (json.loads(crudo) if crudo else None), cabeceras
    except urllib.error.HTTPError as error:
        crudo = error.read().decode("utf-8", errors="replace")
        cabeceras = {clave.lower(): valor for clave, valor in (error.headers or {}).items()}
        try:
            return error.code, json.loads(crudo), cabeceras
        except ValueError:
            return error.code, {"detalle": crudo[:400]}, cabeceras
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise ErrorTransporte(str(error)) from error


def esperar_api(base: str, *, segundos: float) -> dict[str, Any]:
    """Sondea ``/salud`` hasta ``segundos``. Lanza :class:`ErrorTransporte` si no llega."""
    limite = time.monotonic() + max(segundos, 0.0)
    ultimo = "sin intentos"
    while True:
        try:
            estado, cuerpo, _ = peticion(f"{base}/salud", timeout=5.0)
            if estado == 200 and isinstance(cuerpo, dict):
                return cuerpo
            ultimo = f"HTTP {estado}"
        except ErrorTransporte as error:
            ultimo = str(error)
        if time.monotonic() >= limite:
            raise ErrorTransporte(ultimo)
        time.sleep(1.0)


# --------------------------------------------------------------------------- #
# Utilidades de lectura de la respuesta
# --------------------------------------------------------------------------- #
def _texto_entregado(respuesta: dict[str, Any]) -> str:
    """Concatena el texto de todos los bloques que llevan texto."""
    partes: list[str] = []
    for bloque in respuesta.get("bloques") or []:
        if bloque.get("texto"):
            partes.append(str(bloque["texto"]))
    return " ".join(partes)


def _emitir_token(base: str, cuenta: str, nivel: str, **extra: Any) -> str:
    """Pide un token a ``/dev/token`` y devuelve el ``access_token``."""
    cuerpo: dict[str, Any] = {"cuenta_id": cuenta, "nivel": nivel, **extra}
    estado, datos, _ = peticion(f"{base}/dev/token", metodo="POST", cuerpo=cuerpo)
    if estado != 200 or not isinstance(datos, dict) or not datos.get("access_token"):
        raise ErrorPaso(
            f"POST /dev/token → HTTP {estado}: {json.dumps(datos, ensure_ascii=False)[:200]}. "
            "Solo existe con ENTORNO=dev."
        )
    return str(datos["access_token"])


def _explicar(
    base: str, token: str, cuenta: str, periodo: str, verbosidad: str, conversacion: str
) -> dict[str, Any]:
    """Un turno de ``POST /v1/explicar`` con el contrato real del endpoint."""
    estado, datos, _ = peticion(
        f"{base}/v1/explicar",
        metodo="POST",
        cuerpo={
            "conversation_id": conversacion,
            "cuenta_id": cuenta,
            "periodo": periodo,
            "verbosidad": verbosidad,
            "canal": "APP",
            "utterance": "¿por qué me vino más caro este mes?",
        },
        token=token,
    )
    if estado != 200 or not isinstance(datos, dict):
        raise ErrorPaso(
            f"POST /v1/explicar ({verbosidad}) → HTTP {estado}: "
            f"{json.dumps(datos, ensure_ascii=False)[:300]}"
        )
    return datos


# --------------------------------------------------------------------------- #
# El recorrido
# --------------------------------------------------------------------------- #
def ejecutar(
    base: str,
    *,
    cuenta: str = CUENTA_POR_DEFECTO,
    periodo: str = PERIODO_POR_DEFECTO,
    espera_s: float = 30.0,
    silencioso: bool = False,
) -> Informe:
    """Recorre el flujo completo y devuelve el informe con un paso por comprobación.

    Si un paso imprescindible falla (:class:`ErrorPaso`) el recorrido se interrumpe pero
    el informe se devuelve igual, con lo comprobado hasta ahí y el motivo del corte:
    media prueba con diagnóstico vale más que una traza de Python.
    """
    informe = Informe(api=base, cuenta=cuenta, periodo=periodo, silencioso=silencioso)
    try:
        _recorrido(informe, base, cuenta=cuenta, periodo=periodo, espera_s=espera_s)
    except ErrorPaso as error:
        informe.anotar("recorrido interrumpido", False, str(error))
    return informe


def _recorrido(informe: Informe, base: str, *, cuenta: str, periodo: str, espera_s: float) -> None:
    """El recorrido en sí. Anota cada comprobación en ``informe``."""
    # -- 1. La API está en pie --------------------------------------------- #
    salud = esperar_api(base, segundos=espera_s)
    informe.anotar(
        "salud",
        salud.get("estado") == "ok",
        f"entorno={salud.get('entorno')} · llm={salud.get('llm_mode')} "
        f"· reglas={salud.get('rules_version')}",
        **{"en_pie_s": salud.get("en_pie_s")},
    )

    # -- 2. Preparación: sin base de datos y con el corpus cargado ---------- #
    estado, preparacion, _ = peticion(f"{base}/salud/preparacion")
    almacenamiento = (preparacion or {}).get("almacenamiento") or {}
    rag = (preparacion or {}).get("rag") or {}
    vectorial = rag.get("vectorial") or {}
    corpus = rag.get("corpus") or {}
    informe.anotar(
        "preparacion (sin PostgreSQL)",
        estado == 200 and bool(corpus.get("concepto_catalogo")),
        f"almacenamiento={almacenamiento.get('modo')}→{vectorial.get('respaldo')} "
        f"· catálogo={corpus.get('concepto_catalogo')} faq={corpus.get('faq')} "
        f"casuística={corpus.get('casuistica')} · listo={(preparacion or {}).get('listo')}",
        motivo=vectorial.get("motivo_degradacion"),
    )

    # -- 3. El ACL lee el dataset del disco --------------------------------- #
    estado, sistemas, _ = peticion(f"{base}/salud/sistemas")
    brainybill = (sistemas or {}).get("brainybill") or {}
    informe.anotar(
        "sistemas externos (ACL)",
        estado == 200 and bool(brainybill.get("alcanzable")),
        f"BrainyBill={brainybill.get('transporte')} · alcanzable={brainybill.get('alcanzable')}",
        destino=brainybill.get("destino"),
    )

    # -- 4. Token LOA2 ------------------------------------------------------ #
    token_loa2 = _emitir_token(base, cuenta, "LOA2", canal="APP")
    informe.anotar("token LOA2", bool(token_loa2), f"cuenta={cuenta} · {len(token_loa2)} bytes")

    # -- 5. Hechos conciliados ---------------------------------------------- #
    estado, hechos, _ = peticion(
        f"{base}/v1/hechos?cuenta_id={cuenta}&periodo={periodo}", token=token_loa2
    )
    hechos = hechos if isinstance(hechos, dict) else {}
    invariante = hechos.get("invariante") or {}
    residual = invariante.get("residual_cent")
    delta = hechos.get("delta_total_cent")
    suma = (hechos.get("total_previo_cent") or 0) + (delta or 0)
    informe.anotar(
        "hechos conciliados",
        estado == 200
        and residual == 0
        and bool(invariante.get("cumple", True))
        and suma == hechos.get("total_actual_cent"),
        f"Δ={delta} c · residual={residual} c · líneas={len(hechos.get('lineas') or [])} "
        f"· sha256={str(hechos.get('sha256'))[:12]}",
        factset_sha256=hechos.get("sha256"),
    )

    # -- 6. Explicación CORTO ----------------------------------------------- #
    conversacion = str(uuid.uuid4())
    corto = _explicar(base, token_loa2, cuenta, periodo, "CORTO", conversacion)
    gobernanza = corto.get("gobernanza") or {}
    trace_id = str((corto.get("telemetria") or {}).get("explicacion_id") or corto.get("trace_id"))
    informe.anotar(
        "explicar CORTO",
        gobernanza.get("verificacion_numerica") == "PASS"
        and gobernanza.get("aserciones_no_ancladas") == 0
        and bool(gobernanza.get("anclado")),
        f"{gobernanza.get('verificacion_numerica')} · {gobernanza.get('aserciones_ancladas')}"
        f"/{gobernanza.get('aserciones_totales')} ancladas · {len(corto.get('bloques') or [])} "
        f"bloques · modo={gobernanza.get('modo')} · trace={trace_id}",
        trace_id=trace_id,
    )

    # El FactSet que citó la explicación tiene que ser el mismo que devolvió /v1/hechos:
    # si no, la respuesta se está apoyando en cifras de otro cálculo.
    informe.anotar(
        "mismo FactSet sellado",
        bool(hechos.get("sha256")) and gobernanza.get("factset_sha256") == hechos.get("sha256"),
        f"explicar={str(gobernanza.get('factset_sha256'))[:12]} "
        f"· hechos={str(hechos.get('sha256'))[:12]}",
    )

    # -- 7. Explicación DETALLE --------------------------------------------- #
    detalle = _explicar(base, token_loa2, cuenta, periodo, "DETALLE", str(uuid.uuid4()))
    gob_detalle = detalle.get("gobernanza") or {}
    tipos = [str(bloque.get("tipo")) for bloque in detalle.get("bloques") or []]
    informe.anotar(
        "explicar DETALLE",
        gob_detalle.get("verificacion_numerica") == "PASS"
        and gob_detalle.get("aserciones_no_ancladas") == 0
        and len(tipos) >= len(corto.get("bloques") or []),
        f"{gob_detalle.get('verificacion_numerica')} · "
        f"{gob_detalle.get('aserciones_ancladas')}/{gob_detalle.get('aserciones_totales')} "
        f"ancladas · bloques={','.join(tipos)}",
    )

    # -- 8. Evidencia de cada afirmación ------------------------------------ #
    estado, evidencia, _ = peticion(f"{base}/v1/evidencia/{trace_id}", token=token_loa2)
    evidencia = evidencia if isinstance(evidencia, dict) else {}
    tipos_evidencia = sorted({str(item.get("tipo")) for item in evidencia.get("items") or []})
    informe.anotar(
        "evidencia del turno",
        estado == 200
        and (evidencia.get("total") or 0) > 0
        and evidencia.get("factset_sha256") == gobernanza.get("factset_sha256"),
        f"{evidencia.get('total')} items · tipos={','.join(tipos_evidencia)} "
        f"· saneado={evidencia.get('corpus_saneado')}",
    )

    # -- 9. Derivación con contexto cargado --------------------------------- #
    estado, derivacion, _ = peticion(
        f"{base}/v1/derivacion",
        metodo="POST",
        cuerpo={
            "conversation_id": conversacion,
            "cuenta_id": cuenta,
            "periodo": periodo,
            "motivo_codigo": "PETICION_HUMANO",
            "utterance": "quiero hablar con una persona",
        },
        token=token_loa2,
    )
    derivacion = derivacion if isinstance(derivacion, dict) else {}
    context_ref = str(derivacion.get("context_ref") or "")
    brief = str(derivacion.get("resumen_asesor") or "")
    informe.anotar(
        "derivación a asesor",
        estado == 200 and context_ref.startswith("ctx-") and derivacion.get("lineas_brief", 0) > 0,
        f"context_ref={context_ref} · cola={derivacion.get('cola')} "
        f"· brief={derivacion.get('lineas_brief')} líneas · vigencia="
        f"{derivacion.get('vigencia_min')} min",
        primera_linea_brief=brief.splitlines()[0] if brief else "",
    )

    # -- 10. El asesor recupera el contexto --------------------------------- #
    estado, contexto, _ = peticion(f"{base}/v1/derivacion/{context_ref}", token=token_loa2)
    contexto = contexto if isinstance(contexto, dict) else {}
    informe.anotar(
        "contexto recuperable (104)",
        estado == 200 and contexto.get("context_ref") == context_ref,
        f"HTTP {estado} · claves={len(contexto)} · cuenta={contexto.get('cuenta_id')}",
    )

    # -- 11. Auditoría del turno -------------------------------------------- #
    estado, bitacora, _ = peticion(
        f"{base}/v1/auditoria?trace_id={trace_id}&incluir_eventos=false", token=token_loa2
    )
    bitacora = bitacora if isinstance(bitacora, dict) else {}
    terminal = [str(linea) for linea in bitacora.get("terminal") or []]
    # Con ``incluir_eventos=false`` el cuerpo no trae ``eventos`` ni ``total_eventos``:
    # el recuento del turno vive en el resumen, que es lo que se pinta en la terminal.
    resumen_turno = bitacora.get("resumen") or {}
    informe.anotar(
        "auditoría del turno",
        estado == 200
        and (resumen_turno.get("eventos") or 0) > 0
        and bitacora.get("cadena_valida") is True
        and bool(terminal),
        f"{resumen_turno.get('eventos')} eventos · veredicto={resumen_turno.get('veredicto')} "
        f"· cadena_valida={bitacora.get('cadena_valida')}",
        terminal=terminal,
    )

    # -- 12. Cadena de hashes completa -------------------------------------- #
    estado, cadena, _ = peticion(f"{base}/v1/auditoria/cadena", token=token_loa2)
    cadena = cadena if isinstance(cadena, dict) else {}
    informe.anotar(
        "cadena de hashes (JSONL local)",
        estado == 200 and cadena.get("cadena_valida") is True,
        f"{cadena.get('eventos')} eventos · íntegra={cadena.get('cadena_valida')} "
        f"· último={str(cadena.get('hash_ultimo'))[:12]}",
        ruta=cadena.get("ruta"),
    )

    # -- 13. LOA1: explica sin un solo dígito -------------------------------- #
    token_loa1 = _emitir_token(base, cuenta, "LOA1", canal="WHATSAPP")
    loa1 = _explicar(base, token_loa1, cuenta, periodo, "CORTO", str(uuid.uuid4()))
    texto_loa1 = _texto_entregado(loa1)
    digitos = len(re.findall(r"\d", texto_loa1))
    informe.anotar(
        "LOA1 sin importes",
        digitos == 0 and bool(texto_loa1),
        f"{digitos} dígitos en {len(texto_loa1)} caracteres · redactado_por_nivel="
        f"{(loa1.get('telemetria') or {}).get('redactado_por_nivel')}",
    )

    # -- 14. LOA0 no puede leer los hechos ----------------------------------- #
    token_loa0 = _emitir_token(base, cuenta, "LOA0")
    estado, error_nivel, _ = peticion(f"{base}/v1/hechos?cuenta_id={cuenta}", token=token_loa0)
    error_nivel = error_nivel if isinstance(error_nivel, dict) else {}
    informe.anotar(
        "LOA0 bloqueado en /v1/hechos",
        estado == 403 and error_nivel.get("codigo") == "NIVEL_INSUFICIENTE",
        f"HTTP {estado} · codigo={error_nivel.get('codigo')} "
        f"· nivel_requerido={error_nivel.get('nivel_requerido')}",
    )

    # -- 15. Sin token no se pasa -------------------------------------------- #
    estado, sin_token, _ = peticion(f"{base}/v1/hechos?cuenta_id={cuenta}")
    sin_token = sin_token if isinstance(sin_token, dict) else {}
    informe.anotar(
        "sin token → 401",
        estado == 401 and str(sin_token.get("codigo", "")).startswith("TOKEN_"),
        f"HTTP {estado} · codigo={sin_token.get('codigo')}",
    )

    # -- 16. Modo adversario: la cifra inventada se caza ---------------------- #
    estado, adversario, _ = peticion(
        f"{base}/dev/alucinar",
        metodo="POST",
        cuerpo={
            "activar": True,
            "delta_cent": DELTA_ALUCINACION,
            "turnos": 1,
            "cuenta_id": cuenta,
            "periodo": periodo,
        },
        token=token_loa2,
    )
    adversario = adversario if isinstance(adversario, dict) else {}
    demo = adversario.get("demo") or {}
    informe.anotar(
        "modo adversario caza la cifra",
        estado == 200
        and demo.get("veredicto_limpio") == "PASS"
        and demo.get("veredicto_envenenado") == "FAIL"
        and (demo.get("no_ancladas_envenenado") or 0) > 0,
        f"limpio={demo.get('veredicto_limpio')} · envenenado="
        f"{demo.get('veredicto_envenenado')} · infractores={demo.get('infractores')}",
        terminal=demo.get("terminal"),
    )

    # -- 17. El turno adversario acaba en derivación -------------------------- #
    envenenado = _explicar(base, token_loa2, cuenta, periodo, "CORTO", str(uuid.uuid4()))
    gob_envenenado = envenenado.get("gobernanza") or {}
    deriva = envenenado.get("derivacion") or {}
    texto_envenenado = _texto_entregado(envenenado)
    informe.anotar(
        "turno adversario → derivación",
        gob_envenenado.get("verificacion_numerica") == "FAIL"
        and deriva.get("motivo_codigo") == "VERIFICACION_FALLIDA"
        and len(re.findall(r"\d", texto_envenenado)) == 0,
        f"veredicto={gob_envenenado.get('verificacion_numerica')} · motivo="
        f"{deriva.get('motivo_codigo')} · context_ref={deriva.get('context_ref')} "
        f"· dígitos entregados={len(re.findall(r'[0-9]', texto_envenenado))}",
    )

    # -- 18. Se desactiva y se vuelve a PASS ---------------------------------- #
    peticion(f"{base}/dev/alucinar", metodo="POST", cuerpo={"activar": False}, token=token_loa2)
    limpio = _explicar(base, token_loa2, cuenta, periodo, "CORTO", str(uuid.uuid4()))
    gob_limpio = limpio.get("gobernanza") or {}
    informe.anotar(
        "modo adversario desactivado",
        gob_limpio.get("verificacion_numerica") == "PASS"
        and gob_limpio.get("aserciones_no_ancladas") == 0,
        f"{gob_limpio.get('verificacion_numerica')} · "
        f"{gob_limpio.get('aserciones_no_ancladas')} sin anclar",
    )


# --------------------------------------------------------------------------- #
# Presentación
# --------------------------------------------------------------------------- #
def _cabecera(informe: Informe) -> str:
    """Marco inicial del informe."""
    return (
        f"{'=' * _ANCHO}\n"
        f"PRUEBA END-TO-END · recibo-claro · {informe.cuenta} · {informe.periodo}\n"
        f"API {informe.api} · sin Docker · sin PostgreSQL\n"
        f"{'=' * _ANCHO}"
    )


def _cierre(informe: Informe) -> str:
    """Marco final con el recuento y, si hubo fallos, qué falló."""
    total = len(informe.pasos)
    pasan = total - len(informe.fallidos)
    duracion = round(time.perf_counter() - informe.inicio, 2)
    if informe.ok:
        return (
            f"\n{'=' * _ANCHO}\n"
            f"TODO PASA · {pasan}/{total} pasos en {duracion} s\n"
            f"Explicación verificada, cero cifras sin anclar, cadena de hashes íntegra\n"
            f"y el caso adversario bloqueado. Sin base de datos de por medio.\n"
            f"{'=' * _ANCHO}"
        )
    lineas = "\n".join(f"  · {paso.nombre}: {paso.detalle}" for paso in informe.fallidos)
    return (
        f"\n{'!' * _ANCHO}\n"
        f"HAY FALLOS · {pasan}/{total} pasos en {duracion} s\n"
        f"{'!' * _ANCHO}\n"
        f"{lineas}\n"
    )


def _analizador() -> argparse.ArgumentParser:
    """Interfaz de línea de comandos."""
    analizador = argparse.ArgumentParser(
        prog="python scripts/probar_e2e.py",
        description=(
            "Recorre el flujo completo contra una API levantada e imprime PASA/FALLA por paso."
        ),
    )
    analizador.add_argument("--api", default="http://127.0.0.1:8000", help="URL base de la API")
    analizador.add_argument("--cuenta", default=CUENTA_POR_DEFECTO, help="cuenta de guion")
    analizador.add_argument("--periodo", default=PERIODO_POR_DEFECTO, help="periodo YYYY-MM")
    analizador.add_argument(
        "--esperar",
        type=float,
        default=30.0,
        metavar="S",
        help="segundos a esperar a que /salud responda (por defecto 30)",
    )
    analizador.add_argument("--json", action="store_true", help="imprime el informe en JSON")
    return analizador


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada. Devuelve 0, 1 o 2."""
    args = _analizador().parse_args(argv)
    base = args.api.rstrip("/")
    if not args.json:
        print(_cabecera(Informe(api=base, cuenta=args.cuenta, periodo=args.periodo)))

    try:
        informe = ejecutar(
            base,
            cuenta=args.cuenta,
            periodo=args.periodo,
            espera_s=args.esperar,
            silencioso=args.json,
        )
    except ErrorTransporte as error:
        mensaje = {
            "api": base,
            "ok": False,
            "error": "LA API NO RESPONDIÓ",
            "detalle": str(error),
            "solucion": "levántela con `make dev` (o `uvicorn apps.api.main:app --port 8000`)",
        }
        if args.json:
            print(json.dumps(mensaje, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'!' * _ANCHO}", file=sys.stderr)
            print(f"NO SE PUDO PROBAR NADA · {base} no respondió", file=sys.stderr)
            print(f"  motivo   : {error}", file=sys.stderr)
            print(f"  solución : {mensaje['solucion']}", file=sys.stderr)
            print(f"{'!' * _ANCHO}", file=sys.stderr)
        return _SIN_API

    if args.json:
        print(json.dumps(informe.a_json(), ensure_ascii=False, indent=2))
    else:
        print(_cierre(informe))
    return _OK if informe.ok else _FALLO


if __name__ == "__main__":
    raise SystemExit(main())
