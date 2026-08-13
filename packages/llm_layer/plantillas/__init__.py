"""Motor de plantillas determinísticas: la red de seguridad de la capa generativa.

Este paquete rinde dos servicios que parecen uno solo:

1. **Fallback determinístico.** Si el modelo falla, si tarda o si el verificador
   rechaza su texto dos veces, la respuesta se produce aquí. Sin red, sin azar y
   byte-reproducible: la misma entrada da exactamente la misma salida.
2. **Cuerpo del ``MockProvider``.** El mock genera con estas mismas plantillas y
   luego les aplica *jitter* léxico, de modo que la demo sin API key es
   indistinguible en estructura de la demo con Gemini.

**Ninguna plantilla calcula.** Todos los importes llegan ya formateados por
``core_domain.dinero.formatear_soles`` a partir de valores del FactSet, y todos los
enteros que se escriben (días, cuotas, años) están anclados en
``FactSet.mapa_tokens()``. Esa es la razón por la que la ruta de plantilla siempre
pasa el verificador.

Las plantillas trabajan sobre :class:`DatosPlantilla`, que se construye desde la
proyección ``FactSet.resumen_para_prompt()`` — la misma que ve el modelo. Así el mock
no dispone de ninguna información que un proveedor real no tenga.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import AccionSiguiente, ModalidadRenta, TipoMovimiento, Verbosidad
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.recibo import MESES_ES
from packages.llm_layer.providers.base import CausaExplicadaLLM, ExplicacionLLM

__all__ = [
    "PLANTILLAS_DISPONIBLES",
    "RUTA_PLANTILLAS",
    "DatosPlantilla",
    "construir_datos",
    "datos_de_factset",
    "elegir_plantilla",
    "recortar_seguro",
    "renderizar_explicacion",
    "renderizar_texto_libre",
]

#: Directorio de las plantillas ``*.jinja`` (este mismo paquete).
RUTA_PLANTILLAS = Path(__file__).resolve().parent

#: Una plantilla por causa raíz. Cada fichero sirve las dos verbosidades.
PLANTILLAS_DISPONIBLES: tuple[str, ...] = (
    "cambio_plan_adelantada",
    "cambio_plan_vencida",
    "reconexion",
    "fin_descuento",
    "cuota_equipo",
    "paquete",
    "nota_credito",
    "deuda_anterior",
    "estable",
    "generico",
)

#: Causa dominante -> plantilla. Lista cerrada: lo que no está aquí cae en ``generico``.
_MAPA_CAUSA_PLANTILLA: dict[TipoMovimiento, str] = {
    TipoMovimiento.CAMBIO_PLAN: "cambio_plan",  # se resuelve por modalidad de renta
    TipoMovimiento.RECONEXION: "reconexion",
    TipoMovimiento.SUSPENSION: "reconexion",
    TipoMovimiento.AJUSTE_SUSPENSION: "reconexion",
    TipoMovimiento.FIN_DESCUENTO: "fin_descuento",
    TipoMovimiento.ALTA_EQUIPO_FINANCIADO: "cuota_equipo",
    TipoMovimiento.ALTA_PAQUETE: "paquete",
    TipoMovimiento.ALTA_SERVICIO: "paquete",
    TipoMovimiento.NOTA_CREDITO: "nota_credito",
    TipoMovimiento.NOTA_DEBITO: "nota_credito",
    TipoMovimiento.BAJA_SERVICIO: "generico",
}


# --------------------------------------------------------------------------- #
# Datos de plantilla
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DatosPlantilla:
    """Todo lo que una plantilla puede escribir, ya formateado y ya anclado.

    Las cadenas monetarias vienen de ``formatear_soles``; los enteros (días, cuotas,
    años) proceden literalmente del FactSet. Una plantilla que solo use estos campos
    **no puede** producir una cifra no anclada.
    """

    verbosidad: str
    detalle: bool
    modalidad: str
    es_adelantada: bool
    periodo_actual: str
    periodo_previo: str
    mes_actual: str
    mes_previo: str
    dias_ciclo: int
    total_actual: str
    total_previo: str
    total_a_pagar: str
    delta_abs: str
    delta_cent: int
    total_actual_cent: int
    total_previo_cent: int
    subio: bool
    bajo: bool
    sin_variacion: bool
    deuda_anterior: str
    deuda_anterior_cent: int
    tiene_deuda: bool
    causas: list[dict[str, Any]] = field(default_factory=list)
    lineas: list[dict[str, Any]] = field(default_factory=list)
    beneficios: list[str] = field(default_factory=list)
    causa_principal: dict[str, Any] | None = None
    linea_principal: dict[str, Any] | None = None
    tramos: list[dict[str, Any]] = field(default_factory=list)
    cuota: dict[str, Any] | None = None
    plantilla: str = "generico"
    factset_id: str = ""
    # --- separación de signos: lo que sube y lo que ahorra, por separado --- #
    causas_suben: list[dict[str, Any]] = field(default_factory=list)
    causas_bajan: list[dict[str, Any]] = field(default_factory=list)
    causa_que_sube: dict[str, Any] | None = None
    causa_que_ahorra: dict[str, Any] | None = None
    signos_mixtos: bool = False
    # --- honestidad sobre lo que NO se puede confirmar ---------------------- #
    #: Hay al menos una línea con variación cuyo motivo no consta en ningún sistema.
    #: El desglose sigue completo —cuánto y en qué línea salen del propio recibo—, pero
    #: el porqué necesitaría una orden del CRM que no existe. Cuando esto es cierto la
    #: explicación lo dice con todas las letras en vez de insinuar una causa plausible:
    #: la alternativa honesta a inventar no es callarse, es nombrar el límite.
    causa_sin_confirmar: bool = False

    def cifras_usadas_cent(self) -> list[int]:
        """Enteros en céntimos que las plantillas pueden citar. Todos anclados."""
        cifras = [
            self.total_actual_cent,
            self.total_previo_cent,
            self.delta_cent,
            abs(self.delta_cent),
        ]
        if self.tiene_deuda:
            cifras.append(self.deuda_anterior_cent)
        cifras.extend(int(causa["monto_cent"]) for causa in self.causas)
        cifras.extend(int(linea["delta_cent"]) for linea in self.lineas)
        return sorted(dict.fromkeys(cifras))


def _mes_de_periodo(periodo: str) -> str:
    """``"2026-07" -> "julio de 2026"``. Sin cifras nuevas: el año está anclado."""
    try:
        anio, mes = periodo.split("-")
        return f"{MESES_ES[int(mes)]} de {anio}"
    except (ValueError, KeyError):  # pragma: no cover - Periodo ya valida el formato
        return periodo


def _beneficios_narrables(beneficios: Any, maximo: int = 2) -> list[str]:
    """Beneficios del efecto efervescente que se pueden escribir sin riesgo.

    Se descartan los que contienen dígitos ("20 GB de datos"): esas cifras no son
    campos del FactSet sino texto libre, y el verificador las trataría —con razón—
    como no ancladas. El efecto efervescente es una mejora de experiencia, jamás una
    excusa para relajar el anclaje numérico.
    """
    limpios = [str(b).strip() for b in (beneficios or []) if str(b).strip()]
    return [b for b in limpios if not re.search(r"\d", b)][:maximo]


def _mayor_impacto(causas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Causa de mayor impacto absoluto; desempata por etiqueta para ser determinista."""
    if not causas:
        return None
    return max(causas, key=lambda causa: (abs(int(causa["monto_cent"])), causa["etiqueta"]))


