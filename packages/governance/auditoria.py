"""Registro de auditoría append-only con cadena de hashes (sección 7 de la especificación).

La ficha del desafío no pide "cero alucinaciones": pide *"cero invenciones financieras
**comprobables mediante logs de la terminal**"*. Este módulo es esa prueba.

Cada paso del pipeline escribe una línea JSON en un fichero JSONL. Cada línea lleva el
hash de la anterior::

    hash_n = SHA256(hash_{n-1} || json_canonico(evento_n))

Retocar un evento pasado cambia su hash, y con él el de todos los posteriores:
:func:`verificar_cadena` señala el índice exacto donde se rompió. El log no es un
registro de depuración, es una **evidencia**.

Etapas (``EtapaAuditoria``)::

    REQUEST · FACTS_BUILT · INVARIANTE · RETRIEVE · ROUTE
    LLM_CALL · VERIFY · CITATIONS · RESPONSE · CHAIN

Contrato de ``payload`` por etapa (claves esperadas; el lector tolera que falten,
salvo las de :data:`CLAVES_OBLIGATORIAS`, que la especificación compromete):

===============  ==========================================================================
Etapa            Claves
===============  ==========================================================================
REQUEST          ``periodo``, ``canal``, ``nivel``, ``verbosidad``, ``utterance``
FACTS_BUILT      ``factset_sha256``, ``delta_total_cent``, ``total_actual_cent``,
                 ``total_previo_cent``, ``lineas``, ``causas``, ``confianza_global``,
                 **``residual_cent``** (obligatoria)
INVARIANTE       ``ok``, ``residual_cent``, ``suma_deltas_cent``, ``delta_total_cent``
RETRIEVE         ``faq``, ``casuistica``, ``catalogo``, ``saneado``, ``documentos``
ROUTE            ``derivar``, ``motivo_codigo``, ``score_incomprension``, ``modo``
LLM_CALL         ``proveedor``, ``model_version``, ``latencia_ms``, ``intento``, ``timeout``
VERIFY           ``veredicto``, ``aserciones_totales``, ``aserciones_ancladas``,
                 ``aserciones_derivadas``, ``aserciones_no_ancladas``, ``derivaciones``,
                 **``aserciones``** (obligatoria: lista completa con estado y fuente)
CITATIONS        ``citas``, ``fact_ids``
RESPONSE         ``bloques``, ``acciones``, ``modo``, ``derivada``, ``latencia_ms``,
                 ``silence_probe_id``
CHAIN            ``eventos``, ``hash_final``, ``cadena_valida``
===============  ==========================================================================

Uso típico::

    registro = registro_por_defecto()
    registro.emitir(EtapaAuditoria.REQUEST, trace_id, {"periodo": "2026-07"},
                    cuenta_ref="C-DEMO-01", nivel=NivelAseguramiento.LOA2)
    ...
    registro.cerrar_turno(trace_id)
    print(formatear_para_terminal(registro.leer(trace_id), trace_id))
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento
from packages.core_domain.esquemas.auditoria import HASH_GENESIS, EventoAuditoria

__all__ = [
    "CLAVES_OBLIGATORIAS",
    "CLAVES_SENSIBLES",
    "GRUPOS_TERMINAL",
    "MAX_LINEAS_TURNO",
    "RUTA_RELATIVA_AUDITORIA",
    "VAR_ENTORNO_RUTA",
    "ContadorAserciones",
    "RegistroAuditoria",
    "ResumenTurno",
    "formatear_para_terminal",
    "leer_eventos",
    "registro_por_defecto",
    "ruta_auditoria_por_defecto",
    "verificar_cadena",
]

_LOG = logging.getLogger(__name__)

#: Ruta del JSONL de auditoría relativa a la raíz del proyecto.
RUTA_RELATIVA_AUDITORIA = Path("data") / "auditoria" / "eventos.jsonl"

#: Variable de entorno que sobrescribe la ruta del registro.
VAR_ENTORNO_RUTA = "AUDIT_LOG_PATH"

#: Máximo de líneas de evento que imprime la vista de terminal por turno (sin la cabecera).
MAX_LINEAS_TURNO = 6

#: Claves de payload que nunca se escriben en claro: el registro no es sitio para PII.
CLAVES_SENSIBLES = frozenset(
    {
        "dni",
        "documento",
        "documento_identidad",
        "email",
        "correo",
        "telefono",
        "celular",
        "msisdn",
        "numero_linea",
        "direccion",
        "tarjeta",
        "password",
        "token",
        "authorization",
        "jwt",
        "gemini_api_key",
        "api_key",
    }
)

#: Marcador que sustituye a un valor sensible.
MARCA_REDACTADO = "«redactado»"

#: Claves que la especificación compromete explícitamente para ciertas etapas.
CLAVES_OBLIGATORIAS: dict[EtapaAuditoria, tuple[str, ...]] = {
    EtapaAuditoria.FACTS_BUILT: ("residual_cent",),
    EtapaAuditoria.VERIFY: ("aserciones",),
}

#: Agrupación de etapas en las seis líneas de la vista de terminal.
GRUPOS_TERMINAL: tuple[tuple[str, tuple[EtapaAuditoria, ...]], ...] = (
    ("PETICIÓN", (EtapaAuditoria.REQUEST,)),
    ("HECHOS", (EtapaAuditoria.FACTS_BUILT, EtapaAuditoria.INVARIANTE)),
    ("CONTEXTO", (EtapaAuditoria.RETRIEVE, EtapaAuditoria.ROUTE)),
    ("GENERACIÓN", (EtapaAuditoria.LLM_CALL,)),
    ("VERIFICA", (EtapaAuditoria.VERIFY, EtapaAuditoria.CITATIONS)),
    ("RESPUESTA", (EtapaAuditoria.RESPONSE, EtapaAuditoria.CHAIN)),
)


# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #
def _raiz_proyecto() -> Path:
    """Sube desde este fichero hasta el directorio que contiene ``pyproject.toml``."""
    actual = Path(__file__).resolve()
    for candidato in actual.parents:
        if (candidato / "pyproject.toml").is_file():
            return candidato
    return actual.parents[2]


def ruta_auditoria_por_defecto() -> Path:
    """Ruta del JSONL: ``$AUDIT_LOG_PATH`` si está definida, si no ``data/auditoria/eventos.jsonl``."""
    desde_entorno = os.getenv(VAR_ENTORNO_RUTA)
    if desde_entorno:
        return Path(desde_entorno)
    return _raiz_proyecto() / RUTA_RELATIVA_AUDITORIA


# --------------------------------------------------------------------------- #
# Saneamiento del payload
# --------------------------------------------------------------------------- #
def _sanear(valor: Any) -> Any:
    """Convierte cualquier valor a algo serializable en JSON de forma estable.

    Sin esto un ``UUID`` o un ``date`` en el payload rompería ``json_canonico`` y, con
    él, la cadena de hashes. Los ``float`` no finitos se anulan porque ``NaN`` no es
    JSON válido y volvería el fichero irreproducible entre lectores.
    """
    if valor is None or isinstance(valor, bool | int | str):
        return valor
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, Enum):
        return _sanear(valor.value)
    if isinstance(valor, BaseModel):
        return _sanear(valor.model_dump(mode="json"))
    if isinstance(valor, UUID):
        return str(valor)
    if isinstance(valor, datetime | date):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, bytes | bytearray):
        return valor.hex()
    if isinstance(valor, dict):
        return {str(clave): _sanear_campo(str(clave), sub) for clave, sub in valor.items()}
    if isinstance(valor, list | tuple | set | frozenset):
        elementos = sorted(valor, key=str) if isinstance(valor, set | frozenset) else valor
        return [_sanear(elemento) for elemento in elementos]
    return str(valor)


def _sanear_campo(clave: str, valor: Any) -> Any:
    """Sanea un valor y lo redacta si su clave está en :data:`CLAVES_SENSIBLES`."""
    if clave.strip().lower() in CLAVES_SENSIBLES:
        return MARCA_REDACTADO
    return _sanear(valor)


def sanear_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Devuelve una copia del payload serializable y sin claves sensibles en claro."""
    if not payload:
        return {}
    return {str(clave): _sanear_campo(str(clave), valor) for clave, valor in payload.items()}


