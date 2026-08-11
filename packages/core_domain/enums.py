"""Enumeraciones canónicas del dominio.

Todas son ``StrEnum``: serializan como texto plano en JSON y se comparan con ``str``
sin conversiones (``bloque.tipo == "texto"`` funciona igual que ``== TipoBloque.TEXTO``).

Los valores son estables: forman parte del contrato de la API y de los ficheros de
auditoría, así que **no se renombran** sin subir ``rules_version``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ETIQUETAS_CAUSA_OFICIAL",
    "MAPA_MOVIMIENTO_A_CAUSA_OFICIAL",
    "AccionSiguiente",
    "Canal",
    "CausaOficial",
    "ClaseDelta",
    "ConvencionProrrateo",
    "CorpusRag",
    "EstadoAsercion",
    "EstadoServicio",
    "EtapaAuditoria",
    "FamiliaConcepto",
    "ModalidadRenta",
    "ModoGeneracion",
    "MotivoDerivacion",
    "NivelAseguramiento",
    "RiesgoAccion",
    "SeveridadAviso",
    "TipoBarraPuente",
    "TipoBloque",
    "TipoEvidencia",
    "TipoMovimiento",
    "Verbosidad",
    "VeredictoVerificacion",
    "causa_oficial_de",
    "etiqueta_causa_oficial",
]


# --------------------------------------------------------------------------- #
# 3.1 — Enums de la especificación
# --------------------------------------------------------------------------- #
class ModalidadRenta(StrEnum):
    """Cómo se cobra la renta respecto del ciclo facturado.

    En ADELANTADA el recibo del ciclo *k* cobra la renta del ciclo *k+1* y corrige
    el ciclo *k*: por eso conviven dos rentas en el mismo documento y el recibo puede
    subir aunque el plan nuevo sea más barato.
    """

    ADELANTADA = "ADELANTADA"
    VENCIDA = "VENCIDA"


class TipoMovimiento(StrEnum):
    """Evento del historial de órdenes (Amdocs) capaz de explicar una variación."""

    CAMBIO_PLAN = "CAMBIO_PLAN"
    SUSPENSION = "SUSPENSION"
    RECONEXION = "RECONEXION"
    ALTA_SERVICIO = "ALTA_SERVICIO"
    BAJA_SERVICIO = "BAJA_SERVICIO"
    FIN_DESCUENTO = "FIN_DESCUENTO"
    ALTA_PAQUETE = "ALTA_PAQUETE"
    ALTA_EQUIPO_FINANCIADO = "ALTA_EQUIPO_FINANCIADO"
    NOTA_CREDITO = "NOTA_CREDITO"
    NOTA_DEBITO = "NOTA_DEBITO"
    AJUSTE_SUSPENSION = "AJUSTE_SUSPENSION"


class ClaseDelta(StrEnum):
    """Resultado de comparar una misma línea entre el recibo actual y el previo."""

    NUEVO = "NUEVO"
    DESAPARECIDO = "DESAPARECIDO"
    SUBIO = "SUBIO"
    BAJO = "BAJO"
    IGUAL = "IGUAL"


class FamiliaConcepto(StrEnum):
    """Naturaleza contable del concepto; decide si se prorratea y cómo se narra."""

    RECURRENTE = "RECURRENTE"
    UNICO = "UNICO"
    AJUSTE = "AJUSTE"
    FINANCIAMIENTO = "FINANCIAMIENTO"
    IMPUESTO = "IMPUESTO"
    CREDITO = "CREDITO"


class EstadoServicio(StrEnum):
    """Estado del servicio dentro de un tramo del ciclo."""

    ACTIVO = "ACTIVO"
    SUSPENDIDO = "SUSPENDIDO"


class NivelAseguramiento(StrEnum):
    """Nivel de autenticación del solicitante (LOA).

    LOA0 solo ve el catálogo; LOA1 (WhatsApp) ve la existencia y la dirección del
    cambio pero **ningún monto**; LOA2 (App) ve la explicación completa; LOA_ASESOR
    es LOA2 con ``acting_on_behalf_of`` obligatorio y registrado en auditoría.
    """

    LOA0 = "LOA0"
    LOA1 = "LOA1"
    LOA2 = "LOA2"
    LOA_ASESOR = "LOA_ASESOR"


class Verbosidad(StrEnum):
    """Longitud pedida para la explicación."""

    CORTO = "CORTO"
    DETALLE = "DETALLE"


class ModoGeneracion(StrEnum):
    """Cómo se produjo finalmente el texto entregado al cliente."""

    LLM = "LLM"
    LLM_REINTENTO = "LLM_REINTENTO"
    PLANTILLA = "PLANTILLA"


class AccionSiguiente(StrEnum):
    """Siguientes acciones recomendables (lista literal de la ficha del desafío)."""

    PAGAR = "PAGAR"
    VER_DETALLE = "VER_DETALLE"
    REGISTRAR_CONSULTA = "REGISTRAR_CONSULTA"
    VER_ALTERNATIVAS = "VER_ALTERNATIVAS"
    DERIVAR_ASESOR = "DERIVAR_ASESOR"


# --------------------------------------------------------------------------- #
# 3.2 — Las 9 causas oficiales (literal de la ficha del Desafío 1)
# --------------------------------------------------------------------------- #
class CausaOficial(StrEnum):
    """Las nueve causas de variación que enumera literalmente la ficha.

    ``TipoMovimiento`` es el vocabulario técnico del CRM; ``CausaOficial`` es el
    vocabulario del enunciado y de la evaluación. Nunca se mezclan: se traducen con
    ``causa_oficial_de``.
    """

    CAMBIO_DE_PLAN = "CAMBIO_DE_PLAN"
    EQUIPO_FINANCIADO = "EQUIPO_FINANCIADO"
    COMPRA_DE_PAQUETES = "COMPRA_DE_PAQUETES"
    CARGOS_ADICIONALES = "CARGOS_ADICIONALES"
    PROMOCIONES_VENCIDAS = "PROMOCIONES_VENCIDAS"
    NOTAS_CREDITO_DEBITO = "NOTAS_CREDITO_DEBITO"
    PRORRATEOS = "PRORRATEOS"
    RECONEXIONES = "RECONEXIONES"
    AJUSTES_POR_DIAS_DE_SUSPENSION = "AJUSTES_POR_DIAS_DE_SUSPENSION"


#: Texto en lenguaje de cliente de cada causa oficial (redacción de la ficha).
ETIQUETAS_CAUSA_OFICIAL: dict[CausaOficial, str] = {
    CausaOficial.CAMBIO_DE_PLAN: "cambio de plan",
    CausaOficial.EQUIPO_FINANCIADO: "equipo financiado",
    CausaOficial.COMPRA_DE_PAQUETES: "compra de paquetes",
    CausaOficial.CARGOS_ADICIONALES: "cargos adicionales",
    CausaOficial.PROMOCIONES_VENCIDAS: "promociones vencidas",
    CausaOficial.NOTAS_CREDITO_DEBITO: "notas de crédito/débito",
    CausaOficial.PRORRATEOS: "prorrateos",
    CausaOficial.RECONEXIONES: "reconexiones",
    CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION: "ajustes por días de suspensión",
}

#: Traducción CRM -> ficha. Es fija: forma parte del enunciado, no se configura.
MAPA_MOVIMIENTO_A_CAUSA_OFICIAL: dict[TipoMovimiento, CausaOficial] = {
    TipoMovimiento.CAMBIO_PLAN: CausaOficial.CAMBIO_DE_PLAN,
    TipoMovimiento.ALTA_EQUIPO_FINANCIADO: CausaOficial.EQUIPO_FINANCIADO,
    TipoMovimiento.ALTA_PAQUETE: CausaOficial.COMPRA_DE_PAQUETES,
    TipoMovimiento.FIN_DESCUENTO: CausaOficial.PROMOCIONES_VENCIDAS,
    TipoMovimiento.NOTA_CREDITO: CausaOficial.NOTAS_CREDITO_DEBITO,
    TipoMovimiento.NOTA_DEBITO: CausaOficial.NOTAS_CREDITO_DEBITO,
    TipoMovimiento.ALTA_SERVICIO: CausaOficial.PRORRATEOS,
    TipoMovimiento.BAJA_SERVICIO: CausaOficial.PRORRATEOS,
    TipoMovimiento.RECONEXION: CausaOficial.RECONEXIONES,
    TipoMovimiento.SUSPENSION: CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION,
    TipoMovimiento.AJUSTE_SUSPENSION: CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION,
}


def causa_oficial_de(movimiento: TipoMovimiento | None) -> CausaOficial | None:
    """Traduce un ``TipoMovimiento`` del CRM a la causa oficial de la ficha.

    Devuelve ``None`` si no hay movimiento atribuido: en ese caso la línea suele
    corresponder a ``CARGOS_ADICIONALES`` (consumo fuera de plan), pero esa decisión
    la toma la atribución mirando también la familia del concepto, no este mapa.
    """
    if movimiento is None:
        return None
    return MAPA_MOVIMIENTO_A_CAUSA_OFICIAL.get(TipoMovimiento(movimiento))


def etiqueta_causa_oficial(causa: CausaOficial | TipoMovimiento | None) -> str:
    """Devuelve la etiqueta en lenguaje de cliente de una causa (o ``"otros cargos"``)."""
    if causa is None:
        return "otros cargos"
    if isinstance(causa, TipoMovimiento) or causa in TipoMovimiento.__members__:
        oficial = causa_oficial_de(TipoMovimiento(causa))
    else:
        oficial = CausaOficial(causa)
    if oficial is None:
        return "otros cargos"
    return ETIQUETAS_CAUSA_OFICIAL[oficial]


# --------------------------------------------------------------------------- #
# Enums de presentación, gobernanza y configuración
# --------------------------------------------------------------------------- #
class TipoBloque(StrEnum):
    """Discriminador de la unión de bloques de ``RespuestaCanalAgnostica``.

    Los valores van en minúscula porque son el discriminador literal del JSON.
    """

    TEXTO = "texto"
    KV = "kv"
    PUENTE = "puente"
    TABLA = "tabla"
    AVISO = "aviso"


class TipoBarraPuente(StrEnum):
    """Rol de cada barra del gráfico puente (waterfall) previo -> actual."""

    ENTRADA = "entrada"
    INCREMENTO = "incremento"
    DECREMENTO = "decremento"
    TOTAL = "total"
    PROYECCION = "proyeccion"


class SeveridadAviso(StrEnum):
    """Severidad de un bloque de aviso."""

    INFO = "info"
    ADVERTENCIA = "advertencia"
    CRITICO = "critico"


class RiesgoAccion(StrEnum):
    """Riesgo de una acción sugerida. No existen acciones irreversibles en el MVP."""

    INFORMATIVA = "INFORMATIVA"
    REVERSIBLE = "REVERSIBLE"


class VeredictoVerificacion(StrEnum):
    """Resultado del verificador numérico sobre el texto final."""

    PASS = "PASS"
    FAIL = "FAIL"
    NO_APLICA = "NO_APLICA"


class EstadoAsercion(StrEnum):
    """Estado de una cifra encontrada en el texto generado.

    ANCLADA: el token está literalmente en el FactSet.
    DERIVADA: se obtiene del FactSet por álgebra permitida (lista cerrada, se registra).
    NO_ANCLADA: no se puede justificar -> el texto no sale, se reintenta o va a plantilla.
    """

    ANCLADA = "ANCLADA"
    DERIVADA = "DERIVADA"
    NO_ANCLADA = "NO_ANCLADA"


class Canal(StrEnum):
    """Canal por el que llega la consulta (condiciona formato y nivel de detalle)."""

    APP = "APP"
    BOT = "BOT"
    WHATSAPP = "WHATSAPP"
    ASESOR = "ASESOR"


class TipoEvidencia(StrEnum):
    """Tipo de referencia citable en ``evidencia`` y en las citas de gobernanza."""

    LINEA = "linea"
    MOVIMIENTO = "mov"
    CATALOGO = "cat"
    TRAMO = "tramo"
    FAQ = "faq"
    CASUISTICA = "casuistica"
    REGLA = "regla"
    FACTSET = "factset"


class CorpusRag(StrEnum):
    """Corpus recuperables. El recibo NO está aquí: es consulta estructurada."""

    CATALOGO = "concepto_catalogo"
    FAQ = "faq"
    CASUISTICA = "casuistica"


class EtapaAuditoria(StrEnum):
    """Etapas del pipeline que se escriben en la cadena de auditoría."""

    REQUEST = "REQUEST"
    FACTS_BUILT = "FACTS_BUILT"
    INVARIANTE = "INVARIANTE"
    RETRIEVE = "RETRIEVE"
    ROUTE = "ROUTE"
    LLM_CALL = "LLM_CALL"
    VERIFY = "VERIFY"
    CITATIONS = "CITATIONS"
    RESPONSE = "RESPONSE"
    CHAIN = "CHAIN"


class MotivoDerivacion(StrEnum):
    """Por qué se deriva a un asesor humano.

    Los cuatro primeros son reglas duras: derivan sin calcular el score.
    """

    PETICION_HUMANO = "PETICION_HUMANO"
    INVARIANTE_ROTO = "INVARIANTE_ROTO"
    CONCEPTO_FUERA_CATALOGO = "CONCEPTO_FUERA_CATALOGO"
    INTENCION_REGULATORIA = "INTENCION_REGULATORIA"
    UMBRAL_INCOMPRENSION = "UMBRAL_INCOMPRENSION"
    VERIFICACION_FALLIDA = "VERIFICACION_FALLIDA"
    NIVEL_INSUFICIENTE = "NIVEL_INSUFICIENTE"
    #: La pregunta es legítima y del ámbito de la operadora, pero **el ``FactSet`` no
    #: tiene los datos**: consumo de gigas, minutos restantes, saldo. Se distingue de
    #: ``CONCEPTO_FUERA_CATALOGO`` —donde el dato existe pero no se sabe nombrar— y de
    #: ``INTENCION_REGULATORIA``, que es un trámite. Aquí no hay defecto que corregir:
    #: hay una frontera del sistema, y decirlo es más honesto que improvisar una cifra.
    FUERA_DE_ALCANCE = "FUERA_DE_ALCANCE"


class ConvencionProrrateo(StrEnum):
    """Convención de días para el prorrateo.

    ``ACTUAL`` usa los días reales del ciclo (28/29/30/31); ``TREINTA_360`` fuerza
    meses de 30 días. **[POR VALIDAR con Movistar]**: se parametriza en ``rules.yaml``.
    """

    ACTUAL = "actual"
    TREINTA_360 = "30_360"