def _causa_principal(causas: list[dict[str, Any]], delta_cent: int = 0) -> dict[str, Any] | None:
    """Causa que se narra primero: la que **explica la dirección** del recibo.

    Ordenar solo por impacto absoluto basta cuando todas las causas empujan hacia el
    mismo lado, pero produce una explicación al revés cuando no: si el recibo sube y la
    causa más grande es un ahorro, el cliente pregunta *"¿por qué subió?"* y se le
    contesta hablando de lo que bajó.

    Por eso la principal es la de mayor impacto absoluto **entre las que tienen el signo
    del delta total**, y solo si ninguna lo tiene se cae al mayor impacto absoluto a
    secas. Es lo que hace que un fin de descuento gane a un cambio de plan que ahorra.
    """
    if not causas:
        return None
    if delta_cent > 0:
        alineadas = [causa for causa in causas if int(causa["monto_cent"]) > 0]
    elif delta_cent < 0:
        alineadas = [causa for causa in causas if int(causa["monto_cent"]) < 0]
    else:
        alineadas = []
    return _mayor_impacto(alineadas) or _mayor_impacto(causas)


def construir_datos(
    resumen: dict[str, Any],
    *,
    verbosidad: Verbosidad | str = Verbosidad.CORTO,
    factset_id: str = "",
) -> DatosPlantilla:
    """Construye :class:`DatosPlantilla` desde ``FactSet.resumen_para_prompt()``.

    Args:
        resumen: la proyección del FactSet que también recibe el modelo.
        verbosidad: ``CORTO`` o ``DETALLE``.
        factset_id: identificador del FactSet (semilla del jitter del mock).

    Es deliberado que esta función NO reciba el ``FactSet`` completo: el mock debe
    poder producir su salida con exactamente la misma información que un proveedor
    externo, ni un campo más.
    """
    verbosidad_txt = str(verbosidad)
    detalle = verbosidad_txt == str(Verbosidad.DETALLE)

    total_actual_cent = int(resumen.get("total_actual_cent", 0))
    total_previo_cent = int(resumen.get("total_previo_cent", 0))
    delta_cent = int(resumen.get("delta_total_cent", total_actual_cent - total_previo_cent))
    deuda_cent = int(resumen.get("deuda_anterior_cent", 0))
    modalidad = str(resumen.get("modalidad_renta", ModalidadRenta.VENCIDA))

    lineas: list[dict[str, Any]] = []
    for cruda in resumen.get("lineas", []):
        tramos = [
            {
                "etiqueta": tramo.get("etiqueta", ""),
                "dias": int(tramo.get("dias", 0)),
                "tarifa": formatear_soles(int(tramo.get("tarifa_mensual_cent", 0))),
                "tarifa_cent": int(tramo.get("tarifa_mensual_cent", 0)),
                "prorrateado": formatear_soles(int(tramo.get("monto_prorrateado_cent", 0))),
                "prorrateado_cent": int(tramo.get("monto_prorrateado_cent", 0)),
                "estado": str(tramo.get("estado", "ACTIVO")),
                "suspendido": str(tramo.get("estado", "ACTIVO")) == "SUSPENDIDO",
            }
            for tramo in cruda.get("tramos", []) or []
        ]
        delta_linea = int(cruda.get("delta_cent", 0))
        lineas.append(
            {
                "concepto_id": str(cruda.get("concepto_id", "")),
                "nombre": str(cruda.get("nombre_comercial", "")),
                "clase": str(cruda.get("clase", "")),
                "causa": cruda.get("causa"),
                "delta_cent": delta_linea,
                "delta": formatear_soles(abs(delta_linea)),
                "monto_actual": formatear_soles(int(cruda.get("monto_actual_cent", 0))),
                "monto_actual_cent": int(cruda.get("monto_actual_cent", 0)),
                "monto_previo": formatear_soles(int(cruda.get("monto_previo_cent", 0))),
                "monto_previo_cent": int(cruda.get("monto_previo_cent", 0)),
                "sube": delta_linea > 0,
                "es_nuevo": str(cruda.get("clase", "")) == "NUEVO",
                "desaparecio": str(cruda.get("clase", "")) == "DESAPARECIDO",
                "dias_prorrateo": cruda.get("dias_prorrateo"),
                "cuota": cruda.get("cuota"),
                "tramos": tramos,
                "causa_confirmada": bool(cruda.get("causa_confirmada", False)),
                # Por defecto `True`: si una proyección antigua no trae el campo, se sigue
                # exigiendo causa, que es el lado prudente.
                "exige_causa": bool(cruda.get("exige_causa", True)),
            }
        )

    causas: list[dict[str, Any]] = []
    for cruda in resumen.get("causas_agregadas", []):
        monto_cent = int(cruda.get("monto_cent", 0))
        causa_txt = cruda.get("causa")
        concepto = next(
            (linea["concepto_id"] for linea in lineas if linea["causa"] == causa_txt),
            "",
        )
        causas.append(
            {
                "etiqueta": str(cruda.get("etiqueta_cliente", "otros cargos")),
                "causa": causa_txt,
                "concepto_id": concepto or (lineas[0]["concepto_id"] if lineas else "AGREGADO"),
                "monto_cent": monto_cent,
                "monto": formatear_soles(abs(monto_cent)),
                "sube": monto_cent > 0,
            }
        )

    # Las causas ya vienen del motor ordenadas por impacto absoluto descendente. Aquí
    # solo se separan por signo, que es lo que permite a la plantilla decir "esto le
    # subió el recibo" y "esto se lo bajó" sin mezclarlo en un neto que engaña.
    causas_suben = [causa for causa in causas if int(causa["monto_cent"]) > 0]
    causas_bajan = [causa for causa in causas if int(causa["monto_cent"]) < 0]

    principal = _causa_principal(causas, delta_cent)
    linea_principal = next(
        (linea for linea in lineas if principal and linea["causa"] == principal["causa"]),
        lineas[0] if lineas else None,
    )
    con_tramos = next((linea for linea in lineas if linea["tramos"]), None)
    con_cuota = next((linea for linea in lineas if linea["cuota"]), None)

    datos = DatosPlantilla(
        verbosidad=verbosidad_txt,
        detalle=detalle,
        modalidad=modalidad,
        es_adelantada=modalidad == str(ModalidadRenta.ADELANTADA),
        periodo_actual=str(resumen.get("periodo_actual", "")),
        periodo_previo=str(resumen.get("periodo_previo", "")),
        mes_actual=_mes_de_periodo(str(resumen.get("periodo_actual", ""))),
        mes_previo=_mes_de_periodo(str(resumen.get("periodo_previo", ""))),
        dias_ciclo=int(resumen.get("dias_ciclo", 30)),
        total_actual=formatear_soles(total_actual_cent),
        total_previo=formatear_soles(total_previo_cent),
        total_a_pagar=formatear_soles(total_actual_cent + deuda_cent),
        delta_abs=formatear_soles(abs(delta_cent)),
        delta_cent=delta_cent,
        total_actual_cent=total_actual_cent,
        total_previo_cent=total_previo_cent,
        subio=delta_cent > 0,
        bajo=delta_cent < 0,
        sin_variacion=delta_cent == 0,
        deuda_anterior=formatear_soles(deuda_cent),
        deuda_anterior_cent=deuda_cent,
        tiene_deuda=deuda_cent > 0,
        causas=causas,
        lineas=lineas,
        beneficios=_beneficios_narrables(resumen.get("beneficios_vigentes", [])),
        causa_principal=principal,
        causas_suben=causas_suben,
        causas_bajan=causas_bajan,
        causa_que_sube=_mayor_impacto(causas_suben),
        causa_que_ahorra=_mayor_impacto(causas_bajan),
        signos_mixtos=bool(causas_suben and causas_bajan),
        # `lineas` ya viene filtrada a las que varían (`lineas_explicables`), así que
        # esto es exactamente "queda variación cuyo motivo no consta en ningún sistema".
        #
        # Se excluyen las líneas que NO exigen causa —IGV, redondeo—: ahí no falta
        # información, es que no hay ninguna que buscar. Incluirlas hacía que el cliente
        # de guion, con sus tres movimientos perfectamente atribuidos, terminara la
        # explicación disculpándose por el IGV.
        causa_sin_confirmar=any(
            linea["exige_causa"] and not linea["causa_confirmada"] for linea in lineas
        ),
        linea_principal=linea_principal,
        tramos=(con_tramos or {}).get("tramos", []),
        cuota=(
            {
                "texto": str(con_cuota["cuota"]),
                "nombre": con_cuota["nombre"],
                "monto": con_cuota["monto_actual"],
                "monto_cent": con_cuota["monto_actual_cent"],
            }
            if con_cuota
            else None
        ),
        factset_id=factset_id,
    )
    return _con_plantilla(datos, elegir_plantilla(datos))