# --------------------------------------------------------------------------- #
# Lectura y verificación
# --------------------------------------------------------------------------- #
def _lineas_utiles(ruta: Path) -> list[str]:
    """Lee el fichero y devuelve sus líneas no vacías (lista vacía si no existe)."""
    if not ruta.is_file():
        return []
    with ruta.open("r", encoding="utf-8") as fichero:
        return [linea for linea in (bruta.strip() for bruta in fichero) if linea]


def _ultima_linea(ruta: Path, bytes_cola: int = 1 << 16) -> str | None:
    """Última línea no vacía del fichero, leyendo solo su cola.

    Evita recorrer un registro largo al arrancar un proceso. Si la última línea no
    cupiera en la cola leída, el llamador reintenta con el fichero completo.
    """
    if not ruta.is_file():
        return None
    tamano = ruta.stat().st_size
    if tamano == 0:
        return None
    with ruta.open("rb") as fichero:
        fichero.seek(max(0, tamano - bytes_cola))
        bloque = fichero.read()
    for bruta in reversed(bloque.split(b"\n")):
        linea = bruta.strip()
        if linea:
            try:
                return linea.decode("utf-8")
            except UnicodeDecodeError:  # cola cortada a mitad de un carácter
                return None
    return None


def leer_eventos(
    ruta: str | Path,
    trace_id: str | None = None,
    *,
    etapas: Iterable[EtapaAuditoria] | None = None,
    limite: int | None = None,
) -> list[EventoAuditoria]:
    """Carga los eventos de un JSONL de auditoría, opcionalmente filtrados.

    Args:
        ruta: fichero JSONL.
        trace_id: si se indica, devuelve solo los eventos de ese turno.
        etapas: si se indica, restringe a esas etapas.
        limite: número máximo de eventos devueltos (los últimos).

    Returns:
        Los eventos en el orden en que se escribieron. Las líneas ilegibles se
        descartan con un aviso: la verificación de integridad es cosa de
        :func:`verificar_cadena`, no de la lectura.
    """
    camino = Path(ruta)
    filtro_etapas = set(etapas) if etapas is not None else None
    eventos: list[EventoAuditoria] = []
    for numero, linea in enumerate(_lineas_utiles(camino)):
        try:
            evento = EventoAuditoria.model_validate_json(linea)
        except Exception as error:  # línea corrupta: se avisa y se sigue
            _LOG.warning("línea %d ilegible en %s: %s", numero, camino, error)
            continue
        if trace_id is not None and evento.trace_id != trace_id:
            continue
        if filtro_etapas is not None and evento.etapa not in filtro_etapas:
            continue
        eventos.append(evento)
    if limite is not None and limite >= 0:
        eventos = eventos[-limite:]
    return eventos


