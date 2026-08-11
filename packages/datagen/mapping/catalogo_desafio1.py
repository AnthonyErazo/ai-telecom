"""Extrae el **catálogo de conceptos real** del dataset del Desafío 1.

Por qué existe este módulo
--------------------------
Hasta ahora el catálogo eran 31 conceptos **escritos por nosotros** en ``rules.yaml``.
Servían para que el motor funcionase, pero no eran los conceptos de Movistar: eran los
que nos imaginamos que Movistar usaría. La ficha del desafío pide otra cosa:

    *«Respuestas limitadas estrictamente a la base de datos de facturación provista»*
    *«Precisión de Recuperación: capacidad del modelo para extraer el dato exacto de la
    base proporcionada»*

El catálogo real **ya venía en el dataset** y no lo estábamos mirando: los 732
``CHARGE_CODE_ID`` con su ``CHARGE_CODE_DESC`` son el catálogo de conceptos, y esa
descripción es —literalmente— el texto que el cliente lee en su recibo. Cuando alguien
pregunta *«¿qué es este RV Plan Mi Movistar S/29.9?»*, está citando ese campo.

Confidencialidad
----------------
Este módulo **lee** el dataset desde su ruta original y **no lo copia al repositorio**.
La salida se escribe bajo ``data/``, que ``.gitignore`` excluye entero. Lo que se versiona
es el código que sabe extraer el catálogo, nunca el catálogo extraído.

Qué NO hace
-----------
No inventa definiciones de cliente. Un concepto extraído trae su nombre comercial real y
su clasificación real; la ``definicion_cliente`` queda **vacía** salvo que la familia
permita derivarla sin adivinar. Rellenarla con prosa inventada sería volver al problema
que este módulo viene a resolver, solo que con más apariencia de rigor.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.core_domain.enums import FamiliaConcepto

__all__ = [
    "FICHERO_CARGOS",
    "GRUPO_IGNORAR",
    "VAR_RUTA_DATASET",
    "ConceptoExtraido",
    "ResumenCatalogo",
    "escribir_catalogo",
    "extraer_catalogo",
    "familia_de",
    "leer_cargos",
    "ruta_dataset",
]

_LOG = logging.getLogger(__name__)

#: Variable de entorno con la carpeta del dataset entregado. Sin valor por defecto
#: dentro del repositorio: el dataset vive fuera, y esa separación es deliberada.
VAR_RUTA_DATASET = "DATASET_DESAFIO1"

#: Nombre del export de cargos tal y como se entregó.
FICHERO_CARGOS = "Cargos_FacturadosV2.csv"

#: El propio dataset marca qué filas no debe considerar el cálculo. Son los pares
#: `Bono Recurrente Cargo` / `Bono Recurrente Negativo`, que se anulan entre sí: 164 524
#: de 297 002 filas. Un motor que las sume duplica cargos, así que la bandera se respeta
#: aquí, en el borde, y no en cada consumidor.
GRUPO_IGNORAR = "NO CONSIDERAR"

#: Prefijo del código → familia, **solo** como último recurso cuando ni ``GRUPO`` ni
#: ``CHARGE_CODE_CLASSIFICATION`` dicen nada. Cada entrada está comprobada contra el
#: dataset; no se añade ninguna por lo que el prefijo parezca significar (ver el
#: docstring de :func:`familia_de` para el error que costó esa lección).
_FAMILIA_POR_PREFIJO: dict[str, FamiliaConcepto] = {
    "RC": FamiliaConcepto.RECURRENTE,
    "RC1": FamiliaConcepto.RECURRENTE,
    "RCD": FamiliaConcepto.AJUSTE,
    "RCD1": FamiliaConcepto.AJUSTE,
}

#: Marca inequívoca de renta vencida en la descripción comercial.
#:
#: Medido sobre los 102 241 cargos fijos del dataset: cuando la descripción empieza por
#: ``"RV "``, el ``GRUPO`` dice VENCIDO en el **100 %** de los casos (55 315 filas, cero
#: excepciones). Lo que **no** vale es la recíproca: de los cargos sin esa marca, 12 321
#: son igualmente vencidos. Es una implicación en un solo sentido, y tratarla como
#: equivalencia clasificaría mal uno de cada cuatro recibos.
_MARCA_RENTA_VENCIDA = re.compile(r"^RV\s", re.IGNORECASE)


def ruta_dataset() -> Path:
    """Carpeta del dataset entregado, leída de ``DATASET_DESAFIO1``.

    Raises:
        FileNotFoundError: si la variable no está definida o la carpeta no existe. Se
            falla pronto y con el nombre de la variable en el mensaje: un catálogo vacío
            por una ruta mal puesta es peor que un error.
    """
    bruto = os.getenv(VAR_RUTA_DATASET, "").strip()
    if not bruto:
        raise FileNotFoundError(
            f"defina {VAR_RUTA_DATASET} con la carpeta del dataset del Desafío 1 "
            "(el dataset vive fuera del repositorio por confidencialidad)"
        )
    carpeta = Path(bruto)
    if not carpeta.is_dir():
        raise FileNotFoundError(f"{VAR_RUTA_DATASET}={bruto!r} no es una carpeta existente")
    return carpeta


def leer_cargos(carpeta: Path | None = None) -> Iterator[dict[str, str]]:
    """Recorre el export de cargos fila a fila, en flujo.

    Se itera en vez de cargar en memoria porque el fichero ronda los 61 MB y 297 002
    filas: materializarlo entero para contar códigos distintos sería gasto sin motivo.

    Dos saneamientos que el fichero real exige y que no son opcionales:

    * el separador es ``;``, no coma;
    * hay cabeceras con **espacio final** (``"FECHA-VENCIMIENTO "``). Sin recortarlas,
      ``fila["FECHA-VENCIMIENTO"]`` devuelve ``None`` en silencio y el campo se pierde.
    """
    carpeta = carpeta or ruta_dataset()
    fichero = carpeta / FICHERO_CARGOS
    if not fichero.is_file():
        raise FileNotFoundError(f"no se encuentra {fichero}")
    with fichero.open(encoding="utf-8", errors="replace", newline="") as fh:
        lector = csv.DictReader(fh, delimiter=";")
        if lector.fieldnames:
            lector.fieldnames = [c.strip() for c in lector.fieldnames]
        for fila in lector:
            yield {k: (v or "").strip() for k, v in fila.items() if k}


def familia_de(codigo: str, grupo: str, clasificacion: str) -> FamiliaConcepto:
    """Familia contable de un concepto, **a partir de lo que el dataset afirma**.

    La clasificación sale del ``GRUPO`` y de ``CHARGE_CODE_CLASSIFICATION``, que son
    campos del facturador. El prefijo del código solo se consulta cuando esos dos no
    dicen nada, y nunca los contradice.

    Una regla que estuvo aquí y era falsa
    -------------------------------------
    La primera versión asumía que un código ``FR*`` era financiamiento de equipo. Suena
    razonable —«FR» de financiamiento— y es **mentira**: de los 221 códigos ``FR*`` del
    dataset, **ninguno** tiene un grupo de financiamiento; son descuentos (81), paquetes
    (51), cargos fijos (16) y reconexiones (7). La regla clasificaba mal «Movistar
    Internet» y «Bloque Full HD» como cuotas de equipo.

    El error no fue la heurística, sino su origen: se dedujo del nombre del prefijo en
    vez de observarlo en el dato. Por eso ahora el prefijo es el último recurso y solo
    para familias que el propio dataset confirma.
    """
    g, c = grupo.upper(), clasificacion.upper()
    if "DESCUENTO" in g or "DESCUENTO" in c or "BONIFICACION" in c or "GRATUIDAD" in c:
        return FamiliaConcepto.AJUSTE
    if "FINANCIAMIENTO" in g or "FINANCIAMIENTO" in c:
        return FamiliaConcepto.FINANCIAMIENTO
    if "RECONEXION" in g or "CARGO UNICO" in c or "OC " in c:
        return FamiliaConcepto.UNICO
    if "CARGO FIJO" in g or "PAQUETE" in g or "RECURRENTE" in c or "PLAN" in c:
        return FamiliaConcepto.RECURRENTE
    prefijo = codigo.split("_", 1)[0].upper()
    return _FAMILIA_POR_PREFIJO.get(prefijo, FamiliaConcepto.RECURRENTE)


@dataclass(slots=True)
class ConceptoExtraido:
    """Un concepto del catálogo real, con la evidencia que lo respalda."""

    concepto_id: str
    nombre_comercial: str
    familia: FamiliaConcepto
    grupo: str
    sub_grupo: str
    clasificacion: str
    apariciones: int
    renta_vencida: bool | None = None
    considerado: bool = True
    variantes_nombre: list[str] = field(default_factory=list)

    def a_dict(self) -> dict[str, Any]:
        """Forma serializable, apta para indexar en el RAG."""
        return {
            "concepto_id": self.concepto_id,
            "nombre_comercial": self.nombre_comercial,
            "familia": str(self.familia),
            "grupo": self.grupo,
            "sub_grupo": self.sub_grupo,
            "clasificacion": self.clasificacion,
            "apariciones": self.apariciones,
            "renta_vencida": self.renta_vencida,
            "considerado": self.considerado,
            "variantes_nombre": self.variantes_nombre,
            # Deliberadamente vacía: ver el docstring del módulo. Una definición
            # inventada aquí volvería a ser vocabulario nuestro disfrazado de dato.
            "definicion_cliente": "",
        }


@dataclass(slots=True)
class ResumenCatalogo:
    """Lo extraído, más las cuentas que permiten auditarlo."""

    conceptos: list[ConceptoExtraido]
    filas_leidas: int
    filas_ignoradas: int
    grupos: dict[str, int]
    sub_grupos: dict[str, int]

    @property
    def considerados(self) -> list[ConceptoExtraido]:
        """Solo los conceptos que el facturador manda tener en cuenta."""
        return [c for c in self.conceptos if c.considerado]

    def a_texto(self) -> str:
        """Resumen legible para la consola."""
        return (
            f"{len(self.conceptos)} conceptos ({len(self.considerados)} considerados) "
            f"de {self.filas_leidas:,} filas · {self.filas_ignoradas:,} marcadas "
            f"'{GRUPO_IGNORAR}' · {len(self.grupos)} grupos · {len(self.sub_grupos)} subgrupos"
        )


def extraer_catalogo(carpeta: Path | None = None) -> ResumenCatalogo:
    """Recorre el export y devuelve el catálogo de conceptos observado.

    Un concepto se identifica por ``CHARGE_CODE_ID``. Como un mismo código puede
    aparecer con descripciones ligeramente distintas entre ciclos, se toma **la más
    frecuente** como nombre comercial y se conservan las demás en ``variantes_nombre``:
    son sinónimos reales que el cliente puede citar, y tirarlos empobrecería el RAG.
    """
    nombres: dict[str, Counter[str]] = defaultdict(Counter)
    meta: dict[str, dict[str, str]] = {}
    apariciones: Counter[str] = Counter()
    marca_rv: dict[str, set[bool]] = defaultdict(set)
    grupos: Counter[str] = Counter()
    sub_grupos: Counter[str] = Counter()
    filas = ignoradas = 0

    for fila in leer_cargos(carpeta):
        filas += 1
        codigo = fila.get("CHARGE_CODE_ID", "")
        if not codigo:
            continue
        grupo = fila.get("GRUPO", "")
        grupos[grupo] += 1
        sub_grupos[fila.get("SUB_GRUPO", "")] += 1
        if grupo == GRUPO_IGNORAR:
            ignoradas += 1
        descripcion = fila.get("CHARGE_CODE_DESC", "")
        if descripcion:
            nombres[codigo][descripcion] += 1
        apariciones[codigo] += 1
        meta.setdefault(
            codigo,
            {
                "grupo": grupo,
                "sub_grupo": fila.get("SUB_GRUPO", ""),
                "clasificacion": fila.get("CHARGE_CODE_CLASSIFICATION", ""),
            },
        )
        if "CARGO FIJO" in grupo.upper():
            marca_rv[codigo].add(bool(_MARCA_RENTA_VENCIDA.match(descripcion)))

    conceptos: list[ConceptoExtraido] = []
    for codigo, cuenta in nombres.items():
        principal, _ = cuenta.most_common(1)[0]
        info = meta.get(codigo, {})
        grupo = info.get("grupo", "")
        marcas = marca_rv.get(codigo, set())
        conceptos.append(
            ConceptoExtraido(
                concepto_id=codigo,
                nombre_comercial=principal,
                familia=familia_de(codigo, grupo, info.get("clasificacion", "")),
                grupo=grupo,
                sub_grupo=info.get("sub_grupo", ""),
                clasificacion=info.get("clasificacion", ""),
                apariciones=apariciones[codigo],
                # Solo se afirma la modalidad cuando la marca es inequívoca y constante.
                # Ante señales mixtas se devuelve None: "no lo sé" es una respuesta
                # válida y "adelantada" sería una invención.
                renta_vencida=(True if marcas == {True} else None),
                considerado=grupo != GRUPO_IGNORAR,
                variantes_nombre=[n for n, _ in cuenta.most_common()[1:6]],
            )
        )

    conceptos.sort(key=lambda c: (-c.apariciones, c.concepto_id))
    return ResumenCatalogo(
        conceptos=conceptos,
        filas_leidas=filas,
        filas_ignoradas=ignoradas,
        grupos=dict(grupos),
        sub_grupos=dict(sub_grupos),
    )


def escribir_catalogo(destino: Path, resumen: ResumenCatalogo | None = None) -> Path:
    """Vuelca el catálogo a JSON, listo para indexar en el RAG.

    ``destino`` debe caer bajo ``data/``, que el repositorio ignora entero. No se
    comprueba aquí por no acoplar el módulo al layout, pero es la convención.
    """
    resumen = resumen or extraer_catalogo()
    destino.parent.mkdir(parents=True, exist_ok=True)
    payload: Mapping[str, Any] = {
        "origen": FICHERO_CARGOS,
        "filas_leidas": resumen.filas_leidas,
        "filas_ignoradas": resumen.filas_ignoradas,
        "grupos": resumen.grupos,
        "sub_grupos": resumen.sub_grupos,
        "conceptos": [c.a_dict() for c in resumen.conceptos],
    }
    destino.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _LOG.info("catálogo escrito en %s: %s", destino, resumen.a_texto())
    return destino
