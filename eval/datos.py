"""Carga del dataset sintético y de los casos golden.

La evaluación no vuelve a generar nada: lee lo que ``packages.datagen.generar`` dejó en
``data/sintetico/`` exactamente como lo leería la API en producción, es decir, cruzando
la **frontera del ACL**:

* ``bills/{cuenta}.json``  → forma BrainyBill (``header`` + ``lines``) → :class:`Recibo`
* ``ordenes.csv``          → forma Amdocs → :func:`movistar_map.a_movimiento`
* ``ground_truth.csv``     → :class:`GroundTruthCausaDelta`

Que la evaluación pase por el mismo ACL que la ingesta real es deliberado: si el día de
mañana el export de Movistar rompe el mapeo, la evaluación lo detecta igual que el
pipeline, y no queda un camino "de laboratorio" que funcione mientras el de verdad no.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from packages.core_domain.enums import ModalidadRenta
from packages.core_domain.esquemas.evaluacion import CasoGolden, GroundTruthCausaDelta
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.movimiento import MovementEvent
from packages.core_domain.esquemas.recibo import LineaRecibo, Recibo
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas
from packages.datagen.mapping.movistar_map import a_movimiento
from packages.facts_engine.motor import construir_factset

__all__ = [
    "RUTA_RELATIVA_DATASET",
    "RUTA_RELATIVA_GOLDEN",
    "VAR_ENTORNO_DATASET",
    "CuentaSintetica",
    "DatasetAusente",
    "cargar_cuenta",
    "cargar_golden",
    "cargar_ground_truth",
    "cargar_movimientos",
    "cuentas_disponibles",
    "factset_de_caso",
    "factset_de_cuenta",
    "ground_truth_de",
    "raiz_proyecto",
    "recibo_desde_documento",
    "ruta_dataset",
    "ruta_golden",
]

#: Ubicación por defecto del dataset sintético, relativa a la raíz del proyecto.
RUTA_RELATIVA_DATASET = Path("data/sintetico")

#: Ubicación por defecto de los casos golden.
RUTA_RELATIVA_GOLDEN = Path("eval/golden")

#: Permite apuntar la evaluación a otro dataset (por ejemplo, el real anonimizado).
VAR_ENTORNO_DATASET = "DATASET_PATH"


class DatasetAusente(RuntimeError):
    """El dataset sintético no está generado.

    No es un fallo del sistema: ``data/`` está en ``.gitignore`` por la cláusula de
    confidencialidad de diez años de las bases. Se resuelve con ``make seed``.
    """


def raiz_proyecto() -> Path:
    """Raíz del repositorio (dos niveles por encima de este archivo)."""
    return Path(__file__).resolve().parents[1]


def ruta_dataset(ruta: str | Path | None = None) -> Path:
    """Directorio del dataset: argumento → ``DATASET_PATH`` → ``data/sintetico``."""
    if ruta is not None:
        return Path(ruta)
    del_entorno = os.getenv(VAR_ENTORNO_DATASET)
    if del_entorno:
        return Path(del_entorno)
    return raiz_proyecto() / RUTA_RELATIVA_DATASET


def ruta_golden(ruta: str | Path | None = None) -> Path:
    """Directorio de los casos golden."""
    return Path(ruta) if ruta is not None else raiz_proyecto() / RUTA_RELATIVA_GOLDEN


# --------------------------------------------------------------------------- #
# ACL de entrada: documento BrainyBill -> Recibo
# --------------------------------------------------------------------------- #
def recibo_desde_documento(documento: Mapping[str, Any]) -> Recibo:
    """Reconstruye un :class:`Recibo` desde un documento ``{header, lines}``.

    Es la inversa exacta de ``datagen.generar.recibo_a_documento``. Se valida contra el
    modelo de dominio, de modo que un documento que no cuadre (Σlíneas ≠ total, días de
    ciclo inconsistentes) **revienta aquí**, en el borde, y no a mitad de la explicación.
    """
    cabecera = documento["header"]
    lineas = [
        LineaRecibo.model_validate(
            {
                "linea_id": linea["linea_id"],
                "concepto_id": linea["concepto_id"],
                "nombre_comercial": linea["nombre_comercial"],
                "familia": linea["familia"],
                "monto_cent": linea["monto_cent"],
                "periodo": linea["periodo"],
                "servicio_id": linea.get("servicio_id"),
                "descripcion": linea.get("descripcion"),
                "cantidad": linea.get("cantidad", 1),
                "afecto_igv": linea.get("afecto_igv", True),
                "dias_prorrateo": linea.get("dias_prorrateo"),
                "fecha_inicio": linea.get("fecha_inicio"),
                "fecha_fin": linea.get("fecha_fin"),
                "cuota_numero": linea.get("cuota_numero"),
                "cuotas_totales": linea.get("cuotas_totales"),
                "movimiento_id": linea.get("movimiento_id"),
                "tramos": linea.get("tramos", []),
                "meta": linea.get("meta", {}),
            }
        )
        for linea in documento["lines"]
    ]
    return Recibo.model_validate(
        {
            "recibo_id": cabecera["recibo_id"],
            "cuenta_id": cabecera["cuenta_id"],
            "periodo": cabecera["periodo"],
            "modalidad_renta": cabecera["modalidad_renta"],
            "ciclo_inicio": cabecera["ciclo_inicio"],
            "ciclo_fin": cabecera["ciclo_fin"],
            "dias_ciclo": cabecera["dias_ciclo"],
            "fecha_emision": cabecera["emision"],
            "fecha_vencimiento": cabecera["vencimiento"],
            "lineas": [linea.model_dump() for linea in lineas],
            "total_cent": cabecera["total_cent"],
            "deuda_anterior_cent": cabecera.get("deuda_anterior_cent", 0),
            "moneda": cabecera.get("moneda", "PEN"),
            "estado_servicio": cabecera.get("estado_servicio", "ACTIVO"),
            "plan_vigente": cabecera.get("plan_vigente"),
            "meta": cabecera.get("meta", {}),
        }
    )


# --------------------------------------------------------------------------- #
# Cuenta completa
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CuentaSintetica:
    """Todo lo que BrainyBill y Amdocs saben de una cuenta.

    Los recibos van del **más reciente al más antiguo**, igual que los expone BrainyBill.
    """

    cuenta_id: str
    modalidad_renta: ModalidadRenta
    segmento: str
    dia_ciclo: int
    escenarios: tuple[str, ...]
    beneficios_vigentes: tuple[str, ...]
    recibos: tuple[Recibo, ...]
    movimientos: tuple[MovementEvent, ...] = ()

    @property
    def recibo_actual(self) -> Recibo:
        """Recibo del periodo más reciente."""
        return self.recibos[0]

    @property
    def periodo_actual(self) -> str:
        """Periodo del recibo más reciente."""
        return self.recibo_actual.periodo

    def recibo(self, periodo: str) -> Recibo | None:
        """Recibo de un periodo concreto."""
        return next((recibo for recibo in self.recibos if recibo.periodo == periodo), None)

    def previos_de(self, periodo: str) -> list[Recibo]:
        """Recibos anteriores a un periodo dado."""
        return [recibo for recibo in self.recibos if recibo.periodo < periodo]


@lru_cache(maxsize=512)
def _documento_cuenta(cuenta_id: str, directorio: str) -> dict[str, Any]:
    """Lee y cachea el JSON de una cuenta."""
    archivo = Path(directorio) / "bills" / f"{cuenta_id}.json"
    if not archivo.is_file():
        raise DatasetAusente(
            f"no existe {archivo}. Genere el dataset con "
            "`python -m packages.datagen.generar --seed 20260804 --clientes 300`"
        )
    return json.loads(archivo.read_text(encoding="utf-8"))


def cargar_cuenta(cuenta_id: str, ruta: str | Path | None = None) -> CuentaSintetica:
    """Carga una cuenta completa: sus seis recibos y sus movimientos de Amdocs."""
    base = ruta_dataset(ruta)
    documento = _documento_cuenta(cuenta_id, str(base))
    recibos = tuple(
        sorted(
            (recibo_desde_documento(bloque) for bloque in documento["recibos"]),
            key=lambda recibo: recibo.periodo,
            reverse=True,
        )
    )
    if not recibos:
        raise DatasetAusente(f"la cuenta {cuenta_id} no tiene recibos")
    movimientos = tuple(cargar_movimientos(base).get(cuenta_id, ()))
    return CuentaSintetica(
        cuenta_id=documento["cuenta_id"],
        modalidad_renta=ModalidadRenta(documento["modalidad_renta"]),
        segmento=str(documento.get("segmento", "")),
        dia_ciclo=int(documento.get("dia_ciclo", 1)),
        escenarios=tuple(documento.get("escenarios_inyectados", [])),
        beneficios_vigentes=tuple(documento.get("beneficios_vigentes", [])),
        recibos=recibos,
        movimientos=movimientos,
    )


def cuentas_disponibles(ruta: str | Path | None = None) -> list[str]:
    """Identificadores de todas las cuentas del dataset, ordenados."""
    directorio = ruta_dataset(ruta) / "bills"
    if not directorio.is_dir():
        raise DatasetAusente(f"no existe {directorio}: falta generar el dataset")
    return sorted(archivo.stem for archivo in directorio.glob("*.json"))


# --------------------------------------------------------------------------- #
# Movimientos y ground truth
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=8)
def _movimientos_cacheados(directorio: str) -> dict[str, tuple[MovementEvent, ...]]:
    """Lee ``ordenes.csv`` a través del ACL y agrupa por cuenta."""
    archivo = Path(directorio) / "ordenes.csv"
    if not archivo.is_file():
        return {}
    por_cuenta: dict[str, list[MovementEvent]] = {}
    with archivo.open(encoding="utf-8", newline="") as flujo:
        for fila in csv.DictReader(flujo):
            movimiento = a_movimiento(fila)
            por_cuenta.setdefault(movimiento.cuenta_id, []).append(movimiento)
    return {
        cuenta: tuple(
            sorted(eventos, key=lambda evento: (evento.ocurrido_en, evento.movimiento_id))
        )
        for cuenta, eventos in por_cuenta.items()
    }


def cargar_movimientos(ruta: str | Path | None = None) -> dict[str, tuple[MovementEvent, ...]]:
    """Historial de órdenes de todas las cuentas, indexado por ``cuenta_id``."""
    return _movimientos_cacheados(str(ruta_dataset(ruta)))


@lru_cache(maxsize=8)
def _ground_truth_cacheado(
    directorio: str,
) -> dict[tuple[str, str], tuple[GroundTruthCausaDelta, ...]]:
    """Lee ``ground_truth.csv`` e indexa por ``(cuenta_id, periodo)``."""
    archivo = Path(directorio) / "ground_truth.csv"
    if not archivo.is_file():
        raise DatasetAusente(f"no existe {archivo}: falta generar el dataset")
    indice: dict[tuple[str, str], list[GroundTruthCausaDelta]] = {}
    with archivo.open(encoding="utf-8", newline="") as flujo:
        for fila in csv.DictReader(flujo):
            registro = GroundTruthCausaDelta(
                cuenta_id=fila["cuenta_id"],
                periodo=fila["periodo"],
                concepto_id=fila["concepto_id"],
                causa=fila["causa"] or None,
                delta_cent=int(fila["delta_cent"]),
                movimiento_id=int(fila["movimiento_id"]) if fila.get("movimiento_id") else None,
                escenario=fila.get("escenario") or None,
            )
            indice.setdefault((registro.cuenta_id, registro.periodo), []).append(registro)
    return {clave: tuple(valores) for clave, valores in indice.items()}


def cargar_ground_truth(
    ruta: str | Path | None = None,
) -> dict[tuple[str, str], tuple[GroundTruthCausaDelta, ...]]:
    """Ground truth completo, indexado por ``(cuenta_id, periodo)``."""
    return _ground_truth_cacheado(str(ruta_dataset(ruta)))


def ground_truth_de(
    cuenta_id: str, periodo: str, ruta: str | Path | None = None
) -> list[GroundTruthCausaDelta]:
    """Filas de ground truth de una cuenta y periodo."""
    return list(cargar_ground_truth(ruta).get((cuenta_id, periodo), ()))


# --------------------------------------------------------------------------- #
# FactSet
# --------------------------------------------------------------------------- #
def factset_de_cuenta(
    cuenta: CuentaSintetica,
    periodo: str | None = None,
    reglas: ConfiguracionReglas | None = None,
) -> FactSet:
    """Construye el FactSet de un periodo de la cuenta con el motor determinístico."""
    objetivo = periodo or cuenta.periodo_actual
    actual = cuenta.recibo(objetivo)
    if actual is None:
        raise DatasetAusente(f"la cuenta {cuenta.cuenta_id} no tiene recibo de {objetivo}")
    return construir_factset(
        actual,
        cuenta.previos_de(objetivo),
        cuenta.movimientos,
        reglas if reglas is not None else cargar_reglas(),
        beneficios_vigentes=list(cuenta.beneficios_vigentes),
    )


def factset_de_caso(
    caso: CasoGolden,
    ruta: str | Path | None = None,
    reglas: ConfiguracionReglas | None = None,
) -> FactSet:
    """Atajo para el test golden: ``caso -> FactSet`` en una línea."""
    cuenta = cargar_cuenta(caso.cuenta_id, ruta)
    return factset_de_cuenta(cuenta, caso.periodo, reglas)


# --------------------------------------------------------------------------- #
# Casos golden
# --------------------------------------------------------------------------- #
def cargar_golden(
    directorio: str | Path | None = None, *, solo: Sequence[str] | None = None
) -> list[CasoGolden]:
    """Carga y valida todos los ``*.yaml`` de ``eval/golden/``.

    Un archivo puede contener un caso (mapa) o varios (lista de mapas). Se validan
    contra :class:`CasoGolden`, que tiene ``extra="forbid"``: un campo mal escrito
    **falla ruidosamente** en vez de ignorarse en silencio.

    Args:
        directorio: dónde buscar; por defecto ``eval/golden``.
        solo: si se indica, se filtra por ``caso_id``.

    Returns:
        Los casos ordenados por ``caso_id``, que es como salen en la tabla y en los
        parámetros de pytest (orden estable ⇒ diffs legibles entre ejecuciones).

    Raises:
        DatasetAusente: si el directorio no existe o no hay ningún caso.
        ValueError: si un caso no valida o si hay ``caso_id`` duplicados.
    """
    base = ruta_golden(directorio)
    if not base.is_dir():
        raise DatasetAusente(f"no existe el directorio de casos golden: {base}")

    casos: list[CasoGolden] = []
    vistos: dict[str, Path] = {}
    for archivo in sorted(base.glob("*.yaml")) + sorted(base.glob("*.yml")):
        contenido = yaml.safe_load(archivo.read_text(encoding="utf-8"))
        if contenido is None:
            continue
        crudos = contenido if isinstance(contenido, list) else [contenido]
        for crudo in crudos:
            try:
                caso = CasoGolden.model_validate(crudo)
            except Exception as error:
                raise ValueError(f"caso golden inválido en {archivo.name}: {error}") from error
            if caso.caso_id in vistos:
                raise ValueError(
                    f"caso_id duplicado '{caso.caso_id}' en {archivo.name} "
                    f"y {vistos[caso.caso_id].name}"
                )
            vistos[caso.caso_id] = archivo
            casos.append(caso)

    if not casos:
        raise DatasetAusente(f"no hay casos golden en {base}")
    if solo:
        pedidos = set(solo)
        casos = [caso for caso in casos if caso.caso_id in pedidos]
    return sorted(casos, key=lambda caso: caso.caso_id)


def escenarios_de(casos: Iterable[CasoGolden]) -> dict[str, int]:
    """Recuento de casos por escenario (un caso compuesto cuenta en cada escenario)."""
    conteo: dict[str, int] = {}
    for caso in casos:
        for escenario in caso.escenarios or ["SIN_ESCENARIO"]:
            conteo[escenario] = conteo.get(escenario, 0) + 1
    return dict(sorted(conteo.items()))