def verificar_cadena(ruta: str | Path) -> tuple[bool, int | None]:
    """Recalcula la cadena de hashes completa del fichero.

    Comprueba, para cada línea: que el índice sea consecutivo desde 0, que
    ``hash_previo`` coincida con el hash del evento anterior (o con
    :data:`~packages.core_domain.esquemas.auditoria.HASH_GENESIS` en el primero) y que
    el hash almacenado sea el que corresponde al contenido.

    Args:
        ruta: fichero JSONL a verificar.

    Returns:
        ``(True, None)`` si la cadena es íntegra (también si el fichero está vacío o no
        existe); ``(False, indice)`` con la **posición de la primera línea que falla**.
    """
    camino = Path(ruta)
    previo = HASH_GENESIS
    for posicion, linea in enumerate(_lineas_utiles(camino)):
        try:
            evento = EventoAuditoria.model_validate_json(linea)
        except Exception:  # una línea ilegible ya rompe la cadena
            return False, posicion
        if evento.indice != posicion:
            return False, posicion
        if evento.hash_previo != previo:
            return False, posicion
        if not evento.verificar():
            return False, posicion
        previo = evento.hash
    return True, None


# --------------------------------------------------------------------------- #
# Contadores y resumen
# --------------------------------------------------------------------------- #
class ContadorAserciones(BaseModel):
    """Cifras del verificador numérico: el titular de la vista de terminal."""

    model_config = ConfigDict(extra="forbid")

    totales: int = 0
    ancladas: int = 0
    derivadas: int = 0
    no_ancladas: int = 0

    @property
    def limpio(self) -> bool:
        """Verdadero si ninguna cifra del texto quedó sin respaldo en el FactSet."""
        return self.no_ancladas == 0

    def a_cabecera(self) -> str:
        """Texto literal del contador que exige la especificación."""
        return (
            f"AFIRMACIONES NUMÉRICAS {self.totales} · "
            f"ANCLADAS {self.ancladas} · "
            f"NO ANCLADAS {self.no_ancladas}"
        )


class ResumenTurno(BaseModel):
    """Vista agregada de un turno, para ``GET /v1/auditoria?trace_id``."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    eventos: int = 0
    etapas: list[EtapaAuditoria] = Field(default_factory=list)
    cuenta_ref: str | None = None
    aserciones: ContadorAserciones = Field(default_factory=ContadorAserciones)
    veredicto: str | None = None
    modo: str | None = None
    derivada: bool = False
    residual_cent: int | None = None
    latencia_ms: int | None = None
    cadena_valida: bool = True
    indice_roto: int | None = None
    hash_final: str | None = None


def _por_etapa(eventos: Sequence[EventoAuditoria]) -> dict[EtapaAuditoria, list[EventoAuditoria]]:
    """Agrupa los eventos por etapa conservando el orden de escritura."""
    agrupados: defaultdict[EtapaAuditoria, list[EventoAuditoria]] = defaultdict(list)
    for evento in eventos:
        agrupados[evento.etapa].append(evento)
    return dict(agrupados)


def contar_aserciones(eventos: Sequence[EventoAuditoria]) -> ContadorAserciones:
    """Extrae el contador de aserciones del último evento ``VERIFY`` del turno.

    Prefiere los totales explícitos del payload; si no están, los cuenta a partir de la
    lista ``aserciones`` (cada una con su ``estado``: ANCLADA / DERIVADA / NO_ANCLADA).
    """
    verificaciones = _por_etapa(eventos).get(EtapaAuditoria.VERIFY, [])
    if not verificaciones:
        return ContadorAserciones()
    payload = verificaciones[-1].payload
    detalle = payload.get("aserciones")
    conteo = {"ANCLADA": 0, "DERIVADA": 0, "NO_ANCLADA": 0}
    if isinstance(detalle, list):
        for asercion in detalle:
            estado = str(asercion.get("estado", "")) if isinstance(asercion, dict) else ""
            if estado in conteo:
                conteo[estado] += 1
    ancladas = _entero(payload.get("aserciones_ancladas"), conteo["ANCLADA"])
    derivadas = _entero(payload.get("aserciones_derivadas"), conteo["DERIVADA"])
    no_ancladas = _entero(payload.get("aserciones_no_ancladas"), conteo["NO_ANCLADA"])
    totales = _entero(payload.get("aserciones_totales"), ancladas + derivadas + no_ancladas)
    return ContadorAserciones(
        totales=totales, ancladas=ancladas, derivadas=derivadas, no_ancladas=no_ancladas
    )


def _entero(valor: Any, por_defecto: int = 0) -> int:
    """Convierte a ``int`` con tolerancia; devuelve ``por_defecto`` si no se puede."""
    if isinstance(valor, bool) or valor is None:
        return por_defecto
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto


def _entero_opcional(valor: Any) -> int | None:
    """Como :func:`_entero` pero devolviendo ``None`` cuando el valor no existe."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #
