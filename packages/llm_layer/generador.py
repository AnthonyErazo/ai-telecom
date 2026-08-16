"""Orquestación de la explicación: LLM → verificar → reintento → plantilla.

La política es corta y no admite matices (sección 5.3, paso 6):

1. Se pide la explicación al proveedor y se compone la respuesta.
2. Se **verifica** el texto final contra el FactSet.
3. Si el veredicto es ``FAIL``, se reintenta **una** vez diciéndole al modelo, literal,
   *"los números X, Y no existen en FACTSET"*.
4. Si vuelve a fallar, si hay timeout o si el proveedor revienta, se responde con la
   **plantilla determinística**, que por construcción solo escribe cifras del FactSet.
5. Si ni siquiera la plantilla verificase —lo que significaría un fallo grave del
   motor—, la respuesta se **bloquea**: se entrega un texto sin ninguna cifra y se
   marca la derivación a asesor.

De ahí la exigencia dura del proyecto: **ninguna cifra llega al cliente sin estar
anclada en el FactSet**. Los importes de los bloques estructurados (``kv``, ``puente``,
``tabla``) los pone el sistema formateando enteros del FactSet; del modelo solo se
aprovecha la prosa, y esa prosa se audita cifra a cifra antes de salir.
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import formatear_soles
from packages.core_domain.enums import (
    AccionSiguiente,
    Canal,
    ModoGeneracion,
    MotivoDerivacion,
    Verbosidad,
    VeredictoVerificacion,
)
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import (
    Accion,
    BarraPuente,
    Bloque,
    BloqueAviso,
    BloqueCiclos,
    BloqueKV,
    BloquePuente,
    BloqueTabla,
    BloqueTexto,
    CausaVisual,
    CicloExplicado,
    Cita,
    Derivacion,
    Gobernanza,
    HitoCiclo,
    ItemKV,
    RespuestaCanalAgnostica,
)
from packages.facts_engine.intencion import concepto_facturacion, pide_detalle_cargos
from packages.llm_layer.plantillas import (
    DatosPlantilla,
    datos_de_factset,
    recortar_seguro,
    renderizar_explicacion,
    renderizar_texto_libre,
)
from packages.llm_layer.prompts import construir_prompt, mensaje_correccion, version_prompt
from packages.llm_layer.providers.base import (
    ESQUEMA_EXPLICACION_V1,
    ErrorProveedor,
    ExplicacionLLM,
    ProveedorLLM,
    obtener_proveedor,
    timeout_por_defecto,
    version_modelo_de,
)
from packages.llm_layer.providers.mock import aplicar_jitter, semilla_de
from packages.llm_layer.verificador import (
    ConjuntoPermitido,
    ResultadoVerificacion,
    construir_permitidos,
    verificar,
)

__all__ = [
    "ETIQUETAS_ACCION",
    "IntentoGeneracion",
    "ResultadoGeneracion",
    "a_respuesta",
    "componer_bloques",
    "enfocar_resumen_consulta",
    "explicar",
    "fijar_narrativa_de_notas",
    "formatear_centimos_en_prosa",
    "generar_explicacion",
]

_LOG = logging.getLogger(__name__)

#: Etiqueta y riesgo de cada siguiente acción (lista literal de la ficha del desafío).
ETIQUETAS_ACCION: dict[AccionSiguiente, tuple[str, str]] = {
    AccionSiguiente.PAGAR: ("Pagar mi recibo", "REVERSIBLE"),
    AccionSiguiente.VER_DETALLE: ("Ver el detalle del recibo", "INFORMATIVA"),
    AccionSiguiente.REGISTRAR_CONSULTA: ("Registrar mi consulta", "INFORMATIVA"),
    AccionSiguiente.VER_ALTERNATIVAS: ("Ver alternativas para mi plan", "INFORMATIVA"),
    AccionSiguiente.DERIVAR_ASESOR: ("Hablar con un asesor", "INFORMATIVA"),
}

#: Texto de bloqueo. No contiene ni una sola cifra, a propósito, y tampoco nombra al
#: asesor: la derivación se dispara igual por debajo, pero anunciarla aquí convierte el
#: único caso en que el motor se planta en una despedida. Basta con no dar la cifra.
TEXTO_BLOQUEADO = (
    "Prefiero no darle una cifra que no pueda sustentar. Estoy revisando su recibo para "
    "confirmarle el detalle exacto."
)


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #
class IntentoGeneracion(BaseModel):
    """Traza de un intento de generación. Va entera al log ``LLM_CALL``."""

    model_config = ConfigDict(extra="forbid")

    numero: int
    modo: ModoGeneracion
    proveedor: str
    veredicto: str = str(VeredictoVerificacion.NO_APLICA)
    no_ancladas: int = 0
    infractores: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    latencia_ms: int = 0


class ResultadoGeneracion(BaseModel):
    """Todo lo que produce la capa generativa para un turno."""

    model_config = ConfigDict(extra="forbid")

    texto: str
    bloques: list[Bloque] = Field(default_factory=list)
    acciones: list[Accion] = Field(default_factory=list)
    modo: ModoGeneracion
    verificacion: ResultadoVerificacion
    citas: list[Cita] = Field(default_factory=list)
    gobernanza: Gobernanza
    derivacion: Derivacion = Field(default_factory=Derivacion)
    explicacion: ExplicacionLLM | None = None
    proveedor: str = ""
    model_version: str = ""
    plantilla: str = ""
    latencia_ms: int = 0
    intentos: list[IntentoGeneracion] = Field(default_factory=list)
    bloqueada: bool = False

    @property
    def anclado(self) -> bool:
        """``True`` si el texto entregado no contiene ninguna cifra sin anclar."""
        return self.verificacion.anclado

    def telemetria(self) -> dict[str, Any]:
        """Telemetría del turno (incluye la sonda de silencio post-explicación)."""
        return {
            "modo": str(self.modo),
            "proveedor": self.proveedor,
            "model_version": self.model_version,
            "plantilla": self.plantilla,
            "prompt_version": version_prompt(),
            "latencia_ms": self.latencia_ms,
            "intentos": len(self.intentos),
            "aserciones_totales": self.verificacion.aserciones_totales,
            "aserciones_no_ancladas": self.verificacion.no_ancladas,
            "veredicto": str(self.verificacion.veredicto),
        }


# --------------------------------------------------------------------------- #
# Preámbulo: la frase que anuncia la explicación en vez de darla
# --------------------------------------------------------------------------- #
#: Aperturas que no informan de nada. El modelo las produce porque suenan corteses, y en
#: un `resumen` de UNA frase se comen la respuesta entera: el cliente lee «Le explicamos
#: por qué su recibo vino de esta manera», que es exactamente lo que ya sabía —lo
#: preguntó él—, y tiene que seguir leyendo para enterarse de algo. Además suena a
#: locución de central telefónica, no a persona.
#: Sin `\b` a propósito: todas las alternativas van ancladas en `^`, así que basta con
#: reconocer el ARRANQUE de la frase; exigir además final de palabra no cambiaría ninguna
#: decisión y complica un patrón que ya es largo.
_PREAMBULO = re.compile(
    r"^\s*(?:"
    r"(?:le|te)\s+(?:explic|detall|coment|indic|inform|resum|cuent|aclar)\w*"
    r"|(?:aqu[íi]|ac[áa])\s"
    r"|a\s+continuaci[óo]n"
    r"|permítame|perm[íi]tame"
    r"|(?:paso|procedo)\s+a\s"
    r"|(?:claro|por\s+supuesto|entendido|con\s+gusto)[,.\s]"
    r")",
    re.IGNORECASE,
)


def sin_preambulo(texto: str) -> str:
    """Quita las frases iniciales que anuncian la explicación en lugar de darla.

    El prompt ya lo prohíbe, pero una regla de estilo en un prompt es una petición, no
    una garantía: el modelo la cumple casi siempre y falla justo en las respuestas más
    largas, que son las que peor se leen. Esto es el cinturón determinista.

    Corta **frases completas** desde el principio y solo mientras coincidan con el
    patrón; en cuanto una frase dice algo, para. Así «Le explicamos por qué su recibo
    vino de esta manera. Se le cobró la reactivación.» se queda en la segunda, que es la
    que responde. Nunca toca cifras: opera sobre el texto ya escrito por el modelo y solo
    puede quitar frases enteras, de modo que no puede desanclar un importe —o la cifra
    estaba en la frase que se va, y se va entera, o no se toca.

    Si TODO el texto era preámbulo devuelve cadena vacía: quien llama omite el bloque, y
    la respuesta empieza por el hecho, que es lo que se quería.
    """
    frases = re.split(r"(?<=[.!?])\s+", texto.strip())
    while frases and _PREAMBULO.match(frases[0]):
        frases.pop(0)
    return " ".join(frases).strip()


_CENTIMOS_EN_PROSA = re.compile(
    r"(?<![\w.,])(?P<monto>[+-]?\s*\d+)\s+c[eé]ntimos?\b",
    re.IGNORECASE,
)


def formatear_centimos_en_prosa(explicacion: ExplicacionLLM) -> ExplicacionLLM:
    """Convierte importes crudos del LLM al formato monetario visible para el cliente.

    El contrato estructurado conserva ``monto_cent_citado`` como entero para que el
    verificador trabaje sin decimales. Solo se corrigen ``resumen`` y ``frase``: por
    ejemplo, ``20 céntimos`` pasa a ``S/ 0.20`` y ``-900 céntimos`` a ``-S/ 9.00``.
    """

    def convertir(texto: str) -> str:
        return _CENTIMOS_EN_PROSA.sub(
            lambda hallado: formatear_soles(int(hallado.group("monto").replace(" ", ""))),
            texto,
        )

    return explicacion.model_copy(
        update={
            "resumen": convertir(explicacion.resumen),
            "causas": [
                causa.model_copy(update={"frase": convertir(causa.frase)})
                for causa in explicacion.causas
            ],
        }
    )


def fijar_narrativa_de_notas(
    explicacion: ExplicacionLLM,
    datos: DatosPlantilla,
) -> ExplicacionLLM:
    """Impide que un proveedor confunda el valor de una nota con su diferencia mensual.

    El verificador numérico prueba que una cifra existe, pero por sí solo no puede saber
    si el modelo llamó «nota actual» al delta entre notas. En este caso financiero
    sensible se sustituye la narrativa por la plantilla construida con
    ``monto_actual_cent``, ``monto_previo_cent`` y ``delta_cent`` del FactSet.
    """
    if datos.plantilla != "nota_credito":
        return explicacion
    segura = renderizar_explicacion(datos)
    return segura.model_copy(update={"siguiente_paso": explicacion.siguiente_paso})


def _concepto_aparece_en_factset(factset: FactSet, concepto: str) -> bool:
    """Comprueba presencia con campos estructurados; nunca por inferencia del LLM."""
    if concepto == "renta adelantada":
        return str(factset.modalidad_renta) == "ADELANTADA"
    if concepto == "renta vencida":
        return str(factset.modalidad_renta) == "VENCIDA"

    for linea in factset.lineas:
        identificador = linea.concepto_id.upper()
        causa = str(linea.causa or "").upper()
        if concepto == "prorrateo" and (
            "PRORR" in identificador or linea.dias_prorrateo is not None or bool(linea.tramos)
        ):
            return True
        if concepto == "reconexión" and (
            "RECONEX" in identificador or causa == "RECONEXION"
        ):
            return True
        if concepto == "nota de crédito" and (
            "NOTA_CREDITO" in identificador or causa == "NOTA_CREDITO"
        ):
            return True
        if concepto == "nota de débito" and (
            "NOTA_DEBITO" in identificador or causa == "NOTA_DEBITO"
        ):
            return True
        if concepto == "menor abono" and (
            ("NOTA_CREDITO" in identificador or causa == "NOTA_CREDITO")
            and linea.delta_cent > 0
            and linea.monto_previo_cent < 0
        ):
            return True
        if concepto == "mayor abono" and (
            ("NOTA_CREDITO" in identificador or causa == "NOTA_CREDITO")
            and linea.delta_cent < 0
            and linea.monto_actual_cent < 0
        ):
            return True
        if concepto == "mayor cargo" and (
            ("NOTA_DEBITO" in identificador or causa == "NOTA_DEBITO")
            and linea.delta_cent > 0
            and linea.monto_actual_cent > 0
        ):
            return True
        if concepto == "menor cargo" and (
            ("NOTA_DEBITO" in identificador or causa == "NOTA_DEBITO")
            and linea.delta_cent < 0
            and linea.monto_previo_cent > 0
        ):
            return True
        if concepto == "cuota del equipo" and (
            linea.cuota_numero is not None or linea.cuotas_totales is not None
        ):
            return True
    return False


def enfocar_resumen_consulta(
    explicacion: ExplicacionLLM,
    factset: FactSet,
    utterance: str,
) -> ExplicacionLLM:
    """Hace que una consulta aplicada reciba primero un sí/no respaldado por hechos.

    Es especialmente importante en modo plantilla: si Gemini no está disponible, la
    respuesta determinística ya no vuelve a la causa principal olvidando el concepto
    que el cliente preguntó.
    """
    concepto = concepto_facturacion(utterance)
    if concepto is None:
        return explicacion
    if _concepto_aparece_en_factset(factset, concepto):
        resumen = f"Sí. En el recibo consultado aparece {concepto}."
    else:
        resumen = (
            f"No identifico {concepto} en el recibo consultado. "
            "A continuación le muestro lo que sí explica la variación."
        )
    return explicacion.model_copy(update={"resumen": resumen})


# --------------------------------------------------------------------------- #
# Composición de bloques — aquí es donde se ponen las cifras
# --------------------------------------------------------------------------- #
def componer_bloques(
    factset: FactSet,
    explicacion: ExplicacionLLM,
    datos: DatosPlantilla,
    *,
    verbosidad: Verbosidad | str = Verbosidad.CORTO,
    mostrar_ciclos: bool = False,
) -> list[Bloque]:
    """Construye los bloques de la respuesta.

    Del modelo se toma **solo texto**: el resumen y una frase por causa. Todos los
    importes que se muestran salen de ``formatear_soles`` sobre enteros del FactSet,
    de manera que el bloque ``kv``, el ``puente`` y la ``tabla`` son anclados por
    construcción.
    """
    detalle = str(verbosidad) == str(Verbosidad.DETALLE)
    bloques: list[Bloque] = []

    resumen = sin_preambulo(explicacion.resumen)
    if resumen:
        bloques.append(
            BloqueTexto(
                texto=resumen,
                fact_ids=["factset:delta_total_cent", "factset:total_actual_cent"],
            )
        )

    if mostrar_ciclos:
        bloques.append(_bloque_ciclos(factset))

    items = [
        ItemKV(
            clave=f"Recibo de {datos.mes_previo}",
            valor=datos.total_previo,
            monto_cent=factset.total_previo_cent,
            fact_id="factset:total_previo_cent",
        ),
        ItemKV(
            clave=f"Recibo de {datos.mes_actual}",
            valor=datos.total_actual,
            monto_cent=factset.total_actual_cent,
            fact_id="factset:total_actual_cent",
        ),
        ItemKV(
            clave="Diferencia",
            valor=formatear_soles(factset.delta_total_cent),
            monto_cent=factset.delta_total_cent,
            fact_id="factset:delta_total_cent",
        ),
    ]
    if factset.deuda_anterior_cent:
        items.append(
            ItemKV(
                clave="Saldo de recibos anteriores",
                valor=datos.deuda_anterior,
                monto_cent=factset.deuda_anterior_cent,
                fact_id="factset:deuda_anterior_cent",
            )
        )
        items.append(
            ItemKV(
                clave="Total a pagar",
                valor=datos.total_a_pagar,
                monto_cent=factset.total_a_pagar_cent,
                fact_id="factset:total_a_pagar_cent",
            )
        )
    if not mostrar_ciclos:
        bloques.append(
            BloqueKV(
                titulo="Su recibo en números",
                items=items,
                fact_ids=["factset:total_actual_cent", "factset:total_previo_cent"],
            )
        )

    if factset.causas_agregadas and not mostrar_ciclos:
        # Los totales anterior/actual y la diferencia ya aparecen justo arriba en
        # «Su recibo en números». Aquí se muestran solo las causas para no repetir
        # tres importes dentro de la misma respuesta.
        barras = []
        for causa in factset.causas_agregadas:
            barras.append(
                BarraPuente(
                    etiqueta=causa.etiqueta_cliente.capitalize(),
                    monto_cent=causa.monto_cent,
                    tipo="incremento" if causa.monto_cent >= 0 else "decremento",
                    fact_id=f"causa:{causa.causa or causa.causa_oficial or 'SIN_CAUSA'}.monto_cent",
                )
            )
        bloques.append(BloquePuente(titulo="Qué produjo la diferencia", barras=barras))

    # Una causa, un bloque. Antes se unían todas las frases en un solo `BloqueTexto` con
    # los `fact_ids` de todas las líneas juntos, y ahí se perdía el emparejamiento: el
    # texto decía tres cosas y las anclas eran tres, pero nadie sabía cuál iba con cuál.
    # Separadas, cada frase viaja con la línea del recibo de la que habla, que es lo que
    # permite señalar en el recibo lo que se está explicando. El texto que se lee no
    # cambia —los canales concatenan los bloques—; lo que cambia es que ahora se sabe a
    # qué apunta cada frase.
    for indice, causa in enumerate(explicacion.causas):
        frase = sin_preambulo(causa.frase)
        if not frase:
            continue
        bloques.append(
            BloqueTexto(
                titulo="Qué cambió" if indice == 0 else "",
                texto=frase,
                fact_ids=[f"linea:{causa.concepto_id}.delta_cent"],
            )
        )

    if detalle:
        bloques.append(_tabla_cargos_actuales(factset))
        cuerpo = renderizar_texto_libre(datos, "detalle")
        if cuerpo:
            bloques.append(BloqueTexto(titulo="Cómo se calculó", texto=cuerpo))
        tabla = _tabla_tramos(factset, datos)
        if tabla is not None:
            bloques.append(tabla)

    if factset.deuda_anterior_cent:
        bloques.append(
            BloqueAviso(
                severidad="advertencia",
                texto=(
                    f"Tiene un saldo pendiente de {datos.deuda_anterior} de recibos anteriores. "
                    f"Sumado a este recibo, el total a pagar es {datos.total_a_pagar}."
                ),
                fact_ids=["factset:deuda_anterior_cent", "factset:total_a_pagar_cent"],
            )
        )

    if not factset.invariante.ok:
        bloques.append(
            BloqueAviso(
                severidad="critico",
                texto=(
                    "Los importes de su recibo no cierran con la diferencia calculada, "
                    "así que prefiero que lo revise un asesor antes de darle una explicación."
                ),
                fact_ids=["invariante:residual_cent"],
            )
        )

    cierre = renderizar_texto_libre(datos, "cierre")
    if cierre:
        bloques.append(BloqueTexto(texto=cierre))

    return bloques


def _pide_visual_ciclos(utterance: str) -> bool:
    normalizada = utterance.lower()
    return any(
        termino in normalizada
        for termino in (
            "prorrate", "ciclo", "cómo se calcula", "como se calcula",
            "detalle", "por qué", "por que", "subió", "subio", "bajó", "bajo",
        )
    )


def _bloque_ciclos(factset: FactSet) -> BloqueCiclos:
    """Proyecta solo hechos sellados; nunca pide al LLM fechas ni importes."""
    ciclos = [
        CicloExplicado(
            periodo=factset.periodo_previo,
            total_cent=factset.total_previo_cent,
            actual=False,
        ),
        CicloExplicado(
            periodo=factset.periodo_actual,
            total_cent=factset.total_actual_cent,
            actual=True,
            inicio=str(factset.ciclo_inicio) if factset.ciclo_inicio else None,
            cierre=str(factset.ciclo_fin) if factset.ciclo_fin else None,
            vencimiento=str(factset.fecha_vencimiento) if factset.fecha_vencimiento else None,
        ),
    ]
    hitos: list[HitoCiclo] = []
    vistos: set[tuple[str, str]] = set()
    for linea in factset.lineas:
        for tramo in linea.tramos or []:
            tipo = "suspension" if str(tramo.estado) == "SUSPENDIDO" else "prorrateo"
            clave = (str(tramo.inicio), linea.nombre_comercial)
            if clave in vistos or len(hitos) >= 8:
                continue
            vistos.add(clave)
            hitos.append(
                HitoCiclo(fecha=str(tramo.inicio), etiqueta=linea.nombre_comercial, tipo=tipo)
            )
    causas = [
        CausaVisual(
            etiqueta=causa.etiqueta_cliente,
            monto_cent=causa.monto_cent,
            participacion_bp=causa.participacion_bp,
        )
        for causa in factset.causas_agregadas[:8]
    ]
    return BloqueCiclos(
        titulo="Así cambió entre dos ciclos",
        modalidad=str(factset.modalidad_renta),
        ciclos=ciclos,
        hitos=hitos,
        causas=causas,
        fact_ids=[
            "factset:periodo_previo", "factset:total_previo_cent",
            "factset:periodo_actual", "factset:total_actual_cent",
            "factset:ciclo_inicio", "factset:ciclo_fin", "factset:fecha_vencimiento",
        ],
    )


def _tabla_cargos_actuales(factset: FactSet) -> BloqueTabla:
    """Desglose completo del recibo actual, incluidas líneas que no variaron."""
    lineas_actuales = sorted(
        (linea for linea in factset.lineas if linea.monto_actual_cent != 0),
        key=lambda linea: (linea.monto_actual_cent < 0, linea.nombre_comercial.lower()),
    )
    filas = [
        [linea.nombre_comercial, formatear_soles(linea.monto_actual_cent)]
        for linea in lineas_actuales
    ]
    fact_ids = [
        f"linea:{linea.concepto_id}.monto_actual_cent"
        for linea in lineas_actuales
    ]
    return BloqueTabla(
        titulo="Cargos de su recibo actual",
        columnas=["Concepto", "Monto"],
        filas=filas,
        nota=f"Total del recibo: {formatear_soles(factset.total_actual_cent)}.",
        fact_ids=[*fact_ids, "factset:total_actual_cent"],
    )


def _tabla_tramos(factset: FactSet, datos: DatosPlantilla) -> BloqueTabla | None:
    """Tabla de tramos: **la tabla ES la explicación** del prorrateo (sección 4.1)."""
    if not datos.tramos:
        return None
    filas = [
        [
            tramo["etiqueta"],
            str(tramo["dias"]),
            tramo["tarifa"],
            "no se cobró" if tramo["suspendido"] else tramo["prorrateado"],
        ]
        for tramo in datos.tramos
    ]
    return BloqueTabla(
        titulo="Detalle por tramos del mes",
        columnas=["Periodo", "Días", "Tarifa mensual", "Cobrado"],
        filas=filas,
        nota=f"El ciclo completo de facturación tiene {factset.dias_ciclo} días.",
        fact_ids=["factset:dias_ciclo"],
    )


def _acciones(explicacion: ExplicacionLLM, *, derivar: bool) -> list[Accion]:
    """Siguientes acciones sugeridas: la principal más las de escape."""
    principal = explicacion.siguiente_paso
    if derivar:
        principal = AccionSiguiente.DERIVAR_ASESOR

    # La acción de escape es REGISTRAR_CONSULTA, no DERIVAR_ASESOR. Antes se añadían las
    # dos y el botón «Hablar con un asesor» aparecía debajo de TODAS las respuestas,
    # incluidas las que el sistema había explicado bien: el hand-off dejaba de ser el
    # último recurso y pasaba a ser una sugerencia permanente de que la explicación no
    # servía. Cuando la derivación sí procede, `derivar` la pone como acción principal
    # dos líneas más arriba; y si el cliente pide una persona, su intención lo detecta.
    ordenadas: list[AccionSiguiente] = [principal]
    if AccionSiguiente.REGISTRAR_CONSULTA not in ordenadas:
        ordenadas.append(AccionSiguiente.REGISTRAR_CONSULTA)

    acciones: list[Accion] = []
    for identificador in ordenadas:
        etiqueta, riesgo = ETIQUETAS_ACCION[identificador]
        acciones.append(Accion(id=identificador, etiqueta=etiqueta, riesgo=riesgo))  # type: ignore[arg-type]
    return acciones


# --------------------------------------------------------------------------- #
# Generación
# --------------------------------------------------------------------------- #
def generar_explicacion(
    factset: FactSet,
    contexto_recuperado: Sequence[Any] | None = None,
    utterance: str = "",
    verbosidad: Verbosidad | str = Verbosidad.CORTO,
    proveedor: ProveedorLLM | None = None,
    *,
    canal: Canal = Canal.APP,
    estricto: bool | None = None,
    timeout_s: float | None = None,
    permitidos: ConjuntoPermitido | None = None,
    respuestas_previas: Sequence[str] | None = None,
) -> ResultadoGeneracion:
    """Genera la explicación aplicando la política LLM → verificar → reintento → plantilla.

    Args:
        factset: hechos verificados. Es la **única** fuente de cifras.
        contexto_recuperado: fragmentos del retriever (se les enmascaran las cifras).
        utterance: mensaje literal del cliente; entra al prompt como dato delimitado.
        verbosidad: ``CORTO`` o ``DETALLE``.
        proveedor: proveedor a usar; si es ``None`` se resuelve por ``LLM_MODE``.
        canal: canal de origen (APP, BOT, WHATSAPP, ASESOR).
        estricto: fuerza el modo del verificador (por defecto, ``VERIFICADOR_ESTRICTO``).
        timeout_s: timeout por llamada (por defecto, ``LLM_TIMEOUT_S``).
        permitidos: conjunto ``ALLOWED`` ya construido, para reutilizarlo entre turnos.
        respuestas_previas: lo que el asistente ya contestó antes en esta conversación
            (más reciente al final). Deja de repetir la misma redacción en la segunda
            pregunta sobre el mismo recibo — ver ``construir_prompt``.

    Returns:
        :class:`ResultadoGeneracion` con el texto final, el modo realmente usado, el
        resultado de verificación y las citas con sus offsets ``[inicio, fin)`` sobre
        el texto entregado.
    """
    arranque = time.perf_counter()
    if pide_detalle_cargos(utterance):
        verbosidad = Verbosidad.DETALLE
    datos = datos_de_factset(factset, verbosidad)
    conjunto = permitidos or construir_permitidos(factset)
    espera = float(timeout_s if timeout_s is not None else timeout_por_defecto())
    intentos: list[IntentoGeneracion] = []

    if proveedor is None:
        try:
            proveedor = obtener_proveedor()
        except ErrorProveedor as exc:
            _LOG.warning("no hay proveedor disponible (%s); se usa plantilla", exc.codigo)
            intentos.append(
                IntentoGeneracion(
                    numero=0,
                    modo=ModoGeneracion.PLANTILLA,
                    proveedor="ninguno",
                    error=exc.a_dict(),
                )
            )

    nombre_proveedor = getattr(proveedor, "nombre", "ninguno")
    version = version_modelo_de(proveedor) if proveedor is not None else "plantilla-determinista"

    correccion: str | None = None
    for numero, modo in ((1, ModoGeneracion.LLM), (2, ModoGeneracion.LLM_REINTENTO)):
        if proveedor is None:
            break
        inicio_intento = time.perf_counter()
        try:
            prompt = construir_prompt(
                factset,
                contexto_recuperado=contexto_recuperado,
                utterance=utterance,
                verbosidad=Verbosidad(str(verbosidad)),
                canal=canal,
                correccion=correccion,
                respuestas_previas=respuestas_previas,
            )
            bruto = proveedor.completar(prompt, ESQUEMA_EXPLICACION_V1, espera)
            explicacion = fijar_narrativa_de_notas(
                formatear_centimos_en_prosa(ExplicacionLLM.model_validate(bruto)),
                datos,
            )
            explicacion = enfocar_resumen_consulta(explicacion, factset, utterance)
        except ErrorProveedor as exc:
            intentos.append(
                IntentoGeneracion(
                    numero=numero,
                    modo=modo,
                    proveedor=nombre_proveedor,
                    error=exc.a_dict(),
                    latencia_ms=_ms(inicio_intento),
                )
            )
            _LOG.warning("proveedor %s falló (%s); se degrada", nombre_proveedor, exc.codigo)
            break
        except Exception as exc:
            intentos.append(
                IntentoGeneracion(
                    numero=numero,
                    modo=modo,
                    proveedor=nombre_proveedor,
                    error={"codigo": "ERROR_INESPERADO", "detalle": f"{type(exc).__name__}: {exc}"},
                    latencia_ms=_ms(inicio_intento),
                )
            )
            _LOG.exception("error inesperado del proveedor %s", nombre_proveedor)
            break

        bloques = componer_bloques(
            factset, explicacion, datos, verbosidad=verbosidad,
            mostrar_ciclos=_pide_visual_ciclos(utterance),
        )
        texto = _texto_de(bloques)
        resultado = verificar(
            texto,
            factset,
            permitidos=conjunto,
            salida_llm=explicacion,
            estricto=estricto,
        )
        intentos.append(
            IntentoGeneracion(
                numero=numero,
                modo=modo,
                proveedor=nombre_proveedor,
                veredicto=str(resultado.veredicto),
                no_ancladas=resultado.no_ancladas,
                infractores=list(resultado.infractores),
                latencia_ms=_ms(inicio_intento),
            )
        )

        if resultado.veredicto is not VeredictoVerificacion.FAIL:
            return _empaquetar(
                factset=factset,
                datos=datos,
                explicacion=explicacion,
                bloques=bloques,
                texto=texto,
                resultado=resultado,
                modo=modo,
                proveedor=nombre_proveedor,
                version=version,
                intentos=intentos,
                arranque=arranque,
            )

        # FAIL: se le dice al modelo, literal, qué números no existen.
        motivos = resultado.infractores or resultado.tokens_infractores
        correccion = mensaje_correccion(motivos) + (
            (" Además: " + "; ".join(resultado.errores_estructurales))
            if resultado.errores_estructurales
            else ""
        )
        _LOG.warning("verificación FAIL en el intento %s: %s", numero, motivos)

    # --- Ruta determinística ------------------------------------------------ #
    explicacion = formatear_centimos_en_prosa(renderizar_explicacion(datos))
    # Mismo jitter léxico que `MockProvider` (útil aquí porque esta ruta se toma
    # también cuando el proveedor configurado no está disponible de verdad —API key
    # ausente o inválida, cuota agotada—, no solo cuando el LLM falla la
    # verificación). Sin esto, cualquier pregunta sobre el mismo recibo produce el
    # mismo texto byte a byte, con o sin LLM. La semilla varía con `turno_numero`
    # igual que en el mock: en el primer turno es idéntica a como era antes de esto.
    azar = random.Random(semilla_de(datos.factset_id, len(respuestas_previas or ())))
    explicacion = explicacion.model_copy(
        update={
            "resumen": recortar_seguro(aplicar_jitter(explicacion.resumen, azar), 180),
            "causas": [
                causa.model_copy(update={"frase": aplicar_jitter(causa.frase, azar)})
                for causa in explicacion.causas
            ],
        }
    )
    # El foco se aplica al final: conserva la variación natural en las causas, pero la
    # primera frase responde exactamente al concepto pendiente incluso sin proveedor.
    explicacion = enfocar_resumen_consulta(explicacion, factset, utterance)
    bloques = componer_bloques(
        factset, explicacion, datos, verbosidad=verbosidad,
        mostrar_ciclos=_pide_visual_ciclos(utterance),
    )
    texto = _texto_de(bloques)
    resultado = verificar(
        texto, factset, permitidos=conjunto, salida_llm=explicacion, estricto=estricto
    )
    intentos.append(
        IntentoGeneracion(
            numero=len(intentos) + 1,
            modo=ModoGeneracion.PLANTILLA,
            proveedor="plantilla",
            veredicto=str(resultado.veredicto),
            no_ancladas=resultado.no_ancladas,
            infractores=list(resultado.infractores),
        )
    )

    if resultado.veredicto is VeredictoVerificacion.FAIL:
        # No debería ocurrir jamás: la plantilla solo escribe cifras del FactSet.
        # Si ocurre, hay un fallo en el motor y NO se entrega ninguna cifra.
        _LOG.error(
            "la plantilla determinística no verifica (%s); se bloquea la respuesta",
            resultado.infractores,
        )
        return _bloquear(
            factset=factset,
            resultado=resultado,
            intentos=intentos,
            arranque=arranque,
            version=version,
        )

    return _empaquetar(
        factset=factset,
        datos=datos,
        explicacion=explicacion,
        bloques=bloques,
        texto=texto,
        resultado=resultado,
        modo=ModoGeneracion.PLANTILLA,
        proveedor="plantilla",
        version="plantilla-determinista",
        intentos=intentos,
        arranque=arranque,
    )


def _ms(desde: float) -> int:
    """Milisegundos transcurridos desde una marca de ``perf_counter``."""
    return int((time.perf_counter() - desde) * 1000)


def _texto_de(bloques: Sequence[Bloque]) -> str:
    """Concatena los bloques igual que ``RespuestaCanalAgnostica.texto``.

    Debe coincidir exactamente con esa propiedad: es la superficie que se audita.
    """
    return "\n".join(bloque.a_texto() for bloque in bloques)


def _empaquetar(
    *,
    factset: FactSet,
    datos: DatosPlantilla,
    explicacion: ExplicacionLLM,
    bloques: list[Bloque],
    texto: str,
    resultado: ResultadoVerificacion,
    modo: ModoGeneracion,
    proveedor: str,
    version: str,
    intentos: list[IntentoGeneracion],
    arranque: float,
) -> ResultadoGeneracion:
    """Arma el resultado final con su gobernanza y su derivación."""
    latencia = _ms(arranque)
    derivar = not factset.invariante.ok
    gobernanza = Gobernanza(
        anclado=resultado.anclado,
        verificacion_numerica=str(resultado.veredicto),  # type: ignore[arg-type]
        aserciones_totales=resultado.aserciones_totales,
        aserciones_ancladas=resultado.ancladas + resultado.derivadas,
        aserciones_no_ancladas=resultado.no_ancladas,
        confianza=factset.confianza_global,
        modo=modo,
        rules_version=factset.rules_version,
        model_version=version,
        factset_sha256=resultado.factset_sha256,
        citas=resultado.citas,
        aserciones=resultado.aserciones,
        latencia_ms=latencia,
    )
    derivacion = Derivacion()
    if derivar:
        derivacion = Derivacion(
            requerida=True,
            motivo_codigo=MotivoDerivacion.INVARIANTE_ROTO,
            motivo="el recibo no concilia con la diferencia calculada",
            senal_disparadora=f"invariante.residual_cent={factset.invariante.residual_cent}",
        )
    return ResultadoGeneracion(
        texto=texto,
        bloques=bloques,
        acciones=_acciones(explicacion, derivar=derivar),
        modo=modo,
        verificacion=resultado,
        citas=resultado.citas,
        gobernanza=gobernanza,
        derivacion=derivacion,
        explicacion=explicacion,
        proveedor=proveedor,
        model_version=version,
        plantilla=datos.plantilla,
        latencia_ms=latencia,
        intentos=intentos,
    )


def _bloquear(
    *,
    factset: FactSet,
    resultado: ResultadoVerificacion,
    intentos: list[IntentoGeneracion],
    arranque: float,
    version: str,
) -> ResultadoGeneracion:
    """Respuesta de bloqueo: sin una sola cifra y con derivación obligatoria."""
    bloques: list[Bloque] = [
        BloqueAviso(severidad="advertencia", texto=TEXTO_BLOQUEADO),
    ]
    texto = _texto_de(bloques)
    latencia = _ms(arranque)
    gobernanza = Gobernanza(
        anclado=False,
        verificacion_numerica="FAIL",
        aserciones_totales=resultado.aserciones_totales,
        aserciones_ancladas=resultado.ancladas + resultado.derivadas,
        aserciones_no_ancladas=resultado.no_ancladas,
        confianza=0.0,
        modo=ModoGeneracion.PLANTILLA,
        rules_version=factset.rules_version,
        model_version=version,
        factset_sha256=resultado.factset_sha256,
        citas=[],
        aserciones=resultado.aserciones,
        latencia_ms=latencia,
    )
    etiqueta, riesgo = ETIQUETAS_ACCION[AccionSiguiente.DERIVAR_ASESOR]
    return ResultadoGeneracion(
        texto=texto,
        bloques=bloques,
        acciones=[Accion(id=AccionSiguiente.DERIVAR_ASESOR, etiqueta=etiqueta, riesgo=riesgo)],  # type: ignore[arg-type]
        modo=ModoGeneracion.PLANTILLA,
        verificacion=resultado,
        citas=[],
        gobernanza=gobernanza,
        derivacion=Derivacion(
            requerida=True,
            motivo_codigo=MotivoDerivacion.VERIFICACION_FALLIDA,
            motivo="no se pudo anclar toda la explicación numérica",
            senal_disparadora=f"no_ancladas={resultado.no_ancladas}",
        ),
        explicacion=None,
        proveedor="bloqueo",
        model_version=version,
        plantilla="bloqueo",
        latencia_ms=latencia,
        intentos=intentos,
        bloqueada=True,
    )


# --------------------------------------------------------------------------- #
# Fachada para la API y para el test golden
# --------------------------------------------------------------------------- #
def a_respuesta(
    resultado: ResultadoGeneracion,
    *,
    conversation_id: UUID | None = None,
    trace_id: str | None = None,
    telemetria_extra: dict[str, Any] | None = None,
) -> RespuestaCanalAgnostica:
    """Envuelve el resultado en la respuesta canal-agnóstica de la API."""
    traza = trace_id or uuid.uuid4().hex[:16]
    telemetria = resultado.telemetria()
    telemetria["silence_probe_id"] = f"sp-{traza}"
    telemetria.update(telemetria_extra or {})
    return RespuestaCanalAgnostica(
        conversation_id=conversation_id or uuid.uuid4(),
        trace_id=traza,
        bloques=resultado.bloques,
        acciones=resultado.acciones,
        derivacion=resultado.derivacion,
        gobernanza=resultado.gobernanza,
        telemetria=telemetria,
    )


def explicar(
    factset: FactSet,
    *,
    modo: str | None = None,
    verbosidad: Verbosidad | str = Verbosidad.CORTO,
    utterance: str = "",
    contexto_recuperado: Sequence[Any] | None = None,
    canal: Canal = Canal.APP,
    conversation_id: UUID | None = None,
    trace_id: str | None = None,
    estricto: bool | None = None,
) -> RespuestaCanalAgnostica:
    """Atajo de una línea: FactSet → respuesta verificada.

    Es la fachada que usan el test golden (``resp.texto`` y
    ``resp.gobernanza.verificacion_numerica``) y el endpoint ``POST /v1/explicar``.
    """
    proveedor: ProveedorLLM | None
    try:
        proveedor = obtener_proveedor(modo)
    except ErrorProveedor:
        proveedor = None
    resultado = generar_explicacion(
        factset,
        contexto_recuperado=contexto_recuperado,
        utterance=utterance,
        verbosidad=verbosidad,
        proveedor=proveedor,
        canal=canal,
        estricto=estricto,
    )
    return a_respuesta(resultado, conversation_id=conversation_id, trace_id=trace_id)
