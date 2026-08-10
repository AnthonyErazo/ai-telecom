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
    BloqueKV,
    BloquePuente,
    BloqueTabla,
    BloqueTexto,
    Cita,
    Derivacion,
    Gobernanza,
    ItemKV,
    RespuestaCanalAgnostica,
)
from packages.llm_layer.plantillas import (
    DatosPlantilla,
    datos_de_factset,
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
    "explicar",
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

#: Texto de bloqueo. No contiene ni una sola cifra, a propósito.
TEXTO_BLOQUEADO = (
    "Prefiero no darle una cifra que no pueda sustentar. Reviso su recibo con un asesor "
    "y le confirmamos el detalle exacto en unos minutos."
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
# Composición de bloques — aquí es donde se ponen las cifras
# --------------------------------------------------------------------------- #
def componer_bloques(
    factset: FactSet,
    explicacion: ExplicacionLLM,
    datos: DatosPlantilla,
    *,
    verbosidad: Verbosidad | str = Verbosidad.CORTO,
) -> list[Bloque]:
    """Construye los bloques de la respuesta.

    Del modelo se toma **solo texto**: el resumen y una frase por causa. Todos los
    importes que se muestran salen de ``formatear_soles`` sobre enteros del FactSet,
    de manera que el bloque ``kv``, el ``puente`` y la ``tabla`` son anclados por
    construcción.
    """
    detalle = str(verbosidad) == str(Verbosidad.DETALLE)
    bloques: list[Bloque] = []

    if explicacion.resumen.strip():
        bloques.append(
            BloqueTexto(
                texto=explicacion.resumen.strip(),
                fact_ids=["factset:delta_total_cent", "factset:total_actual_cent"],
            )
        )

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
    bloques.append(
        BloqueKV(
            titulo="Su recibo en números",
            items=items,
            fact_ids=["factset:total_actual_cent", "factset:total_previo_cent"],
        )
    )

    if factset.causas_agregadas:
        barras = [
            BarraPuente(
                etiqueta=f"Recibo de {datos.mes_previo}",
                monto_cent=factset.total_previo_cent,
                tipo="entrada",
                fact_id="factset:total_previo_cent",
            )
        ]
        for causa in factset.causas_agregadas:
            barras.append(
                BarraPuente(
                    etiqueta=causa.etiqueta_cliente.capitalize(),
                    monto_cent=causa.monto_cent,
                    tipo="incremento" if causa.monto_cent >= 0 else "decremento",
                    fact_id=f"causa:{causa.causa or causa.causa_oficial or 'SIN_CAUSA'}.monto_cent",
                )
            )
        barras.append(
            BarraPuente(
                etiqueta=f"Recibo de {datos.mes_actual}",
                monto_cent=factset.total_actual_cent,
                tipo="total",
                fact_id="factset:total_actual_cent",
            )
        )
        bloques.append(BloquePuente(titulo="De un mes a otro", barras=barras))

    frases = [causa.frase.strip() for causa in explicacion.causas if causa.frase.strip()]
    if frases:
        bloques.append(
            BloqueTexto(
                titulo="Qué cambió",
                texto=" ".join(frases),
                fact_ids=[f"linea:{causa.concepto_id}.delta_cent" for causa in explicacion.causas],
            )
        )

    if detalle:
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

    ordenadas: list[AccionSiguiente] = [principal]
    for candidata in (AccionSiguiente.REGISTRAR_CONSULTA, AccionSiguiente.DERIVAR_ASESOR):
        if candidata not in ordenadas:
            ordenadas.append(candidata)

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

    Returns:
        :class:`ResultadoGeneracion` con el texto final, el modo realmente usado, el
        resultado de verificación y las citas con sus offsets ``[inicio, fin)`` sobre
        el texto entregado.
    """
    arranque = time.perf_counter()
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
            )
            bruto = proveedor.completar(prompt, ESQUEMA_EXPLICACION_V1, espera)
            explicacion = ExplicacionLLM.model_validate(bruto)
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

        bloques = componer_bloques(factset, explicacion, datos, verbosidad=verbosidad)
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
    explicacion = renderizar_explicacion(datos)
    bloques = componer_bloques(factset, explicacion, datos, verbosidad=verbosidad)
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