class RegistroAuditoria:
    """Escritor y lector del JSONL de auditoría encadenado.

    El fichero se abre siempre en modo *append* y nunca se reescribe. El estado de la
    cadena (índice siguiente y último hash) se recupera al construir el registro
    leyendo la cola del fichero, de modo que reiniciar el proceso continúa la misma
    cadena en vez de empezar otra.

    Es seguro entre hilos. Entre procesos detecta que el fichero creció por fuera
    (comparando el tamaño con el que dejó) y recupera el estado antes de escribir; aun
    así, la escritura simultánea desde dos procesos no está garantizada y no ocurre en
    esta arquitectura (un único proceso de API escribe).
    """

    def __init__(
        self,
        ruta: str | Path | None = None,
        *,
        actor: str | None = None,
        autocrear: bool = True,
        sincronizar: bool = True,
    ) -> None:
        """Abre (o crea) el registro y recupera el estado de la cadena.

        Args:
            ruta: fichero JSONL. Por defecto :func:`ruta_auditoria_por_defecto`.
            actor: componente que se anota por defecto en cada evento (p. ej. ``"api"``).
            autocrear: crea el directorio contenedor si no existe.
            sincronizar: fuerza ``fsync`` tras cada evento. Es lo correcto para una
                evidencia; desactívelo solo en pruebas masivas.
        """
        self.ruta = Path(ruta) if ruta is not None else ruta_auditoria_por_defecto()
        self.actor = actor
        self.sincronizar = sincronizar
        self._cerrojo = threading.RLock()
        self._indice_siguiente = 0
        self._hash_ultimo = HASH_GENESIS
        self._tamano_visto = -1
        if autocrear:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self._recuperar_estado()

    # ------------------------------------------------------------------ #
    # Estado de la cadena
    # ------------------------------------------------------------------ #
    def _recuperar_estado(self) -> None:
        """Relee la cola del fichero para continuar la cadena donde se quedó."""
        ultima = _ultima_linea(self.ruta)
        evento: EventoAuditoria | None = None
        if ultima is not None:
            try:
                evento = EventoAuditoria.model_validate_json(ultima)
            except Exception:  # cola cortada: se reintenta leyendo todo
                lineas = _lineas_utiles(self.ruta)
                if lineas:
                    try:
                        evento = EventoAuditoria.model_validate_json(lineas[-1])
                    except Exception:
                        evento = None
        if evento is None:
            self._indice_siguiente = 0
            self._hash_ultimo = HASH_GENESIS
        else:
            self._indice_siguiente = evento.indice + 1
            self._hash_ultimo = evento.hash or HASH_GENESIS
        self._tamano_visto = self.ruta.stat().st_size if self.ruta.is_file() else 0

    def _sincronizar_con_fichero(self) -> None:
        """Si el fichero creció por fuera de este proceso, recupera el estado."""
        tamano = self.ruta.stat().st_size if self.ruta.is_file() else 0
        if tamano != self._tamano_visto:
            self._recuperar_estado()

    @property
    def indice_siguiente(self) -> int:
        """Índice que tomará el próximo evento escrito."""
        return self._indice_siguiente

    @property
    def hash_ultimo(self) -> str:
        """Hash del último evento de la cadena (génesis si el registro está vacío)."""
        return self._hash_ultimo

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #
    def emitir(
        self,
        etapa: EtapaAuditoria,
        trace_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str | None = None,
        cuenta_ref: str | None = None,
        acting_on_behalf_of: str | None = None,
        nivel: NivelAseguramiento | None = None,
        ts: datetime | None = None,
    ) -> EventoAuditoria:
        """Escribe un evento al final de la cadena y lo devuelve ya sellado.

        Args:
            etapa: etapa del pipeline.
            trace_id: identificador del turno (agrupa todos sus eventos).
            payload: contenido de la etapa. Se sanea: los valores no serializables se
                convierten y las claves de :data:`CLAVES_SENSIBLES` se redactan.
            actor: componente que origina el evento; por defecto el del registro.
            cuenta_ref: referencia tokenizada de la cuenta. **Jamás DNI ni teléfono.**
            acting_on_behalf_of: obligatorio cuando ``nivel`` es ``LOA_ASESOR``.
            nivel: nivel de aseguramiento con el que se atendió la petición.
            ts: marca de tiempo; por defecto, ahora en UTC.

        Returns:
            El evento escrito, con ``indice``, ``hash_previo`` y ``hash`` ya fijados.

        Raises:
            ValueError: si falta una clave comprometida por la especificación
                (:data:`CLAVES_OBLIGATORIAS`) o si un evento de nivel ``LOA_ASESOR`` no
                declara en nombre de quién actúa el asesor.
        """
        limpio = sanear_payload(payload)
        faltantes = [
            clave for clave in CLAVES_OBLIGATORIAS.get(etapa, ()) if clave not in limpio
        ]
        if faltantes:
            raise ValueError(
                f"el evento {etapa} debe incluir en su payload: {', '.join(faltantes)}"
            )
        if nivel is NivelAseguramiento.LOA_ASESOR and not acting_on_behalf_of:
            raise ValueError(
                "un evento de nivel LOA_ASESOR debe registrar acting_on_behalf_of"
            )

        with self._cerrojo:
            self._sincronizar_con_fichero()
            evento = EventoAuditoria.encadenar(
                indice=self._indice_siguiente,
                trace_id=trace_id,
                etapa=etapa,
                payload=limpio,
                hash_previo=self._hash_ultimo,
                actor=actor or self.actor,
                cuenta_ref=cuenta_ref,
                acting_on_behalf_of=acting_on_behalf_of,
                nivel=nivel,
                ts=ts,
            )
            self._escribir(evento)
            self._indice_siguiente = evento.indice + 1
            self._hash_ultimo = evento.hash
        return evento

    def _escribir(self, evento: EventoAuditoria) -> None:
        """Añade la línea JSONL al fichero y la sincroniza a disco."""
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        with self.ruta.open("a", encoding="utf-8", newline="\n") as fichero:
            fichero.write(evento.a_linea_jsonl() + "\n")
            fichero.flush()
            if self.sincronizar:
                os.fsync(fichero.fileno())
        self._tamano_visto = self.ruta.stat().st_size

    def cerrar_turno(
        self,
        trace_id: str,
        *,
        cuenta_ref: str | None = None,
        verificar: bool = True,
    ) -> EventoAuditoria:
        """Emite el evento ``CHAIN`` que cierra el turno con el estado de la cadena.

        Args:
            trace_id: turno que se cierra.
            cuenta_ref: referencia tokenizada, si procede.
            verificar: recalcula la cadena completa antes de cerrar. Es lo que hace
                honesto el ``cadena_valida`` del payload.

        Returns:
            El evento ``CHAIN`` escrito.
        """
        eventos = self.leer(trace_id)
        valida, indice_roto = verificar_cadena(self.ruta) if verificar else (True, None)
        return self.emitir(
            EtapaAuditoria.CHAIN,
            trace_id,
            {
                "eventos": len(eventos) + 1,
                "hash_final": self._hash_ultimo,
                "cadena_valida": valida,
                "indice_roto": indice_roto,
            },
            cuenta_ref=cuenta_ref,
        )

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #
    def leer(
        self,
        trace_id: str | None = None,
        *,
        etapas: Iterable[EtapaAuditoria] | None = None,
        limite: int | None = None,
    ) -> list[EventoAuditoria]:
        """Devuelve los eventos del registro, filtrados por turno si se indica."""
        return leer_eventos(self.ruta, trace_id, etapas=etapas, limite=limite)

    def verificar_cadena(self, ruta: str | Path | None = None) -> tuple[bool, int | None]:
        """Verifica la cadena del fichero indicado (por defecto, el propio registro)."""
        return verificar_cadena(ruta if ruta is not None else self.ruta)

    def resumen(self, trace_id: str) -> ResumenTurno:
        """Construye el resumen de un turno para la API de auditoría."""
        eventos = self.leer(trace_id)
        valida, indice_roto = self.verificar_cadena()
        return resumir_turno(eventos, trace_id, cadena_valida=valida, indice_roto=indice_roto)

    def trazas(self) -> list[str]:
        """``trace_id`` presentes en el registro, en orden de primera aparición."""
        vistos: dict[str, None] = {}
        for evento in self.leer():
            vistos.setdefault(evento.trace_id, None)
        return list(vistos)


