"""ACL (*anti-corruption layer*) entre los sistemas de Movistar y el modelo canónico.

El motor de hechos, el verificador y la API **solo conocen el modelo canónico** de
``packages.core_domain``. Este módulo es la única frontera por la que entran datos de
BrainyBill (recibos) y de Amdocs (órdenes), y la única que sabe cómo se llaman sus
campos.

Piezas
------
* :class:`Transporte` — protocolo de acceso. Dos implementaciones:
  :class:`TransporteHTTP` (apunta al mock o al sistema real cambiando ``BASE_URL``) y
  :class:`TransporteArchivo` (lee el dataset sintético del disco, para arrancar la demo
  sin levantar los mocks). Ninguna de las dos aparece en los adaptadores: se inyecta.
* :class:`AdaptadorBrainyBill` — ``documento``/``lines`` de BrainyBill →
  :class:`~packages.core_domain.esquemas.recibo.Recibo`.
* :class:`AdaptadorAmdocs` — filas del export de órdenes →
  :class:`~packages.core_domain.esquemas.movimiento.MovementEvent`, delegando la tabla
  de columnas y de tipos en ``packages.datagen.mapping.movistar_map``.
* :class:`RepositorioCuentas` — fachada que junta ambos y entrega lo que el motor pide.

Interruptores documentados (no adivinados)
------------------------------------------
* :data:`IMPORTES_EN_CENTIMOS` — el JSON de BrainyBill del dataset trae céntimos
  enteros. Un BrainyBill real devuelve soles decimales: con el interruptor en ``False``
  los importes pasan por ``dinero.a_centimos`` y el resto del sistema no se entera.
* :data:`FIN_CICLO_INCLUSIVO_EN_ORIGEN` — el modelo canónico usa rangos ``[inicio, fin)``
  con ``fin`` exclusivo. Si el origen marca el último día incluido, se le suma un día
  aquí y solo aquí.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from packages.core_domain.dinero import Centimos, a_centimos
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.esquemas.recibo import LineaRecibo, Recibo
from packages.datagen.mapping.movistar_map import (
    COLUMNAS_ORDENES,
    a_movimiento,
    normalizar_orden,
)

__all__ = [
    "CAMPOS_CABECERA_BRAINYBILL",
    "CAMPOS_LINEA_BRAINYBILL",
    "FIN_CICLO_INCLUSIVO_EN_ORIGEN",
    "IMPORTES_EN_CENTIMOS",
    "AdaptadorAmdocs",
    "AdaptadorBrainyBill",
    "CuentaNoEncontradaExterna",
    "DatosCuenta",
    "ErrorSistemaExterno",
    "RepositorioCuentas",
    "Transporte",
    "TransporteArchivo",
    "TransporteHTTP",
    "crear_repositorio",
]

_LOG = logging.getLogger(__name__)

#: **[POR VALIDAR con Movistar]** El dataset entrega céntimos enteros; el BrainyBill
#: real devuelve soles decimales. Cambiar a ``False`` activa la conversión por
#: ``dinero.a_centimos`` y no hay que tocar nada más en el proyecto.
IMPORTES_EN_CENTIMOS = True

#: **[POR VALIDAR con Movistar]** ``True`` si el origen marca el fin de ciclo como el
#: último día incluido. El modelo canónico lo quiere exclusivo.
FIN_CICLO_INCLUSIVO_EN_ORIGEN = False

#: Cabecera de BrainyBill → campos de ``Recibo``. Solo estos nombres viajan del origen.
CAMPOS_CABECERA_BRAINYBILL: dict[str, str] = {
    "recibo_id": "recibo_id",
    "cuenta_id": "cuenta_id",
    "periodo": "periodo",
    "modalidad_renta": "modalidad_renta",
    "emision": "fecha_emision",
    "vencimiento": "fecha_vencimiento",
    "ciclo_inicio": "ciclo_inicio",
    "ciclo_fin": "ciclo_fin",
    "dias_ciclo": "dias_ciclo",
    "moneda": "moneda",
    "total_cent": "total_cent",
    "deuda_anterior_cent": "deuda_anterior_cent",
    "estado_servicio": "estado_servicio",
    "plan_vigente": "plan_vigente",
    "meta": "meta",
}

#: Línea de detalle de BrainyBill → campos de ``LineaRecibo``.
CAMPOS_LINEA_BRAINYBILL: dict[str, str] = {
    "linea_id": "linea_id",
    "concepto_id": "concepto_id",
    "nombre_comercial": "nombre_comercial",
    "familia": "familia",
    "descripcion": "descripcion",
    "monto_cent": "monto_cent",
    "periodo": "periodo",
    "servicio_id": "servicio_id",
    "cantidad": "cantidad",
    "afecto_igv": "afecto_igv",
    "dias_prorrateo": "dias_prorrateo",
    "fecha_inicio": "fecha_inicio",
    "fecha_fin": "fecha_fin",
    "cuota_numero": "cuota_numero",
    "cuotas_totales": "cuotas_totales",
    "movimiento_id": "movimiento_id",
    "tramos": "tramos",
    "meta": "meta",
}

#: Campos monetarios de la cabecera y de la línea (los únicos que cruzan ``a_centimos``).
_CAMPOS_IMPORTE = frozenset({"total_cent", "deuda_anterior_cent", "monto_cent"})

#: Campos de fecha simple.
_CAMPOS_FECHA = frozenset(
    {"fecha_emision", "fecha_vencimiento", "ciclo_inicio", "ciclo_fin", "fecha_inicio", "fecha_fin"}
)


# --------------------------------------------------------------------------- #
# Errores
# --------------------------------------------------------------------------- #
class ErrorSistemaExterno(RuntimeError):
    """Un sistema de Movistar no respondió o respondió algo que no se entiende."""

    def __init__(self, sistema: str, detalle: str) -> None:
        self.sistema = sistema
        self.detalle = detalle
        super().__init__(f"{sistema}: {detalle}")


class CuentaNoEncontradaExterna(ErrorSistemaExterno):
    """El sistema respondió correctamente y la cuenta no existe."""

    def __init__(self, sistema: str, cuenta_id: str) -> None:
        self.cuenta_id = cuenta_id
        super().__init__(sistema, f"la cuenta {cuenta_id} no existe")


# --------------------------------------------------------------------------- #
# Transporte
# --------------------------------------------------------------------------- #
@runtime_checkable
class Transporte(Protocol):
    """Acceso a un sistema externo. Lo implementa HTTP y lo implementa el disco."""

    nombre: str

    def obtener(self, ruta: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """Devuelve el cuerpo JSON de ``ruta``."""
        ...

    def cerrar(self) -> None:
        """Libera los recursos del transporte."""
        ...


class TransporteHTTP:
    """Cliente HTTP hacia el mock o hacia el sistema real.

    Se apunta a uno u otro cambiando ``BASE_URL``: ni los adaptadores ni el motor se
    enteran del cambio. Los errores de red y los estados 5xx se traducen a
    :class:`ErrorSistemaExterno`; el 404 a :class:`CuentaNoEncontradaExterna`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        nombre: str = "sistema-externo",
        timeout_s: float = 5.0,
        cliente: httpx.Client | None = None,
    ) -> None:
        self.nombre = nombre
        self.base_url = base_url.rstrip("/")
        self._propio = cliente is None
        self._cliente = cliente or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"Accept": "application/json", "User-Agent": "recibo-claro-acl/1.0"},
        )

    def obtener(self, ruta: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """GET a ``ruta`` devolviendo el JSON ya decodificado."""
        try:
            respuesta = self._cliente.get(ruta, params=dict(params or {}))
        except httpx.HTTPError as exc:
            raise ErrorSistemaExterno(self.nombre, f"no se pudo conectar: {exc}") from exc
        if respuesta.status_code == 404:
            raise CuentaNoEncontradaExterna(self.nombre, ruta.rsplit("/", 1)[-1])
        if respuesta.status_code >= 400:
            raise ErrorSistemaExterno(
                self.nombre, f"respondió {respuesta.status_code}: {respuesta.text[:200]}"
            )
        try:
            return respuesta.json()
        except ValueError as exc:
            raise ErrorSistemaExterno(self.nombre, f"respuesta no es JSON: {exc}") from exc

    def cerrar(self) -> None:
        """Cierra el cliente si lo creó este transporte."""
        if self._propio:
            self._cliente.close()


class TransporteArchivo:
    """Lee el dataset sintético del disco emulando el contrato de los mocks.

    Existe para que ``make demo`` funcione sin levantar dos servicios más y para que los
    tests no necesiten red. Responde exactamente a las mismas rutas que los mocks
    (``/bills/{cuenta}`` y ``/orders/{cuenta}``) y con el mismo cuerpo, de modo que
    cambiar de transporte no cambia una sola línea de los adaptadores.
    """

    def __init__(self, raiz: str | Path, *, nombre: str = "dataset-local") -> None:
        self.nombre = nombre
        self.raiz = Path(raiz)

    def obtener(self, ruta: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """Resuelve ``/bills/{cuenta}`` y ``/orders/{cuenta}`` contra el disco."""
        partes = [parte for parte in ruta.strip("/").split("/") if parte]
        if len(partes) != 2:
            raise ErrorSistemaExterno(self.nombre, f"ruta no soportada por el disco: {ruta}")
        recurso, cuenta_id = partes
        if recurso == "bills":
            return self._bills(cuenta_id, int((params or {}).get("cycles", 6)))
        if recurso == "orders":
            return self._orders(cuenta_id)
        raise ErrorSistemaExterno(self.nombre, f"recurso desconocido: {recurso}")

    def _bills(self, cuenta_id: str, ciclos: int) -> dict[str, Any]:
        """Documento de BrainyBill de una cuenta, recortado a ``ciclos`` recibos."""
        ruta = self.raiz / "bills" / f"{cuenta_id}.json"
        if not ruta.is_file():
            raise CuentaNoEncontradaExterna(self.nombre, cuenta_id)
        documento = json.loads(ruta.read_text(encoding="utf-8"))
        recibos = documento.get("recibos", [])
        documento["recibos"] = recibos[: max(ciclos, 1)]
        documento["ciclos"] = len(documento["recibos"])
        return documento

    def _orders(self, cuenta_id: str) -> dict[str, Any]:
        """Órdenes de la cuenta con las columnas nativas del export de Amdocs."""
        ruta = self.raiz / "ordenes.csv"
        if not ruta.is_file():
            raise ErrorSistemaExterno(self.nombre, f"no existe {ruta}")
        with ruta.open(encoding="utf-8", newline="") as fichero:
            filas = [
                fila
                for fila in csv.DictReader(fichero)
                if str(fila.get("ACCOUNT_ID", "")).strip() == cuenta_id
            ]
        return {
            "cuenta_id": cuenta_id,
            "formato": "amdocs",
            "total": len(filas),
            "orders": filas,
        }

    def cerrar(self) -> None:
        """El disco no mantiene recursos abiertos."""
        return None


# --------------------------------------------------------------------------- #
# Conversión de valores del origen
# --------------------------------------------------------------------------- #
def _a_fecha(valor: Any) -> date | None:
    """Convierte una fecha del origen (ISO o ``DD/MM/YYYY``) a ``date``."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ErrorSistemaExterno("BrainyBill", f"fecha no reconocible: {valor!r}")


def _importe(valor: Any) -> Centimos:
    """Importe del origen a céntimos enteros, según :data:`IMPORTES_EN_CENTIMOS`."""
    if valor is None or valor == "":
        return 0
    if IMPORTES_EN_CENTIMOS:
        return int(valor)
    return a_centimos(valor if isinstance(valor, str) else str(valor))


# --------------------------------------------------------------------------- #
# BrainyBill
# --------------------------------------------------------------------------- #
class AdaptadorBrainyBill:
    """Traduce lo que expone BrainyBill al modelo canónico.

    Contrato del origen (sección 3 de la especificación y ficha del desafío:
    *"BrainyBill expone la información de la factura actual y de los CINCO recibos
    previos"*)::

        GET /bills/{cuenta_id}?cycles=6
        {
          "cuenta_id": "C-DEMO-01", "modalidad_renta": "ADELANTADA",
          "segmento": "PREMIUM", "dia_ciclo": 1, "moneda": "PEN",
          "beneficios_vigentes": ["..."],
          "recibos": [ {"header": {...}, "lines": [...]}, ... ]   # más reciente primero
        }
    """

    sistema = "BrainyBill"

    def __init__(self, transporte: Transporte, *, ciclos_por_defecto: int = 6) -> None:
        self.transporte = transporte
        self.ciclos_por_defecto = ciclos_por_defecto

    # -- acceso ------------------------------------------------------------- #
    def documento(self, cuenta_id: str, ciclos: int | None = None) -> dict[str, Any]:
        """Documento crudo de la cuenta, tal cual lo devuelve el sistema."""
        crudo = self.transporte.obtener(
            f"/bills/{cuenta_id}",
            params={"cycles": ciclos or self.ciclos_por_defecto},
        )
        if not isinstance(crudo, Mapping):
            raise ErrorSistemaExterno(self.sistema, "el documento de la cuenta no es un objeto")
        if not crudo.get("recibos"):
            raise CuentaNoEncontradaExterna(self.sistema, cuenta_id)
        return dict(crudo)

    def recibos(self, cuenta_id: str, ciclos: int | None = None) -> list[Recibo]:
        """Recibos canónicos de la cuenta, del más reciente al más antiguo."""
        documento = self.documento(cuenta_id, ciclos)
        return self.recibos_de_documento(documento)

    def recibos_de_documento(self, documento: Mapping[str, Any]) -> list[Recibo]:
        """Convierte el documento completo, saltándose los recibos ilegibles.

        Un recibo que no cuadra (``Σ líneas ≠ total``) hace fallar la validación de
        ``Recibo``. No se corrige aquí: se descarta con un aviso y, si el descartado es
        el que se pedía explicar, el motor no tendrá con qué comparar y la API derivará.
        Falsear un total en la frontera es exactamente lo que este proyecto no hace.
        """
        recibos: list[Recibo] = []
        for bruto in documento.get("recibos", []):
            try:
                recibos.append(self.a_recibo(bruto, documento))
            except (ValueError, TypeError, KeyError) as error:
                cabecera = bruto.get("header", {}) if isinstance(bruto, Mapping) else {}
                _LOG.warning(
                    "recibo descartado en la ingesta (%s, periodo %s): %s",
                    cabecera.get("recibo_id", "?"),
                    cabecera.get("periodo", "?"),
                    error,
                )
        recibos.sort(key=lambda recibo: recibo.periodo, reverse=True)
        return recibos

    # -- traducción --------------------------------------------------------- #
    def a_recibo(
        self, bruto: Mapping[str, Any], documento: Mapping[str, Any] | None = None
    ) -> Recibo:
        """Traduce ``{"header": ..., "lines": [...]}`` a un :class:`Recibo` canónico."""
        cabecera = bruto.get("header")
        if not isinstance(cabecera, Mapping):
            raise ValueError("el recibo no trae 'header'")
        datos: dict[str, Any] = {}
        for origen, destino in CAMPOS_CABECERA_BRAINYBILL.items():
            if origen not in cabecera:
                continue
            valor = cabecera[origen]
            if destino in _CAMPOS_IMPORTE:
                datos[destino] = _importe(valor)
            elif destino in _CAMPOS_FECHA:
                datos[destino] = _a_fecha(valor)
            else:
                datos[destino] = valor

        if FIN_CICLO_INCLUSIVO_EN_ORIGEN and datos.get("ciclo_fin"):
            datos["ciclo_fin"] = datos["ciclo_fin"] + timedelta(days=1)
        if datos.get("ciclo_inicio") and datos.get("ciclo_fin"):
            # El origen puede no traer `dias_ciclo`, o traerlo con otra convención:
            # manda el rango, que es lo que valida el modelo canónico.
            datos["dias_ciclo"] = (datos["ciclo_fin"] - datos["ciclo_inicio"]).days
        datos.setdefault("moneda", str(documento.get("moneda", "PEN")) if documento else "PEN")
        meta = dict(datos.get("meta") or {})
        if documento and documento.get("beneficios_vigentes"):
            # El efecto efervescente necesita saber qué beneficios YA tiene el cliente.
            meta.setdefault("beneficios_vigentes", list(documento["beneficios_vigentes"]))
        if documento and documento.get("segmento"):
            meta.setdefault("segmento", documento["segmento"])
        datos["meta"] = meta
        datos["lineas"] = [self.a_linea(linea) for linea in bruto.get("lines", [])]

        declarado = cabecera.get("total_a_pagar_cent")
        recibo = Recibo.model_validate(datos)
        if declarado is not None and _importe(declarado) != recibo.total_a_pagar_cent:
            _LOG.warning(
                "el total a pagar declarado por %s (%s) no coincide con el calculado (%s)",
                self.sistema,
                _importe(declarado),
                recibo.total_a_pagar_cent,
            )
        return recibo

    def a_linea(self, bruto: Mapping[str, Any]) -> LineaRecibo:
        """Traduce una línea de detalle de BrainyBill a :class:`LineaRecibo`."""
        datos: dict[str, Any] = {}
        for origen, destino in CAMPOS_LINEA_BRAINYBILL.items():
            if origen not in bruto:
                continue
            valor = bruto[origen]
            if destino in _CAMPOS_IMPORTE:
                datos[destino] = _importe(valor)
            elif destino in _CAMPOS_FECHA:
                datos[destino] = _a_fecha(valor)
            else:
                datos[destino] = valor
        if FIN_CICLO_INCLUSIVO_EN_ORIGEN and datos.get("fecha_fin"):
            datos["fecha_fin"] = datos["fecha_fin"] + timedelta(days=1)
        datos["tramos"] = [dict(tramo) for tramo in (datos.get("tramos") or [])]
        datos["meta"] = dict(datos.get("meta") or {})
        return LineaRecibo.model_validate(datos)


# --------------------------------------------------------------------------- #
# Amdocs
# --------------------------------------------------------------------------- #
class AdaptadorAmdocs:
    """Traduce el historial de órdenes de Amdocs (CRM) a ``MovementEvent``.

    La tabla de columnas (``ORDER_ID``, ``ORDER_TYPE``…) y la de tipos de orden viven en
    ``packages.datagen.mapping.movistar_map``, no aquí. Este adaptador solo decide de
    dónde vienen las filas y qué hacer con las que no se entienden.

    El dataset del desafío **no trae órdenes de CRM**: son 297 002 líneas de cargo, sin
    historial de movimientos. Así que este adaptador sigue leyendo el export de Amdocs, y
    cuando no hay ninguno la atribución causal se apoya solo en las reglas de concepto y
    lo declara con menos confianza. Es lo correcto: inventar una orden sería peor.

    Contrato del origen::

        GET /orders/{cuenta_id}?formato=amdocs
        {"cuenta_id": "...", "formato": "amdocs", "total": 2,
         "orders": [{"ORDER_ID": "...", "ACCOUNT_ID": "...", ...}]}
    """

    sistema = "Amdocs"

    def __init__(self, transporte: Transporte, *, estricto: bool = False) -> None:
        self.transporte = transporte
        self.estricto = estricto

    def movimientos(self, cuenta_id: str) -> list[MovementEvent]:
        """Movimientos canónicos de la cuenta, ordenados cronológicamente.

        Una orden con un tipo no mapeado no se inventa: se descarta con un aviso (o se
        propaga, si ``estricto``). Que falte un movimiento hace bajar la confianza de la
        atribución y, si el impacto es grande, dispara la derivación; que se invente uno
        produciría una explicación falsa.
        """
        try:
            crudo = self.transporte.obtener(f"/orders/{cuenta_id}", params={"formato": "amdocs"})
        except CuentaNoEncontradaExterna:
            # Una cuenta sin órdenes es normal (cliente estable): no es un error.
            _LOG.info("Amdocs no tiene órdenes para %s", cuenta_id)
            return []
        return self.a_movimientos(crudo, cuenta_id)

    def a_movimientos(self, crudo: Any, cuenta_id: str) -> list[MovementEvent]:
        """Convierte el cuerpo devuelto por Amdocs, en cualquiera de sus dos formatos."""
        filas: Iterable[Mapping[str, Any]]
        if isinstance(crudo, Mapping):
            if crudo.get("movimientos") is not None and crudo.get("orders") is None:
                # El origen ya entregó el modelo canónico (formato=canonico).
                return sorted(
                    (MovementEvent.model_validate(fila) for fila in crudo["movimientos"]),
                    key=lambda evento: (evento.ocurrido_en, evento.movimiento_id),
                )
            filas = crudo.get("orders") or []
        elif isinstance(crudo, list):
            filas = crudo
        else:
            raise ErrorSistemaExterno(self.sistema, "el historial de órdenes no es una lista")

        movimientos: list[MovementEvent] = []
        for fila in filas:
            if str(fila.get("ACCOUNT_ID", cuenta_id)).strip() != cuenta_id:
                continue
            try:
                movimientos.append(a_movimiento(fila))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                if self.estricto:
                    raise ErrorSistemaExterno(self.sistema, str(error)) from error
                _LOG.warning("orden descartada (%s): %s", fila.get("ORDER_ID", "?"), error)
        movimientos.sort(key=lambda evento: (evento.ocurrido_en, evento.movimiento_id))
        return movimientos

    @staticmethod
    def filas_canonicas(filas: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Normaliza filas del export sin construir modelos (diagnóstico del ACL)."""
        return [normalizar_orden(fila) for fila in filas]


# --------------------------------------------------------------------------- #
# Fachada
# --------------------------------------------------------------------------- #
class DatosCuenta:
    """Todo lo que el motor necesita de una cuenta para un periodo dado."""

    __slots__ = (
        "beneficios",
        "cuenta_id",
        "documento",
        "movimientos",
        "periodo",
        "previos",
        "recibo",
    )

    def __init__(
        self,
        cuenta_id: str,
        periodo: str,
        recibo: Recibo,
        previos: list[Recibo],
        movimientos: list[MovementEvent],
        beneficios: list[str],
        documento: Mapping[str, Any],
    ) -> None:
        self.cuenta_id = cuenta_id
        self.periodo = periodo
        self.recibo = recibo
        self.previos = previos
        self.movimientos = movimientos
        self.beneficios = beneficios
        self.documento = documento

    @property
    def periodos_disponibles(self) -> list[str]:
        """Periodos que devolvió BrainyBill, del más reciente al más antiguo."""
        return [self.recibo.periodo, *[previo.periodo for previo in self.previos]]


class RepositorioCuentas:
    """Punto único desde el que la API pide datos de cuenta ya canónicos."""

    def __init__(
        self,
        brainybill: AdaptadorBrainyBill,
        amdocs: AdaptadorAmdocs,
        *,
        ciclos: int = 6,
    ) -> None:
        self.brainybill = brainybill
        self.amdocs = amdocs
        self.ciclos = ciclos

    def cargar(self, cuenta_id: str, periodo: str | None = None) -> DatosCuenta:
        """Carga recibos y órdenes de la cuenta y elige el periodo a explicar.

        Args:
            cuenta_id: cuenta ya autorizada (sale del token, nunca del cuerpo).
            periodo: ``YYYY-MM``; si se omite, el más reciente que exponga BrainyBill.

        Raises:
            CuentaNoEncontradaExterna: si BrainyBill no conoce la cuenta o el periodo.
            ErrorSistemaExterno: si algún sistema no responde.
        """
        documento = self.brainybill.documento(cuenta_id, self.ciclos)
        recibos = self.brainybill.recibos_de_documento(documento)
        if not recibos:
            raise CuentaNoEncontradaExterna(self.brainybill.sistema, cuenta_id)

        objetivo = periodo.strip() if periodo else recibos[0].periodo
        actual = next((recibo for recibo in recibos if recibo.periodo == objetivo), None)
        if actual is None:
            raise CuentaNoEncontradaExterna(self.brainybill.sistema, f"{cuenta_id}@{objetivo}")
        previos = [recibo for recibo in recibos if recibo.periodo < actual.periodo]
        beneficios = [str(item) for item in documento.get("beneficios_vigentes", [])]
        return DatosCuenta(
            cuenta_id=cuenta_id,
            periodo=actual.periodo,
            recibo=actual,
            previos=previos,
            movimientos=self.amdocs.movimientos(cuenta_id),
            beneficios=beneficios,
            documento=documento,
        )

    def cerrar(self) -> None:
        """Cierra los transportes de ambos sistemas."""
        self.brainybill.transporte.cerrar()
        if self.amdocs.transporte is not self.brainybill.transporte:
            self.amdocs.transporte.cerrar()


def crear_repositorio(
    *,
    brainybill_base_url: str = "",
    amdocs_base_url: str = "",
    raiz_datos: str | Path = "data/sintetico",
    timeout_s: float = 5.0,
    ciclos: int = 6,
) -> RepositorioCuentas:
    """Construye el repositorio eligiendo transporte por configuración.

    Con ``BASE_URL`` vacía se lee el dataset del disco; con ``BASE_URL`` puesta se habla
    con el mock o con el sistema real. Es el único sitio donde se decide, y se decide con
    una variable de entorno.
    """
    # Supabase manda sobre el disco cuando `ORIGEN_RECIBOS=supabase`. Se exige el valor
    # explícito y no basta con que exista `SUPABASE_DB_URL`: la conexión también la usan
    # el vocabulario y las FAQs, y tenerla configurada no debe cambiar en silencio de
    # dónde salen los recibos que se le explican a un cliente.
    if os.environ.get("ORIGEN_RECIBOS", "").strip().lower() == "supabase":
        from apps.api.transporte_supabase import TransporteSupabase

        transporte_bb: Transporte = TransporteSupabase()
    elif brainybill_base_url:
        transporte_bb = TransporteHTTP(
            brainybill_base_url, nombre="BrainyBill", timeout_s=timeout_s
        )
    else:
        transporte_bb = TransporteArchivo(raiz_datos, nombre="BrainyBill")

    if amdocs_base_url:
        transporte_am: Transporte = TransporteHTTP(
            amdocs_base_url, nombre="Amdocs", timeout_s=timeout_s
        )
    elif brainybill_base_url or isinstance(transporte_bb, TransporteArchivo) is False:
        # Amdocs nunca hereda el transporte de recibos salvo que ambos sean el mismo
        # disco. Con Supabase sirviendo los recibos, el de órdenes debe seguir siendo el
        # archivo: el dataset del desafío **no trae órdenes de CRM**, y pedirle `/orders`
        # a un transporte que solo sabe de `/bills` rompía la carga entera.
        transporte_am = TransporteArchivo(raiz_datos, nombre="Amdocs")
    else:
        transporte_am = transporte_bb

    _LOG.info(
        "ACL configurado: BrainyBill=%s · Amdocs=%s",
        brainybill_base_url or f"archivo:{raiz_datos}",
        amdocs_base_url or f"archivo:{raiz_datos}",
    )
    return RepositorioCuentas(
        AdaptadorBrainyBill(transporte_bb, ciclos_por_defecto=ciclos),
        AdaptadorAmdocs(transporte_am),
        ciclos=ciclos,
    )


#: Columnas que el mock de Amdocs publica (las nativas del export). Se reexporta para
#: que el servidor mock y el ACL no puedan desincronizarse.
COLUMNAS_ORDENES_AMDOCS: tuple[str, ...] = COLUMNAS_ORDENES