def _con_plantilla(datos: DatosPlantilla, nombre: str) -> DatosPlantilla:
    """Devuelve una copia de ``datos`` con la plantilla elegida (el dataclass es inmutable)."""
    campos = {
        campo: getattr(datos, campo)
        for campo in DatosPlantilla.__dataclass_fields__  # type: ignore[attr-defined]
    }
    campos["plantilla"] = nombre
    return DatosPlantilla(**campos)


def datos_de_factset(
    factset: FactSet, verbosidad: Verbosidad | str = Verbosidad.CORTO
) -> DatosPlantilla:
    """Atajo: ``construir_datos(factset.resumen_para_prompt(), ...)``."""
    return construir_datos(
        factset.resumen_para_prompt(),
        verbosidad=verbosidad,
        factset_id=str(factset.factset_id),
    )


def elegir_plantilla(datos: DatosPlantilla) -> str:
    """Elige la plantilla por causa raíz dominante (y modalidad, si es cambio de plan)."""
    principal = datos.causa_principal
    if principal is None:
        if datos.tiene_deuda:
            return "deuda_anterior"
        return "estable" if datos.sin_variacion else "generico"

    causa = principal.get("causa")
    if causa is None:
        return "deuda_anterior" if datos.tiene_deuda else "generico"

    try:
        movimiento = TipoMovimiento(str(causa))
    except ValueError:
        return "generico"

    base = _MAPA_CAUSA_PLANTILLA.get(movimiento, "generico")
    if base == "cambio_plan":
        return "cambio_plan_adelantada" if datos.es_adelantada else "cambio_plan_vencida"
    return base


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _entorno() -> Environment:
    """Entorno Jinja de las plantillas de respuesta (texto plano, sin autoescape)."""
    return Environment(
        loader=FileSystemLoader(str(RUTA_PLANTILLAS)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _normalizar(texto: str) -> str:
    """Colapsa espacios y saltos: las plantillas se escriben legibles, no compactas."""
    return re.sub(r"\s+", " ", str(texto)).strip()


def recortar_seguro(texto: str, limite: int = 180) -> str:
    """Recorta a ``limite`` caracteres **sin partir jamás una cifra**.

    El corte se hace en un final de frase o, en su defecto, en un espacio: como
    ningún número contiene espacios, es imposible que el recorte fabrique una cifra
    inexistente (``"S/ 124.90"`` nunca se convierte en ``"S/ 12"``). Si queda un
    símbolo de moneda huérfano al final, se elimina.
    """
    limpio = _normalizar(texto)
    if len(limpio) <= limite:
        return limpio
    corte = limpio.rfind(". ", 0, limite)
    if corte > limite // 2:
        return limpio[: corte + 1]
    corte = limpio.rfind(" ", 0, limite)
    recorte = limpio[: corte if corte > 0 else limite].rstrip()
    recorte = re.sub(r"[\s,;:]*(?:S/\.?)?$", "", recorte).rstrip(" ,;:")
    return recorte


def _modulo(nombre: str) -> Any:
    """Carga el módulo Jinja de una plantilla para invocar sus macros por separado."""
    if nombre not in PLANTILLAS_DISPONIBLES:
        nombre = "generico"
    return _entorno().get_template(f"{nombre}.jinja").module


def accion_sugerida(datos: DatosPlantilla) -> AccionSiguiente:
    """Siguiente paso recomendado según la causa dominante (lista literal de la ficha)."""
    if datos.tiene_deuda:
        return AccionSiguiente.PAGAR
    if datos.sin_variacion:
        return AccionSiguiente.PAGAR
    if datos.subio and datos.plantilla in {
        "cambio_plan_adelantada",
        "cambio_plan_vencida",
        "fin_descuento",
    }:
        return AccionSiguiente.VER_ALTERNATIVAS
    return AccionSiguiente.VER_DETALLE


def renderizar_explicacion(datos: DatosPlantilla) -> ExplicacionLLM:
    """Produce una :class:`ExplicacionLLM` determinística a partir de las plantillas.

    Es la salida de la ruta PLANTILLA y también el punto de partida del
    ``MockProvider`` (que después le aplica jitter léxico).
    """
    modulo = _modulo(datos.plantilla)

    #: Cuántas causas se NARRAN. Las demás siguen contando en el cuadre y siguen en la
    #: tabla de la pantalla; lo que no hacen es alargar el texto. Una explicación que
    #: enumera cuatro causas con la misma estructura gramatical no es más completa: es
    #: más difícil de leer, y el cliente la abandona antes de llegar a la que importa.
    #: Dos es el máximo que cabe en la pantalla de un móvil sin hacer scroll.
    MAX_CAUSAS_NARRADAS = 2

    causas: list[CausaExplicadaLLM] = []
    for causa in datos.causas[:MAX_CAUSAS_NARRADAS]:
        causas.append(
            CausaExplicadaLLM(
                concepto_id=causa["concepto_id"],
                frase=_normalizar(modulo.frase(datos, causa))[:320],
                monto_cent_citado=int(causa["monto_cent"]),
            )
        )

    # La suma de importes citados debe cerrar exactamente contra el delta total
    # (sección 5.3, paso 5). Si el agregado de causas no cierra, se declara el resto
    # como "otros cargos": nunca se deja un descuadre silencioso.
    residual = datos.delta_cent - sum(causa.monto_cent_citado for causa in causas)
    if residual != 0 and (causas or datos.delta_cent != 0):
        causas.append(
            CausaExplicadaLLM(
                concepto_id=(datos.lineas[0]["concepto_id"] if datos.lineas else "AGREGADO"),
                frase=_normalizar(modulo.frase_residual(datos)),
                monto_cent_citado=residual,
            )
        )

    return ExplicacionLLM(
        resumen=recortar_seguro(modulo.resumen(datos), 180),
        causas=causas,
        siguiente_paso=accion_sugerida(datos),
        cifras_usadas=datos.cifras_usadas_cent(),
    )


def renderizar_texto_libre(datos: DatosPlantilla, macro: str) -> str:
    """Renderiza una macro suelta de la plantilla elegida (``detalle`` o ``cierre``).

    Lo usa el generador para los bloques que redacta el sistema —nunca el modelo—:
    el párrafo de detalle y el cierre con el efecto efervescente.
    """
    modulo = _modulo(datos.plantilla)
    funcion = getattr(modulo, macro, None)
    if funcion is None:
        return ""
    return _normalizar(funcion(datos))