_REGISTRO_POR_DEFECTO: RegistroAuditoria | None = None
_CERROJO_SINGLETON = threading.Lock()


def registro_por_defecto(ruta: str | Path | None = None) -> RegistroAuditoria:
    """Devuelve el registro compartido del proceso, creándolo la primera vez.

    Pasar ``ruta`` fuerza la recreación del singleton apuntando a otro fichero (útil en
    pruebas y en el generador de datos).
    """
    global _REGISTRO_POR_DEFECTO
    with _CERROJO_SINGLETON:
        if _REGISTRO_POR_DEFECTO is None or ruta is not None:
            _REGISTRO_POR_DEFECTO = RegistroAuditoria(ruta, actor="api")
        return _REGISTRO_POR_DEFECTO


def resumir_turno(
    eventos: Sequence[EventoAuditoria],
    trace_id: str,
    *,
    cadena_valida: bool = True,
    indice_roto: int | None = None,
) -> ResumenTurno:
    """Agrega los eventos de un turno en un :class:`ResumenTurno`."""
    del_turno = [evento for evento in eventos if evento.trace_id == trace_id]
    agrupados = _por_etapa(del_turno)
    verificaciones = agrupados.get(EtapaAuditoria.VERIFY, [])
    respuestas = agrupados.get(EtapaAuditoria.RESPONSE, [])
    hechos = agrupados.get(EtapaAuditoria.FACTS_BUILT, [])
    rutas = agrupados.get(EtapaAuditoria.ROUTE, [])
    cuenta = next((evento.cuenta_ref for evento in del_turno if evento.cuenta_ref), None)
    return ResumenTurno(
        trace_id=trace_id,
        eventos=len(del_turno),
        etapas=[evento.etapa for evento in del_turno],
        cuenta_ref=cuenta,
        aserciones=contar_aserciones(del_turno),
        veredicto=(
            str(verificaciones[-1].payload.get("veredicto")) if verificaciones else None
        ),
        modo=str(respuestas[-1].payload.get("modo")) if respuestas else None,
        derivada=bool(
            (respuestas[-1].payload.get("derivada") if respuestas else False)
            or (rutas[-1].payload.get("derivar") if rutas else False)
        ),
        residual_cent=(
            _entero_opcional(hechos[-1].payload.get("residual_cent")) if hechos else None
        ),
        latencia_ms=(
            _entero_opcional(respuestas[-1].payload.get("latencia_ms")) if respuestas else None
        ),
        cadena_valida=cadena_valida,
        indice_roto=indice_roto,
        hash_final=del_turno[-1].hash if del_turno else None,
    )


# --------------------------------------------------------------------------- #
# Vista de terminal — activo de la demo
# --------------------------------------------------------------------------- #
_RESET = "\x1b[0m"
_NEGRITA = "\x1b[1m"
_TENUE = "\x1b[2m"
_VERDE = "\x1b[32m"
_ROJO = "\x1b[31m"
_AMBAR = "\x1b[33m"
_CIAN = "\x1b[36m"
_GRIS = "\x1b[90m"
_INV_VERDE = "\x1b[1;30;42m"
_INV_ROJO = "\x1b[1;97;41m"

_MARCAS_UNICODE = {"ok": "✔", "alerta": "▲", "error": "✖", "ausente": "·"}
_MARCAS_ASCII = {"ok": "+", "alerta": "!", "error": "x", "ausente": "."}
_MARCO_UNICODE = ("╭", "─", "╮", "│", "╰", "╯")
_MARCO_ASCII = ("+", "-", "+", "|", "+", "+")


def _quiere_color(color: bool | None) -> bool:
    """Decide si se emiten secuencias ANSI (respeta ``NO_COLOR`` y las tuberías)."""
    if color is not None:
        return color
    if os.getenv("NO_COLOR") is not None:
        return False
    if os.getenv("TERM", "") == "dumb":
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _soporta_marco_unicode(explicito: bool | None) -> bool:
    """Comprueba que la consola pueda representar los caracteres de marco."""
    if explicito is not None:
        return explicito
    codificacion = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "╭✔·Δ".encode(codificacion)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _pintar(texto: str, *codigos: str, color: bool) -> str:
    """Envuelve el texto en secuencias ANSI si el color está activo."""
    if not color or not codigos:
        return texto
    return "".join(codigos) + texto + _RESET


def _recortar(texto: str, ancho: int) -> str:
    """Recorta a ``ancho`` columnas añadiendo puntos suspensivos si sobra."""
    if ancho <= 1 or len(texto) <= ancho:
        return texto
    return texto[: ancho - 1].rstrip() + "…"


def _monto(valor: Any) -> str:
    """Formatea un importe en céntimos con signo explícito, o ``"—"`` si no lo hay."""
    centimos = _entero_opcional(valor)
    if centimos is None:
        return "—"
    signo = "+" if centimos > 0 else ""
    return f"{signo}{formatear_soles(centimos)}"


def _cuenta(valor: Any) -> int:
    """Cuenta elementos si el valor es una colección; si es un número, lo devuelve."""
    if isinstance(valor, list | tuple | set | dict):
        return len(valor)
    return _entero(valor, 0)


def _plural(cantidad: int, singular: str, plural: str) -> str:
    """``2 líneas`` / ``1 línea``: la vista de la demo se lee, no se tolera."""
    return f"{cantidad} {singular if cantidad == 1 else plural}"


def _linea_peticion(grupo: dict[EtapaAuditoria, list[EventoAuditoria]]) -> tuple[str, str]:
    """Fila ``PETICIÓN``: quién pregunta, por qué recibo y con qué nivel."""
    eventos = grupo.get(EtapaAuditoria.REQUEST)
    if not eventos:
        return "ausente", "sin evento REQUEST"
    evento = eventos[-1]
    payload = evento.payload
    partes = [
        str(evento.cuenta_ref or payload.get("cuenta_ref") or "cuenta n/d"),
        str(payload.get("periodo") or "periodo n/d"),
        str(payload.get("canal") or "APP"),
        str(evento.nivel or payload.get("nivel") or "LOA?"),
    ]
    if evento.acting_on_behalf_of:
        partes.append(f"asesor→{evento.acting_on_behalf_of}")
    return "ok", " · ".join(partes)


def _linea_hechos(grupo: dict[EtapaAuditoria, list[EventoAuditoria]]) -> tuple[str, str]:
    """Fila ``HECHOS``: el delta, cuántas líneas lo componen y si concilia."""
    hechos = grupo.get(EtapaAuditoria.FACTS_BUILT)
    invariantes = grupo.get(EtapaAuditoria.INVARIANTE)
    if not hechos and not invariantes:
        return "ausente", "sin FactSet"
    payload = hechos[-1].payload if hechos else {}
    payload_inv = invariantes[-1].payload if invariantes else {}
    residual = _entero_opcional(payload_inv.get("residual_cent"))
    if residual is None:
        residual = _entero_opcional(payload.get("residual_cent")) or 0
    ok = payload_inv.get("ok")
    concilia = bool(ok) if ok is not None else abs(residual) <= 1
    partes = [
        f"Δ {_monto(payload.get('delta_total_cent'))}",
        _plural(_cuenta(payload.get("lineas")), "línea", "líneas"),
        f"residual {residual} c",
        "invariante OK" if concilia else "INVARIANTE ROTO",
    ]
    return ("ok" if concilia else "error"), " · ".join(partes)


def _linea_contexto(grupo: dict[EtapaAuditoria, list[EventoAuditoria]]) -> tuple[str, str]:
    """Fila ``CONTEXTO``: qué se recuperó y hacia dónde se enrutó el turno."""
    recuperaciones = grupo.get(EtapaAuditoria.RETRIEVE)
    rutas = grupo.get(EtapaAuditoria.ROUTE)
    if not recuperaciones and not rutas:
        return "ausente", "sin recuperación"
    partes: list[str] = []
    if recuperaciones:
        payload = recuperaciones[-1].payload
        partes.append(_plural(_cuenta(payload.get("faq")), "faq", "faq"))
        partes.append(_plural(_cuenta(payload.get("casuistica")), "casuística", "casuísticas"))
        partes.append("saneado" if payload.get("saneado", True) else "SIN SANEAR")
    estado = "ok"
    if rutas:
        payload = rutas[-1].payload
        if payload.get("derivar"):
            estado = "alerta"
            motivo = payload.get("motivo_codigo") or "UMBRAL_INCOMPRENSION"
            partes.append(f"derivación: {motivo}")
        else:
            score = payload.get("score_incomprension")
            partes.append(f"U={float(score):.2f}" if isinstance(score, int | float) else "explica")
        if not recuperaciones or not recuperaciones[-1].payload.get("saneado", True):
            estado = "alerta"
    return estado, " · ".join(partes)


def _linea_generacion(grupo: dict[EtapaAuditoria, list[EventoAuditoria]]) -> tuple[str, str]:
    """Fila ``GENERACIÓN``: proveedor, modelo, latencia e intentos."""
    llamadas = grupo.get(EtapaAuditoria.LLM_CALL)
    if not llamadas:
        return "ausente", "sin llamada al modelo"
    payload = llamadas[-1].payload
    intentos = max(len(llamadas), _entero(payload.get("intento"), 1))
    partes = [
        str(payload.get("proveedor") or "n/d"),
        str(payload.get("model_version") or payload.get("modelo") or "sin versión"),
        f"{_entero(payload.get('latencia_ms'), 0)} ms",
    ]
    estado = "ok"
    if payload.get("timeout"):
        estado = "alerta"
        partes.append("TIMEOUT → plantilla")
    elif intentos > 1:
        estado = "alerta"
        partes.append(f"{intentos} intentos")
    return estado, " · ".join(partes)


def _linea_verifica(
    grupo: dict[EtapaAuditoria, list[EventoAuditoria]], contador: ContadorAserciones
) -> tuple[str, str]:
    """Fila ``VERIFICA``: veredicto del verificador numérico y citas emitidas."""
    verificaciones = grupo.get(EtapaAuditoria.VERIFY)
    citas = grupo.get(EtapaAuditoria.CITATIONS)
    if not verificaciones:
        return "ausente", "sin verificación numérica"
    veredicto = str(verificaciones[-1].payload.get("veredicto") or "?")
    partes = [
        veredicto,
        _plural(contador.ancladas, "anclada", "ancladas"),
        _plural(contador.derivadas, "derivada", "derivadas"),
        _plural(contador.no_ancladas, "no anclada", "no ancladas"),
    ]
    if citas:
        partes.append(_plural(_cuenta(citas[-1].payload.get("citas")), "cita", "citas"))
    estado = "ok" if veredicto == "PASS" and contador.limpio else "error"
    return estado, " · ".join(partes)


def _linea_respuesta(
    grupo: dict[EtapaAuditoria, list[EventoAuditoria]], total_eventos: int
) -> tuple[str, str]:
    """Fila ``RESPUESTA``: forma de la respuesta y estado de la cadena de hashes."""
    respuestas = grupo.get(EtapaAuditoria.RESPONSE)
    cadenas = grupo.get(EtapaAuditoria.CHAIN)
    if not respuestas and not cadenas:
        return "ausente", "turno sin cerrar"
    partes: list[str] = []
    estado = "ok"
    if respuestas:
        payload = respuestas[-1].payload
        partes.append(_plural(_cuenta(payload.get("bloques")), "bloque", "bloques"))
        partes.append(_plural(_cuenta(payload.get("acciones")), "acción", "acciones"))
        partes.append(str(payload.get("modo") or "n/d"))
        if payload.get("derivada"):
            estado = "alerta"
            partes.append("derivada a asesor")
        latencia = _entero_opcional(payload.get("latencia_ms"))
        if latencia is not None:
            partes.append(f"{latencia} ms")
    if cadenas:
        payload = cadenas[-1].payload
        valida = payload.get("cadena_valida", True)
        eventos = _entero(payload.get("eventos"), total_eventos)
        partes.append(
            f"cadena íntegra ({_plural(eventos, 'evento', 'eventos')})"
            if valida
            else f"CADENA ROTA en {payload.get('indice_roto')}"
        )
        if not valida:
            estado = "error"
    return estado, " · ".join(partes)


def formatear_para_terminal(
    eventos: Sequence[EventoAuditoria],
    trace_id: str,
    *,
    color: bool | None = None,
    ancho: int = 92,
    banner: bool = True,
    marco_unicode: bool | None = None,
) -> str:
    """Proyecta un turno en la terminal: cabecera destacada y **6 líneas como máximo**.

    Es la prueba que la ficha exige *"comprobable mediante logs de la terminal"*, y es
    lo que se ve en la demo, así que está pensada para leerse de un vistazo: una
    cabecera con el contador de aserciones (verde si ninguna quedó sin anclar, rojo si
    alguna lo hizo) y seis filas fijas, una por fase del pipeline. Las diez etapas se
    agrupan en esas seis filas (:data:`GRUPOS_TERMINAL`), de modo que el número de
    líneas de evento **no depende** de cuántos eventos haya: siempre son
    :data:`MAX_LINEAS_TURNO`.

    Args:
        eventos: eventos del registro (se filtran por ``trace_id``).
        trace_id: turno que se proyecta.
        color: fuerza o desactiva ANSI. Por defecto se autodetecta (respeta ``NO_COLOR``).
        ancho: columnas máximas por línea.
        banner: si es ``False``, la cabecera se reduce a una sola línea invertida.
        marco_unicode: fuerza o desactiva los caracteres de marco; por defecto se
            comprueba que la codificación de la consola los admita.

    Returns:
        El bloque listo para imprimir, sin salto de línea final.
    """
    usar_color = _quiere_color(color)
    rico = _soporta_marco_unicode(marco_unicode)
    marcas = _MARCAS_UNICODE if rico else _MARCAS_ASCII
    esquina_ai, horizontal, esquina_ad, vertical, esquina_bi, esquina_bd = (
        _MARCO_UNICODE if rico else _MARCO_ASCII
    )

    del_turno = [evento for evento in eventos if evento.trace_id == trace_id]
    contador = contar_aserciones(del_turno)
    grupo = _por_etapa(del_turno)

    # --- cabecera destacada (3 líneas: el título va incrustado en el marco) --- #
    titulo = _recortar(f"RECIBO CLARO · trace {trace_id}", max(20, ancho - 8))
    cabecera = contador.a_cabecera()
    estilo_cabecera = _INV_VERDE if contador.limpio else _INV_ROJO
    interior = min(
        max(len(titulo) + 4, len(cabecera) + 2, 44),
        max(24, ancho - 2),
    )
    cabecera = _recortar(cabecera, interior - 2)
    lineas: list[str] = []
    if banner:
        relleno = max(1, interior - 3 - len(titulo))
        superior = esquina_ai + horizontal + " " + titulo + " " + horizontal * relleno + esquina_ad
        inferior = esquina_bi + horizontal * interior + esquina_bd
        lineas.append(_pintar(superior, _NEGRITA, _CIAN, color=usar_color))
        lineas.append(
            _pintar(vertical, _NEGRITA, _CIAN, color=usar_color)
            + _pintar(f" {cabecera} ".ljust(interior), estilo_cabecera, color=usar_color)
            + _pintar(vertical, _NEGRITA, _CIAN, color=usar_color)
        )
        lineas.append(_pintar(inferior, _NEGRITA, _CIAN, color=usar_color))
    else:
        lineas.append(
            _pintar(f" {titulo} ", _NEGRITA, _CIAN, color=usar_color)
            + _pintar(f" {cabecera} ", estilo_cabecera, color=usar_color)
        )

    # --- seis filas de pipeline ------------------------------------------ #
    # Los rótulos salen de GRUPOS_TERMINAL para que no puedan divergir de la
    # agrupación documentada de etapas.
    constructores = (
        lambda: _linea_peticion(grupo),
        lambda: _linea_hechos(grupo),
        lambda: _linea_contexto(grupo),
        lambda: _linea_generacion(grupo),
        lambda: _linea_verifica(grupo, contador),
        lambda: _linea_respuesta(grupo, len(del_turno)),
    )
    filas: list[tuple[str, str, str]] = [
        (rotulo, *constructor())
        for (rotulo, _etapas), constructor in zip(GRUPOS_TERMINAL, constructores, strict=True)
    ]
    colores_estado = {
        "ok": (_VERDE,),
        "alerta": (_AMBAR,),
        "error": (_NEGRITA, _ROJO),
        "ausente": (_TENUE, _GRIS),
    }
    etiqueta_max = max(len(nombre) for nombre, _, _ in filas)
    for nombre, estado, detalle in filas[:MAX_LINEAS_TURNO]:
        marca = _pintar(marcas[estado], *colores_estado[estado], color=usar_color)
        rotulo = _pintar(nombre.ljust(etiqueta_max), _NEGRITA, color=usar_color)
        espacio = ancho - etiqueta_max - 6
        cuerpo = _recortar(detalle, max(20, espacio))
        if estado == "ausente":
            cuerpo = _pintar(cuerpo, _TENUE, _GRIS, color=usar_color)
        elif estado == "error":
            cuerpo = _pintar(cuerpo, _ROJO, color=usar_color)
        lineas.append(f"  {marca} {rotulo}  {cuerpo}")

    return "\n".join(lineas)
