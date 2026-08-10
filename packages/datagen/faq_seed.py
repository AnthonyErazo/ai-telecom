"""Preguntas frecuentes y casuísticas semilla para el índice vectorial.

Dos corpus distintos con dos usos distintos:

* **FAQ** — responde a la pregunta suelta ("¿qué es el prorrateo?"). Se recupera por
  híbrido BM25 + vectorial, filtrado por los ``concepto_id`` que trae el FactSet.
* **CASUÍSTICA** — no responde nada: describe **cómo se estructura** la explicación de
  un tipo de caso. Se recupera por **firma causal**, es decir, por el conjunto de
  causas del FactSet más la modalidad de renta más el signo del delta. Guía la
  narrativa, no aporta contenido.

La firma se construye igual que ``FactSet.firma_causal()``, que es lo que el
recuperador tendrá en la mano en tiempo de ejecución::

    "CAMBIO_PLAN#ADELANTADA#+"
    "FIN_DESCUENTO|SUSPENSION#VENCIDA#+"
    "SIN_CAUSA#VENCIDA#0"

Como en ``catalogo_seed``, **ningún texto contiene cifras**. Una casuística que dijera
"por ejemplo, un ajuste de tanto" metería en el prompt un número que no es de este
cliente. Las casuísticas hablan de estructura y de orden, nunca de montos.

Dos registros distintos en la misma entrada
-------------------------------------------
En una FAQ, la **pregunta y la respuesta no se escriben igual**, y eso es deliberado:

* La **pregunta** está escrita como la escribe un cliente en un chat: en minúsculas,
  casi sin tildes, con "q", "xq", "pq", "xfa", "porfa" y con las formas con las que la
  gente cuenta el problema en Perú — *me llegó más caro*, *no me cuadra*, *me cobraron
  de más*, *se me venció la promoción*, *me cortaron el servicio*, *ya cancelé mi
  recibo* (que aquí, como en toda Lima, significa que **ya lo pagó**). El corpus se
  recupera con BM25 sobre lo que el cliente teclea; si las preguntas estuvieran
  redactadas como las imagina un redactor, el índice léxico buscaría un vocabulario que
  nadie usa y la recuperación fallaría justo en las consultas reales.
* La **respuesta** está escrita con corrección y cuidado, porque es lo que se le
  devuelve a una persona que ya está molesta. De usted, sin jerga, y con el mismo
  vocabulario del recibo: se dice *recibo* y nunca *factura*, y *cancelar* no se usa
  jamás con el sentido de anular.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import Canal, CausaOficial, ModalidadRenta, TipoMovimiento

__all__ = [
    "MINIMO_CASUISTICAS",
    "MINIMO_FAQS",
    "CasuisticaSeed",
    "FaqSeed",
    "construir_casuisticas",
    "construir_faqs",
    "escribir_casuisticas",
    "escribir_faqs",
    "firma_causal",
    "validar_sin_cifras",
]

#: Mínimos exigidos por la especificación (sección 8).
MINIMO_FAQS = 30
MINIMO_CASUISTICAS = 15

_PATRON_CIFRA = re.compile(r"[0-9]|S/|%")

SignoDelta = Literal["+", "-", "0"]


def firma_causal(
    causas: list[TipoMovimiento] | tuple[TipoMovimiento, ...],
    modalidad: ModalidadRenta,
    signo: SignoDelta,
) -> str:
    """Firma causal en el mismo formato que ``FactSet.firma_causal()``.

    Causas ordenadas alfabéticamente y unidas por barra vertical, modalidad de renta y
    signo del delta total, separados por almohadilla. Sin causas se usa ``SIN_CAUSA``,
    que es el caso de la deuda arrastrada y del recibo estable.
    """
    ordenadas = sorted({str(causa) for causa in causas})
    return f"{'|'.join(ordenadas) or 'SIN_CAUSA'}#{modalidad}#{signo}"


class FaqSeed(BaseModel):
    """Pregunta frecuente anonimizada, en el lenguaje con el que pregunta el cliente.

    ``pregunta`` va en registro de chat (minúsculas, abreviaturas, tildes ausentes) y
    ``respuesta`` en registro cuidado. Ambas se indexan juntas: la primera es la que
    hace que BM25 encuentre la entrada, la segunda es la que se lee.
    """

    model_config = ConfigDict(extra="forbid")

    faq_id: str
    pregunta: str = Field(description="Tal como la escribiría el cliente en un chat")
    respuesta: str = Field(description="Correcta y cuidada: es lo que lee una persona")
    conceptos: list[str] = Field(default_factory=list, description="concepto_id relacionados")
    causas_oficiales: list[CausaOficial] = Field(default_factory=list)
    canales: list[Canal] = Field(default_factory=lambda: [Canal.APP, Canal.BOT, Canal.WHATSAPP])
    etiquetas: list[str] = Field(default_factory=list)

    @property
    def texto_indexable(self) -> str:
        """Texto que va al índice léxico y al vectorial."""
        return f"{self.pregunta} {self.respuesta} {' '.join(self.etiquetas)}"


class CasuisticaSeed(BaseModel):
    """Patrón narrativo de un tipo de caso, recuperable por firma causal.

    No contiene la explicación: contiene el **orden** en que hay que explicar, qué hay
    que decir primero para desactivar la sensación de cobro indebido, y qué error
    típico hay que evitar. El contenido numérico siempre viene del FactSet.
    """

    model_config = ConfigDict(extra="forbid")

    casuistica_id: str
    titulo: str
    causas: list[TipoMovimiento] = Field(default_factory=list)
    modalidad: ModalidadRenta
    signo_delta: SignoDelta
    firma: str = ""
    situacion: str = Field(description="Qué le pasó al cliente, sin cifras")
    estructura: list[str] = Field(description="Pasos ordenados de la explicación")
    guia_narrativa: str = Field(description="Tono y foco de la respuesta")
    error_frecuente: str = Field(description="El error de explicación que hay que evitar")
    accion_sugerida: str = ""
    conceptos: list[str] = Field(default_factory=list)

    def model_post_init(self, _contexto: object) -> None:
        """Deriva la firma causal si no se pasó explícitamente."""
        if not self.firma:
            self.firma = firma_causal(self.causas, self.modalidad, self.signo_delta)

    @property
    def texto_indexable(self) -> str:
        """Texto que va al índice vectorial de casuísticas."""
        return " ".join([self.titulo, self.situacion, *self.estructura, self.guia_narrativa])


# --------------------------------------------------------------------------- #
# FAQ
# --------------------------------------------------------------------------- #
def construir_faqs() -> list[FaqSeed]:
    """Corpus de preguntas frecuentes, en el registro con el que pregunta el cliente.

    Raises:
        ValueError: si el corpus no llega al mínimo exigido por la especificación.
    """
    faqs = [
        FaqSeed(
            faq_id="FAQ_POR_QUE_MAS_CARO",
            pregunta="xq mi recibo vino mas caro este mes? no me cuadra",
            respuesta=(
                "Un recibo le llega más caro por un motivo concreto y casi siempre "
                "identificable: un cambio de plan a mitad de mes, una promoción que se le "
                "venció, la cuota de un equipo que compró en cuotas, un paquete que "
                "activó, un cargo de reconexión o un consumo fuera de su plan. En el "
                "detalle puede comparar línea por línea con el mes pasado y ver "
                "exactamente cuál de esos conceptos se movió."
            ),
            conceptos=["RENTA_PLAN_MOVIL", "DESCUENTO_PROMOCIONAL", "CUOTA_EQUIPO_FINANCIADO"],
            causas_oficiales=[CausaOficial.CAMBIO_DE_PLAN, CausaOficial.PROMOCIONES_VENCIDAS],
            etiquetas=["variación", "recibo alto", "comparación", "me llego caro", "no me cuadra"],
        ),
        FaqSeed(
            faq_id="FAQ_QUE_ES_PRORRATEO",
            pregunta="q es el prorrateo q me estan cobrando",
            respuesta=(
                "Es el cobro proporcional a los días. Su recibo no cobra meses de "
                "calendario, cobra su ciclo de facturación. Si algo cambió en medio del "
                "ciclo, no se cobra el mes entero de cada plan: se cobra la parte de días "
                "que le corresponde a cada uno, y la suma de las partes equivale a un mes "
                "completo."
            ),
            conceptos=["PRORRATEO_PLAN", "RENTA_PLAN_MOVIL"],
            causas_oficiales=[CausaOficial.PRORRATEOS],
            etiquetas=["prorrateo", "días", "proporcional"],
        ),
        FaqSeed(
            faq_id="FAQ_DOS_RENTAS_MISMO_RECIBO",
            pregunta="pq me sale dos veces el cobro de mi plan en el mismo recibo",
            respuesta=(
                "Porque su plan se cobra por adelantado. El recibo cobra el mes que viene y, "
                "además, corrige el mes que ya se le había cobrado si algo cambió en medio. "
                "Por eso conviven la renta del mes siguiente y el ajuste del mes anterior. "
                "No es un cobro duplicado: son dos periodos distintos."
            ),
            conceptos=["RENTA_PLAN_MOVIL", "AJUSTE_RETROACTIVO_RENTA"],
            causas_oficiales=[CausaOficial.PRORRATEOS, CausaOficial.CAMBIO_DE_PLAN],
            etiquetas=["renta adelantada", "ajuste", "doble cobro"],
        ),
        FaqSeed(
            faq_id="FAQ_PLAN_MAS_BARATO_RECIBO_SUBE",
            pregunta="cambie a un plan mas barato y me llego mas caro, xq?",
            respuesta=(
                "Puede pasar por dos motivos. El primero es que su plan se cobre por "
                "adelantado: el recibo trae el mes nuevo y el ajuste de los días del mes en "
                "curso. El segundo, y el más frecuente, es que el descuento que tenía "
                "estaba atado al plan anterior: al cambiar de plan el descuento deja de "
                "aplicarse, y aunque el precio de lista sea menor, lo que usted pagaba "
                "antes ya venía rebajado."
            ),
            conceptos=["RENTA_PLAN_MOVIL", "DESCUENTO_PROMOCIONAL", "AJUSTE_RETROACTIVO_RENTA"],
            causas_oficiales=[CausaOficial.CAMBIO_DE_PLAN, CausaOficial.PROMOCIONES_VENCIDAS],
            etiquetas=["cambio de plan", "descuento perdido", "renta adelantada", "me llego mas caro"],
        ),
        FaqSeed(
            faq_id="FAQ_FIN_PROMOCION",
            pregunta="xq ya no me aplican mi descuento? se me vencio la promocion?",
            respuesta=(
                "Las promociones se contratan con una duración pactada. Cuando llega su "
                "fecha de fin, no se le cobra nada nuevo: simplemente deja de restarse el "
                "descuento y su recibo vuelve al precio de lista. Por eso el recibo sube sin "
                "que aparezca ninguna línea nueva."
            ),
            conceptos=["DESCUENTO_PROMOCIONAL"],
            causas_oficiales=[CausaOficial.PROMOCIONES_VENCIDAS],
            etiquetas=["promoción", "descuento", "vencimiento", "se me vencio la promocion"],
        ),
        FaqSeed(
            faq_id="FAQ_DESCUENTO_MENOR",
            pregunta="mi descuento salio por menos de lo normal, me cobraron de mas?",
            respuesta=(
                "No es un error. Si su promoción venció a mitad del ciclo, ese mes el "
                "descuento se aplica solo por los días en que estuvo vigente. Del mes "
                "siguiente en adelante ya no aparecerá."
            ),
            conceptos=["DESCUENTO_PROMOCIONAL", "PRORRATEO_PLAN"],
            causas_oficiales=[CausaOficial.PROMOCIONES_VENCIDAS, CausaOficial.PRORRATEOS],
            etiquetas=["descuento parcial", "promoción", "prorrateo"],
        ),
        FaqSeed(
            faq_id="FAQ_CUOTA_EQUIPO",
            pregunta="q es la cuota de equipo q me cobran",
            respuesta=(
                "Es la cuota mensual del equipo que compró en cuotas. Se cobra junto con su "
                "servicio pero es un pago aparte, por el equipo. El recibo le indica en qué "
                "número de cuota va y cuántas le faltan; cuando paga la última, esa línea "
                "desaparece y su recibo baja de forma permanente."
            ),
            conceptos=["CUOTA_EQUIPO_FINANCIADO"],
            causas_oficiales=[CausaOficial.EQUIPO_FINANCIADO],
            etiquetas=["equipo", "cuotas", "financiamiento"],
        ),
        FaqSeed(
            faq_id="FAQ_CUOTA_NO_PRORRATEA",
            pregunta="compre mi celular a fin de mes, xq me cobran la cuota completa",
            respuesta=(
                "La cuota del equipo no se reparte por días: no es un servicio, es el pago "
                "de un bien. Se cobra completa desde el primer recibo, aunque haya comprado "
                "el equipo el último día de su ciclo, y termina cuando paga la última cuota."
            ),
            conceptos=["CUOTA_EQUIPO_FINANCIADO"],
            causas_oficiales=[CausaOficial.EQUIPO_FINANCIADO],
            etiquetas=["equipo", "cuota completa", "sin prorrateo"],
        ),
        FaqSeed(
            faq_id="FAQ_CUANDO_TERMINA_CUOTA",
            pregunta="cuando dejo de pagar la cuota de mi celular",
            respuesta=(
                "En el recibo aparece en qué número de cuota va y cuántas cuotas tiene el "
                "financiamiento. Al pagar la última, esa línea deja de aparecer y su recibo "
                "baja de forma permanente."
            ),
            conceptos=["CUOTA_EQUIPO_FINANCIADO"],
            causas_oficiales=[CausaOficial.EQUIPO_FINANCIADO],
            etiquetas=["equipo", "última cuota", "fin de financiamiento"],
        ),
        FaqSeed(
            faq_id="FAQ_INTERES_FINANCIAMIENTO",
            pregunta="xq mi cuota tiene intereses si era sin intereses",
            respuesta=(
                "Si su financiamiento se contrató con intereses, la cuota se compone de dos "
                "partes: la que va al precio del equipo y la que corresponde al interés. La "
                "parte de interés va bajando mes a mes. Si compró en cuotas sin intereses, "
                "esta línea no aparece en su recibo."
            ),
            conceptos=["INTERES_FINANCIAMIENTO", "CUOTA_EQUIPO_FINANCIADO"],
            causas_oficiales=[CausaOficial.EQUIPO_FINANCIADO],
            etiquetas=["intereses", "financiamiento"],
        ),
        FaqSeed(
            faq_id="FAQ_CARGO_RECONEXION",
            pregunta="q es el cargo de reconexion q me estan cobrando",
            respuesta=(
                "Es un cobro único por volver a activar su servicio después de una "
                "suspensión. Es un monto fijo: no depende de cuántos días estuvo "
                "suspendido ni de su plan, y se cobra una sola vez, en el recibo del mes en "
                "que se reactivó el servicio."
            ),
            conceptos=["CARGO_RECONEXION"],
            causas_oficiales=[CausaOficial.RECONEXIONES],
            etiquetas=["reconexión", "reactivación", "corte", "me cortaron el servicio"],
        ),
        FaqSeed(
            faq_id="FAQ_DIAS_SIN_SERVICIO",
            pregunta="estuve varios dias sin servicio, me van a devolver esa plata?",
            respuesta=(
                "Los días en que el servicio estuvo suspendido no se cobran. Si su renta se "
                "cobra por adelantado, esos días ya estaban pagados, así que la devolución "
                "aparece como una línea que resta en el recibo siguiente. Si su renta se "
                "cobra vencida, la renta de ese mes ya viene calculada solo por los días con "
                "servicio."
            ),
            conceptos=["AJUSTE_DIAS_SUSPENSION", "RENTA_PLAN_MOVIL"],
            causas_oficiales=[CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION],
            etiquetas=["suspensión", "devolución", "días sin servicio"],
        ),
        FaqSeed(
            faq_id="FAQ_RECONEXION_Y_DEVOLUCION",
            pregunta="me devolvieron los dias sin servicio pero igual me llego mas caro, xq",
            respuesta=(
                "Porque son dos cosas distintas en el mismo recibo. Por un lado se le "
                "descuentan los días en que no tuvo servicio; por otro se le cobra el cargo "
                "único de reconexión. Cuando el cargo de reconexión es mayor que la "
                "devolución, el recibo sube aunque le hayan devuelto los días."
            ),
            conceptos=["CARGO_RECONEXION", "AJUSTE_DIAS_SUSPENSION"],
            causas_oficiales=[
                CausaOficial.RECONEXIONES,
                CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION,
            ],
            etiquetas=["reconexión", "suspensión", "recibo alto"],
        ),
        FaqSeed(
            faq_id="FAQ_POR_QUE_ME_CORTARON",
            pregunta="pq me cortaron el servicio",
            respuesta=(
                "El corte por deuda se aplica cuando un recibo queda sin pagar pasada su "
                "fecha de vencimiento y el plazo de gracia. Apenas se pone al día el "
                "servicio se reactiva, y en el recibo siguiente verá el ajuste por los días "
                "sin servicio y el cargo de reconexión."
            ),
            conceptos=["AJUSTE_DIAS_SUSPENSION", "CARGO_RECONEXION", "DEUDA_ANTERIOR"],
            causas_oficiales=[CausaOficial.AJUSTES_POR_DIAS_DE_SUSPENSION],
            etiquetas=["suspensión", "morosidad", "corte"],
        ),
        FaqSeed(
            faq_id="FAQ_PAQUETE_DATOS",
            pregunta="q es el paquete de datos q me estan cobrando",
            respuesta=(
                "Es la compra de gigas adicionales que hizo durante el mes, aparte de los "
                "que ya incluye su plan. Es una compra puntual: se cobra completa en el "
                "recibo del mes en que la activó y no se repite al mes siguiente, salvo que "
                "vuelva a comprarla."
            ),
            conceptos=["PAQUETE_DATOS_ADICIONAL"],
            causas_oficiales=[CausaOficial.COMPRA_DE_PAQUETES],
            etiquetas=["paquete", "gigas", "datos"],
        ),
        FaqSeed(
            faq_id="FAQ_NO_RECUERDO_PAQUETE",
            pregunta="no recuerdo haber comprado ningun paquete, q hago",
            respuesta=(
                "En el detalle del recibo aparece la fecha en que se activó el paquete y el "
                "canal por el que se compró. Si con ese dato usted no lo reconoce, se puede "
                "registrar su consulta y derivarla a un asesor con todo el contexto, sin que "
                "tenga que volver a explicar el caso desde cero."
            ),
            conceptos=["PAQUETE_DATOS_ADICIONAL", "PAQUETE_ROAMING"],
            causas_oficiales=[CausaOficial.COMPRA_DE_PAQUETES],
            etiquetas=["desconocimiento", "reclamo", "derivación", "me cobraron de mas"],
        ),
        FaqSeed(
            faq_id="FAQ_ROAMING",
            pregunta="xq me cobran roaming si ya volvi del viaje",
            respuesta=(
                "Es el paquete que activó para usar su línea fuera del país. Cubre el uso "
                "durante la vigencia del paquete y se cobra en el recibo del mes del viaje. "
                "No se renueva solo."
            ),
            conceptos=["PAQUETE_ROAMING"],
            causas_oficiales=[CausaOficial.COMPRA_DE_PAQUETES],
            etiquetas=["roaming", "viaje", "extranjero"],
        ),
        FaqSeed(
            faq_id="FAQ_CANALES_PREMIUM",
            pregunta="xq el primer cobro de mis canales salio mas bajo",
            respuesta=(
                "Porque el primer mes se le cobra solo desde el día en que contrató los "
                "canales, no el mes completo. A partir del mes siguiente verá el monto "
                "mensual normal."
            ),
            conceptos=["PAQUETE_TV_PREMIUM"],
            causas_oficiales=[CausaOficial.COMPRA_DE_PAQUETES, CausaOficial.PRORRATEOS],
            etiquetas=["canales", "primer cobro", "prorrateo"],
        ),
        FaqSeed(
            faq_id="FAQ_NOTA_CREDITO",
            pregunta="q es una nota de credito",
            respuesta=(
                "Es un documento que resta de su recibo. Se emite para devolverle un cobro "
                "que no correspondía o para aplicar una compensación. Es un documento "
                "tributario, por eso aparece con su propio número y no como un descuento "
                "comercial."
            ),
            conceptos=["NOTA_CREDITO"],
            causas_oficiales=[CausaOficial.NOTAS_CREDITO_DEBITO],
            etiquetas=["nota de crédito", "devolución", "compensación"],
        ),
        FaqSeed(
            faq_id="FAQ_NOTA_DEBITO",
            pregunta="q es una nota de debito",
            respuesta=(
                "Es un documento que suma a su recibo. Corrige hacia arriba una facturación "
                "anterior: se refiere a un consumo o cargo de un periodo pasado que no se "
                "había facturado, no al mes que usted está leyendo."
            ),
            conceptos=["NOTA_DEBITO"],
            causas_oficiales=[CausaOficial.NOTAS_CREDITO_DEBITO],
            etiquetas=["nota de débito", "regularización"],
        ),
        FaqSeed(
            faq_id="FAQ_RECIBO_MAS_BARATO",
            pregunta="este mes mi recibo vino mas barato, esta bien o me lo cobran despues",
            respuesta=(
                "Puede ser por una nota de crédito aplicada a su cuenta, por la devolución "
                "de días sin servicio, porque terminó de pagar la cuota de su equipo o "
                "porque este mes no compró paquetes adicionales. En el detalle puede ver "
                "qué línea bajó respecto del mes anterior."
            ),
            conceptos=["NOTA_CREDITO", "CUOTA_EQUIPO_FINANCIADO", "AJUSTE_DIAS_SUSPENSION"],
            causas_oficiales=[CausaOficial.NOTAS_CREDITO_DEBITO],
            etiquetas=["recibo bajo", "variación"],
        ),
        FaqSeed(
            faq_id="FAQ_DEUDA_ANTERIOR",
            pregunta="q es el saldo anterior q me sale en el recibo",
            respuesta=(
                "Es lo que quedó pendiente de recibos anteriores. No es un cobro nuevo de "
                "este mes: se arrastra hasta que se paga. Por eso su consumo puede ser el "
                "mismo del mes pasado y aun así el monto a pagar sube."
            ),
            conceptos=["DEUDA_ANTERIOR"],
            causas_oficiales=[],
            etiquetas=["deuda", "saldo anterior", "pendiente", "cuanto tengo que pagar"],
        ),
        FaqSeed(
            faq_id="FAQ_YA_PAGUE",
            pregunta="ya cancele mi recibo y todavia me sale deuda, q paso",
            respuesta=(
                "El pago puede demorar algunos días en verse reflejado, según el medio con "
                "el que usted haya pagado. Si ya pasó ese plazo y la deuda sigue "
                "apareciendo, se registra su consulta y se pasa a un asesor con el "
                "detalle del recibo y del saldo a la vista."
            ),
            conceptos=["DEUDA_ANTERIOR"],
            causas_oficiales=[],
            etiquetas=["pago", "deuda", "derivación", "ya cancele", "ya pague"],
        ),
        FaqSeed(
            faq_id="FAQ_INTERES_MORATORIO",
            pregunta="xq me cobran intereses si ya pague",
            respuesta=(
                "El interés moratorio se genera cuando un recibo se paga después de su "
                "fecha de vencimiento o sigue pendiente. Se calcula sobre el saldo impago y "
                "deja de generarse en cuanto usted regulariza el pago."
            ),
            conceptos=["INTERES_MORATORIO", "DEUDA_ANTERIOR"],
            causas_oficiales=[CausaOficial.CARGOS_ADICIONALES],
            etiquetas=["mora", "intereses", "vencimiento"],
        ),
        FaqSeed(
            faq_id="FAQ_IGV",
            pregunta="q es el impuesto q me sale en el recibo",
            respuesta=(
                "Es el impuesto general a las ventas, un tributo de ley que se aplica sobre "
                "los servicios afectos del recibo. No es un cobro de Movistar. Como se "
                "calcula sobre esa base, cuando sus servicios suben o bajan, el impuesto "
                "acompaña el movimiento."
            ),
            conceptos=["IGV"],
            causas_oficiales=[],
            etiquetas=["impuesto", "igv", "tributo"],
        ),
        FaqSeed(
            faq_id="FAQ_LLAMADAS_FUERA_PLAN",
            pregunta="q son las llamadas fuera de mi plan",
            respuesta=(
                "Son las llamadas que hizo por encima de la bolsa de minutos que incluye su "
                "plan, o a destinos que su plan no cubre. Se tarifican aparte, según el "
                "tiempo hablado y el destino."
            ),
            conceptos=["LLAMADAS_FUERA_DE_PLAN"],
            causas_oficiales=[CausaOficial.CARGOS_ADICIONALES],
            etiquetas=["llamadas", "minutos", "consumo"],
        ),
        FaqSeed(
            faq_id="FAQ_DATOS_FUERA_PLAN",
            pregunta="xq me cobran datos si tengo plan con gigas",
            respuesta=(
                "Cuando se agotan los gigas que incluye su plan y usted sigue navegando sin "
                "comprar un paquete, el consumo adicional se tarifica aparte. Aparece como "
                "una línea de datos fuera de plan."
            ),
            conceptos=["CONSUMO_DATOS_ADICIONAL"],
            causas_oficiales=[CausaOficial.CARGOS_ADICIONALES],
            etiquetas=["datos", "gigas", "consumo"],
        ),
        FaqSeed(
            faq_id="FAQ_SMS_PREMIUM",
            pregunta="q son esos mensajes premium q me cobran, yo no me suscribi a nada",
            respuesta=(
                "Son mensajes a números especiales o a servicios de contenido. Los cobra el "
                "proveedor del contenido y Movistar los traslada a su recibo. Suelen venir "
                "de suscripciones que se activan desde el celular y pueden darse de baja."
            ),
            conceptos=["SMS_PREMIUM"],
            causas_oficiales=[CausaOficial.CARGOS_ADICIONALES],
            etiquetas=["mensajes", "suscripción", "contenido"],
        ),
        FaqSeed(
            faq_id="FAQ_CICLO_FACTURACION",
            pregunta="xq mi recibo no va del primero al ultimo dia del mes",
            respuesta=(
                "Porque su facturación sigue un ciclo propio, que empieza siempre el mismo "
                "día de cada mes. Ese ciclo es el que define qué consumos y qué días de "
                "servicio entran en cada recibo."
            ),
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN"],
            causas_oficiales=[CausaOficial.PRORRATEOS],
            etiquetas=["ciclo", "periodo", "fechas"],
        ),
        FaqSeed(
            faq_id="FAQ_RENTA_ADELANTADA_VENCIDA",
            pregunta="q diferencia hay entre renta adelantada y renta vencida",
            respuesta=(
                "Con renta adelantada usted paga por anticipado el mes que viene, y por eso "
                "los cambios de mitad de mes aparecen como un ajuste en el recibo siguiente. "
                "Con renta vencida usted paga el mes que acaba de cerrar, así que los "
                "cambios ya vienen calculados dentro de la propia renta."
            ),
            conceptos=["RENTA_PLAN_MOVIL", "AJUSTE_RETROACTIVO_RENTA"],
            causas_oficiales=[CausaOficial.PRORRATEOS],
            etiquetas=["modalidad", "adelantada", "vencida"],
        ),
        FaqSeed(
            faq_id="FAQ_MOVISTAR_TOTAL",
            pregunta="xq mi recibo tiene menos lineas desde q tengo movistar total",
            respuesta=(
                "Porque el paquete reúne sus servicios en un solo plan y una sola renta. "
                "Sigue teniendo los mismos servicios, pero se cobran juntos en lugar de "
                "cobrarse cada uno por su lado."
            ),
            conceptos=["RENTA_MOVISTAR_TOTAL", "DESCUENTO_MOVISTAR_TOTAL"],
            causas_oficiales=[CausaOficial.CAMBIO_DE_PLAN],
            etiquetas=["movistar total", "convergente", "paquete"],
        ),
        FaqSeed(
            faq_id="FAQ_ALQUILER_EQUIPOS",
            pregunta="xq pago alquiler de equipos si ya pago el servicio",
            respuesta=(
                "El decodificador y el router siguen siendo de Movistar y usted paga por "
                "usarlos, aparte del servicio. Si devuelve un equipo, ese cobro deja de "
                "aparecer desde el ciclo siguiente."
            ),
            conceptos=["ALQUILER_EQUIPO_HOGAR"],
            causas_oficiales=[],
            etiquetas=["equipos", "router", "decodificador"],
        ),
        FaqSeed(
            faq_id="FAQ_DONDE_VEO_DETALLE",
            pregunta="donde veo el detalle de mi recibo xfa",
            respuesta=(
                "En la app puede abrir el detalle del recibo y ver cada concepto por "
                "separado, con la comparación contra el mes anterior. Desde el chat también "
                "se le puede resumir qué cambió y por qué."
            ),
            conceptos=[],
            causas_oficiales=[],
            canales=[Canal.APP, Canal.BOT, Canal.WHATSAPP],
            etiquetas=["detalle", "app", "consulta"],
        ),
        FaqSeed(
            faq_id="FAQ_HABLAR_CON_ASESOR",
            pregunta="quiero hablar con una persona porfa, no con un bot",
            respuesta=(
                "Claro que sí. Lo pasamos con un asesor y le llevamos el contexto de esta "
                "conversación, con el detalle de su recibo y lo que ya se revisó, para que "
                "usted no tenga que explicarlo todo de nuevo."
            ),
            conceptos=[],
            causas_oficiales=[],
            canales=[Canal.APP, Canal.BOT, Canal.WHATSAPP, Canal.ASESOR],
            etiquetas=["derivación", "asesor", "humano"],
        ),
        FaqSeed(
            faq_id="FAQ_RECLAMO_FORMAL",
            pregunta="quiero poner un reclamo formal por mi recibo",
            respuesta=(
                "Un reclamo formal se registra con un asesor, que le entrega el código de "
                "atención correspondiente. Se le deriva con el contexto de su consulta y el "
                "detalle del recibo ya cargado."
            ),
            conceptos=[],
            causas_oficiales=[],
            canales=[Canal.APP, Canal.BOT, Canal.WHATSAPP, Canal.ASESOR],
            etiquetas=["reclamo", "derivación", "regulatorio"],
        ),
        FaqSeed(
            faq_id="FAQ_NO_ENTIENDO_NADA",
            pregunta="no entiendo nada de mi recibo, expliquemelo simple porfa",
            respuesta=(
                "Se lo resumimos en tres partes: cuánto pagó el mes pasado, qué se movió y "
                "cuánto paga ahora. De lo que se movió, le señalamos el concepto concreto y "
                "el motivo. Si después de eso sigue sin cuadrarle, lo pasamos con un asesor "
                "con todo el contexto."
            ),
            conceptos=[],
            causas_oficiales=[],
            etiquetas=["lenguaje simple", "resumen", "comprensión"],
        ),
    ]
    if len(faqs) < MINIMO_FAQS:
        raise ValueError(
            f"el corpus de FAQ tiene {len(faqs)} entradas y la especificación exige al "
            f"menos {MINIMO_FAQS}"
        )
    return faqs


# --------------------------------------------------------------------------- #
# Casuísticas
# --------------------------------------------------------------------------- #
def construir_casuisticas() -> list[CasuisticaSeed]:
    """Patrones narrativos indexados por firma causal.

    Cubren los ocho escenarios del generador en ambas modalidades de renta, más los
    casos compuestos, el caso sin causa atribuible y el caso de delta cero.

    Raises:
        ValueError: si el corpus no llega al mínimo exigido por la especificación.
    """
    casuisticas = [
        CasuisticaSeed(
            casuistica_id="CAS_CAMBIO_PLAN_VENCIDA_SUBE",
            titulo="Cambio de plan a mitad de ciclo con renta vencida, el recibo sube",
            causas=[TipoMovimiento.CAMBIO_PLAN],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "El cliente mejoró su plan en medio del ciclo y ve la renta con un monto "
                "que no coincide con ninguno de los dos planes."
            ),
            estructura=[
                "Confirmar de entrada que el recibo subió y por cuánto, sin rodeos.",
                "Nombrar la causa única: el cambio de plan, con la fecha en que ocurrió.",
                "Mostrar la tabla de tramos: qué plan rigió en qué días del ciclo.",
                "Señalar que la suma de las dos partes equivale a un mes completo.",
                "Anticipar el mes siguiente: la renta será la del plan nuevo, completa.",
            ],
            guia_narrativa=(
                "El cliente no discute el precio del plan, discute un monto que no "
                "reconoce. Lo que resuelve la consulta es la tabla de días, no la fórmula."
            ),
            error_frecuente=(
                "Explicar el prorrateo con lenguaje de facturación antes de decir qué pasó. "
                "Primero el hecho, después el mecanismo."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_CAMBIO_PLAN_VENCIDA_BAJA",
            titulo="Cambio a un plan menor con renta vencida, el recibo baja",
            causas=[TipoMovimiento.CAMBIO_PLAN],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="-",
            situacion=(
                "El cliente redujo su plan y quiere confirmar que el cobro ya refleja el cambio."
            ),
            estructura=[
                "Confirmar que el recibo bajó y desde qué fecha rige el plan nuevo.",
                "Mostrar los dos tramos y aclarar que este mes todavía es mixto.",
                "Indicar que el mes siguiente ya vendrá con la renta del plan nuevo entera.",
                "Cerrar recordando qué beneficios conserva con su plan actual.",
            ],
            guia_narrativa=(
                "Es una consulta de confirmación, no de queja. La respuesta debe ser corta y "
                "terminar en certeza sobre el mes siguiente."
            ),
            error_frecuente=(
                "Aprovechar para ofrecer un plan superior. El cliente acaba de reducir su "
                "plan: no hay regla de negocio que habilite una oferta aquí."
            ),
            accion_sugerida="PAGAR",
            conceptos=["RENTA_PLAN_MOVIL", "PRORRATEO_PLAN"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_CAMBIO_PLAN_ADELANTADA_SUBE",
            titulo="Cambio de plan con renta adelantada: dos rentas en el mismo recibo",
            causas=[TipoMovimiento.CAMBIO_PLAN],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "El cliente cambió de plan en medio del ciclo y su recibo trae la renta del "
                "mes siguiente más un ajuste del mes que ya había pagado. Cree que le "
                "cobraron dos veces."
            ),
            estructura=[
                "Desactivar primero el miedo al doble cobro: son dos periodos distintos.",
                "Separar las dos líneas: la renta por adelantado del mes que viene y el "
                "ajuste de los días del mes en curso.",
                "Explicar por qué existe el ajuste: el mes en curso ya estaba pagado con el "
                "plan anterior.",
                "Decir explícitamente que el recibo del mes siguiente vuelve a tener una "
                "sola renta.",
            ],
            guia_narrativa=(
                "Es el caso que más desconfianza genera. La palabra clave es adelantado: si "
                "el cliente entiende que paga por anticipado, el resto se entiende solo. "
                "Háblele de usted y con sus mismas palabras: le llegó más caro, le "
                "cobraron de más. Nada de jerga de facturación."
            ),
            error_frecuente=(
                "Presentar el ajuste como un cargo nuevo. No es un cargo: es la corrección "
                "de algo ya cobrado."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=["RENTA_PLAN_MOVIL", "AJUSTE_RETROACTIVO_RENTA"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_CAMBIO_PLAN_ADELANTADA_PIERDE_DESCUENTO",
            titulo="Plan más barato pero recibo más caro: el descuento iba con el plan anterior",
            causas=[TipoMovimiento.CAMBIO_PLAN],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "El cliente se pasó a un plan de menor precio de lista y su recibo subió. "
                "Está convencido de que hay un error."
            ),
            estructura=[
                "Reconocer el hecho tal como lo ve el cliente: el plan nuevo cuesta menos.",
                "Señalar que lo que pagaba antes ya venía rebajado por una promoción.",
                "Mostrar las dos líneas por separado: la renta bajó, el descuento dejó de "
                "aplicarse.",
                "Sumar el efecto del ajuste por los días del mes en curso.",
                "Cerrar con el monto que verá el mes siguiente, ya estabilizado.",
            ],
            guia_narrativa=(
                "Nunca decir que el cliente se equivoca. Tiene razón en lo que ve: el plan "
                "es más barato. Lo que cambió es el descuento, no el plan."
            ),
            error_frecuente=(
                "Resumir en una sola causa. Aquí hay dos movimientos en la misma línea de "
                "tiempo y omitir uno deja la explicación coja."
            ),
            accion_sugerida="VER_ALTERNATIVAS",
            conceptos=["RENTA_PLAN_MOVIL", "DESCUENTO_PROMOCIONAL", "AJUSTE_RETROACTIVO_RENTA"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_EQUIPO_FINANCIADO_VENCIDA",
            titulo="Primera cuota de equipo financiado con renta vencida",
            causas=[TipoMovimiento.ALTA_EQUIPO_FINANCIADO],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "El cliente compró un equipo en cuotas y ve una línea nueva en el recibo de "
                "su servicio."
            ),
            estructura=[
                "Nombrar el equipo y decir que la línea corresponde a su compra en cuotas.",
                "Indicar en qué cuota va y cuántas cuotas tiene el financiamiento.",
                "Aclarar que la cuota no se reparte por días aunque haya comprado a fin de mes.",
                "Anticipar el final: cuando pague la última cuota, esa línea desaparece.",
            ],
            guia_narrativa=(
                "El cliente separa mentalmente el equipo del servicio; el recibo los junta. "
                "Hay que devolverle esa separación en la explicación."
            ),
            error_frecuente=(
                "Hablar de amortización o de sistema de cuotas. Basta con el número de "
                "cuota y el total de cuotas."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=["CUOTA_EQUIPO_FINANCIADO", "INTERES_FINANCIAMIENTO"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_EQUIPO_FINANCIADO_ADELANTADA",
            titulo="Primera cuota de equipo financiado con renta adelantada",
            causas=[TipoMovimiento.ALTA_EQUIPO_FINANCIADO],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "Aparece la cuota del equipo junto a la renta anticipada del mes siguiente y "
                "el cliente no distingue qué corresponde a qué."
            ),
            estructura=[
                "Separar en dos bloques: lo que es servicio y lo que es equipo.",
                "Precisar que la renta corresponde al mes que viene y la cuota al equipo.",
                "Indicar el número de cuota y cuántas faltan.",
                "Cerrar diciendo que la cuota será igual todos los meses hasta la última.",
            ],
            guia_narrativa=(
                "La confusión no es el monto, es la mezcla. Separar antes de explicar."
            ),
            error_frecuente=(
                "Sumar la cuota a la renta en el resumen. Son conceptos de naturaleza "
                "distinta y el cliente los vive por separado."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=["CUOTA_EQUIPO_FINANCIADO", "RENTA_PLAN_MOVIL"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_FIN_FINANCIAMIENTO",
            titulo="Última cuota pagada: el recibo baja de forma permanente",
            causas=[TipoMovimiento.ALTA_EQUIPO_FINANCIADO],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="-",
            situacion=(
                "El cliente terminó de pagar su equipo y su recibo bajó respecto del mes anterior."
            ),
            estructura=[
                "Dar la buena noticia primero: terminó de pagar su equipo.",
                "Señalar que la baja es permanente, no una promoción temporal.",
                "Recordar los beneficios que su plan ya incluye, sin presentarlos como nuevos.",
            ],
            guia_narrativa=(
                "Es el único caso donde la explicación puede cerrar en clave positiva sin "
                "forzar nada. El efecto efervescente encaja aquí de forma natural."
            ),
            error_frecuente=(
                "Convertir la buena noticia en una venta. Solo procede una oferta si existe "
                "una regla de negocio explícita que la habilite."
            ),
            accion_sugerida="PAGAR",
            conceptos=["CUOTA_EQUIPO_FINANCIADO"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_RECONEXION_VENCIDA",
            titulo="Corte por deuda y reconexión con renta vencida",
            causas=[TipoMovimiento.RECONEXION, TipoMovimiento.SUSPENSION],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "El servicio estuvo suspendido unos días y luego se reactivó. El cliente ve "
                "un cargo nuevo y siente que le cobran por un servicio que no tuvo."
            ),
            estructura=[
                "Decir primero que los días sin servicio no se le cobraron.",
                "Mostrar la renta con los días efectivamente cobrados.",
                "Introducir después el cargo de reconexión, aclarando que es único y fijo.",
                "Explicar el neto: por qué el recibo sube pese a la devolución.",
                "Indicar cómo evitar que vuelva a ocurrir.",
            ],
            guia_narrativa=(
                "El orden importa más que en ningún otro caso: primero lo que le devuelven, "
                "después lo que le cobran. Al revés, la explicación se lee como un castigo."
            ),
            error_frecuente=(
                "Justificar la suspensión antes de explicar el recibo. El cliente pregunta "
                "por el monto, no por la política de cobranza."
            ),
            accion_sugerida="PAGAR",
            conceptos=["RENTA_PLAN_MOVIL", "CARGO_RECONEXION"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_RECONEXION_ADELANTADA",
            titulo="Corte y reconexión con renta adelantada: la devolución llega después",
            causas=[TipoMovimiento.RECONEXION, TipoMovimiento.SUSPENSION],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "El cliente ve una línea que resta por los días sin servicio y otra que suma "
                "por la reconexión, en el mismo recibo."
            ),
            estructura=[
                "Explicar que la renta de esos días ya estaba pagada por adelantado.",
                "Presentar la línea de devolución como la corrección de ese cobro.",
                "Presentar el cargo de reconexión como cobro único y fijo.",
                "Cerrar con el efecto neto sobre el total.",
            ],
            guia_narrativa=(
                "Dos líneas de signo contrario en el mismo recibo confunden. Nombrarlas por "
                "separado y solo entonces sumarlas."
            ),
            error_frecuente=(
                "Presentar la devolución como un descuento comercial. Es una corrección, y "
                "llamarla descuento genera expectativas para el mes siguiente."
            ),
            accion_sugerida="PAGAR",
            conceptos=["AJUSTE_DIAS_SUSPENSION", "CARGO_RECONEXION"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_FIN_DESCUENTO_VENCIDA",
            titulo="Promoción vencida a mitad de ciclo con renta vencida",
            causas=[TipoMovimiento.FIN_DESCUENTO],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "El descuento sigue apareciendo pero por un monto menor, porque venció en "
                "medio del ciclo. El cliente cree que se lo redujeron sin avisar."
            ),
            estructura=[
                "Confirmar que la promoción llegó a su fecha de fin y decir cuál era.",
                "Aclarar que este mes el descuento se aplicó solo por los días vigentes.",
                "Avisar de que el mes siguiente ya no aparecerá.",
                "Ofrecer revisar alternativas comerciales si el cliente lo pide.",
            ],
            guia_narrativa=(
                "El cliente siente que le quitaron algo, y lo dice así: se me venció la "
                "promoción, ahora me cobran de más. Conviene nombrar la promoción por su "
                "nombre y su fecha de fin: convierte una sospecha en un dato."
            ),
            error_frecuente=(
                "Decir que no se le cobró nada nuevo y quedarse ahí. Es cierto y es "
                "insuficiente: hay que decir qué dejó de restarse."
            ),
            accion_sugerida="VER_ALTERNATIVAS",
            conceptos=["DESCUENTO_PROMOCIONAL"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_FIN_DESCUENTO_ADELANTADA",
            titulo="Promoción vencida con renta adelantada: la línea desaparece",
            causas=[TipoMovimiento.FIN_DESCUENTO],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "La línea de descuento ya no está en el recibo y el cliente no encuentra "
                "ninguna línea nueva que justifique la subida."
            ),
            estructura=[
                "Nombrar de entrada lo que ya no está, porque es lo que el cliente no encuentra.",
                "Recordar la duración pactada de la promoción y su fecha de fin.",
                "Confirmar que el resto del recibo no cambió.",
                "Dejar claro que este es el monto que verá de aquí en adelante.",
            ],
            guia_narrativa=(
                "Es el caso más difícil de ver para el cliente: la causa es una ausencia. "
                "Hay que señalar la ausencia de forma explícita."
            ),
            error_frecuente=(
                "Explicar solo las líneas que sí están. La causa no está en el recibo "
                "actual, está en el anterior."
            ),
            accion_sugerida="VER_ALTERNATIVAS",
            conceptos=["DESCUENTO_PROMOCIONAL"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_ALTA_PAQUETE_VENCIDA",
            titulo="Compra de paquete durante el ciclo con renta vencida",
            causas=[TipoMovimiento.ALTA_PAQUETE],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "Aparece una línea de paquete que el cliente no siempre recuerda haber comprado."
            ),
            estructura=[
                "Nombrar el paquete, la fecha de activación y el canal por el que se compró.",
                "Aclarar que es un cobro puntual, no recurrente.",
                "Confirmar que no se repetirá el mes siguiente salvo nueva compra.",
                "Ofrecer registrar la consulta si el cliente no lo reconoce.",
            ],
            guia_narrativa=(
                "La fecha y el canal son lo que hace que el cliente lo recuerde. Sin esos "
                "dos datos la explicación no cierra."
            ),
            error_frecuente=(
                "Dar por hecho que el cliente lo compró. Si no lo reconoce, corresponde "
                "derivar con contexto, no insistir."
            ),
            accion_sugerida="REGISTRAR_CONSULTA",
            conceptos=["PAQUETE_DATOS_ADICIONAL", "PAQUETE_ROAMING"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_ALTA_PAQUETE_ADELANTADA",
            titulo="Compra de paquete con renta adelantada",
            causas=[TipoMovimiento.ALTA_PAQUETE],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "El paquete se cobra en este recibo mientras la renta corresponde al mes "
                "siguiente, y el cliente mezcla ambos periodos."
            ),
            estructura=[
                "Separar el periodo de la renta del periodo del paquete.",
                "Nombrar el paquete con su fecha de activación.",
                "Aclarar si es puntual o si se renovará el mes siguiente.",
            ],
            guia_narrativa=(
                "Con renta adelantada conviven dos periodos en el mismo documento; conviene "
                "decirlo antes de entrar en el detalle del paquete."
            ),
            error_frecuente=(
                "Hablar del paquete sin situarlo en el tiempo. La fecha es la que ancla el "
                "recuerdo del cliente."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=["PAQUETE_DATOS_ADICIONAL", "PAQUETE_TV_PREMIUM"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_NOTA_CREDITO_VENCIDA",
            titulo="Nota de crédito aplicada: el recibo baja",
            causas=[TipoMovimiento.NOTA_CREDITO],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="-",
            situacion=(
                "El cliente ve un recibo más bajo de lo normal y quiere confirmar que no es "
                "un error que le vayan a cobrar después."
            ),
            estructura=[
                "Confirmar que la baja es correcta y a qué documento corresponde.",
                "Explicar el motivo por el que se emitió la nota.",
                "Aclarar que es un ajuste aplicado, no un aplazamiento del cobro.",
                "Indicar cuál será el monto habitual del mes siguiente.",
            ],
            guia_narrativa=(
                "La duda no es por qué bajó, es si se lo van a cobrar después. Hay que "
                "responder esa pregunta aunque no la haya hecho."
            ),
            error_frecuente=(
                "Explicar la naturaleza tributaria del documento. Al cliente le importa el "
                "motivo y la certeza, no la figura contable."
            ),
            accion_sugerida="PAGAR",
            conceptos=["NOTA_CREDITO"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_NOTA_DEBITO_ADELANTADA",
            titulo="Nota de débito: se cobra algo de un periodo anterior",
            causas=[TipoMovimiento.NOTA_DEBITO],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "Aparece un cargo que corresponde a un mes pasado y el cliente no lo asocia "
                "con este recibo."
            ),
            estructura=[
                "Situar el cargo en su periodo de origen, no en el mes actual.",
                "Explicar el motivo concreto de la regularización.",
                "Confirmar que es un cobro único y no se repetirá.",
                "Ofrecer derivación si el cliente no está de acuerdo con el cargo.",
            ],
            guia_narrativa=(
                "El desconcierto viene del desfase temporal. Nombrar el periodo de origen "
                "resuelve la mayor parte de la consulta."
            ),
            error_frecuente=(
                "Tratarlo como un cargo del mes en curso. Se pierde la única información "
                "que lo hace comprensible."
            ),
            accion_sugerida="REGISTRAR_CONSULTA",
            conceptos=["NOTA_DEBITO"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_DEUDA_ANTERIOR_VENCIDA",
            titulo="El consumo no cambió pero el monto a pagar sí: deuda arrastrada",
            causas=[],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="0",
            situacion=(
                "El cliente dice que le vino más caro, pero el recibo del mes es idéntico al "
                "anterior: lo que subió es el total a pagar, por el saldo pendiente."
            ),
            estructura=[
                "Distinguir de entrada dos cifras: el recibo del mes y el total a pagar.",
                "Afirmar con claridad que el consumo del mes no varió.",
                "Presentar el saldo anterior como arrastre, no como cargo nuevo.",
                "Mencionar el interés por pago fuera de fecha si lo hubiera.",
                "Indicar qué pasa cuando regularice el pago.",
            ],
            guia_narrativa=(
                "Es el caso donde el cliente y el sistema hablan de dos números distintos. "
                "Separar las dos cifras es toda la explicación."
            ),
            error_frecuente=(
                "Responder que el recibo no varió y cerrar. Es cierto y no resuelve nada: el "
                "cliente mira el monto a pagar."
            ),
            accion_sugerida="PAGAR",
            conceptos=["DEUDA_ANTERIOR", "INTERES_MORATORIO"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_DEUDA_ANTERIOR_ADELANTADA",
            titulo="Saldo pendiente con renta adelantada",
            causas=[],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="0",
            situacion=(
                "El cliente arrastra un recibo impago y además paga por adelantado, así que "
                "ve dos periodos y una deuda en el mismo documento."
            ),
            estructura=[
                "Ordenar el documento en tres piezas: lo que ya debía, lo que se cobra "
                "ahora y el periodo al que corresponde.",
                "Confirmar que el consumo del mes no varió.",
                "Señalar el riesgo de suspensión si el saldo sigue pendiente.",
                "Explicar cómo se refleja el pago una vez realizado.",
            ],
            guia_narrativa=(
                "Con renta adelantada y deuda conviven tres periodos. Ordenarlos "
                "cronológicamente evita que el cliente crea que le cobran de más."
            ),
            error_frecuente=(
                "Mezclar la deuda con la renta anticipada en una sola cifra. Deja al cliente "
                "sin saber qué está pagando."
            ),
            accion_sugerida="PAGAR",
            conceptos=["DEUDA_ANTERIOR", "RENTA_PLAN_MOVIL"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_ESTABLE_VENCIDA",
            titulo="El recibo no varió",
            causas=[],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="0",
            situacion=(
                "El cliente pregunta por qué le vino más caro, pero el recibo es idéntico al "
                "del mes anterior."
            ),
            estructura=[
                "Decir con claridad que el recibo no varió respecto del mes anterior.",
                "Enumerar brevemente los conceptos que lo componen.",
                "Preguntar si la duda es por el monto habitual o por algún concepto concreto.",
                "Ofrecer derivación si el cliente insiste en que hay una diferencia.",
            ],
            guia_narrativa=(
                "Es el control de honestidad del sistema. Si no hay variación, no se "
                "construye una explicación: se dice que no la hay."
            ),
            error_frecuente=(
                "Fabricar una causa para no dejar la respuesta vacía. Inventar una "
                "explicación cuando el delta es cero es exactamente la alucinación que hay "
                "que evitar."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=[],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_ESTABLE_ADELANTADA",
            titulo="El recibo no varió, con renta adelantada",
            causas=[],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="0",
            situacion=(
                "No hubo cambios y el cliente consulta igualmente, normalmente porque "
                "compara periodos distintos."
            ),
            estructura=[
                "Confirmar que no hubo variación.",
                "Recordar que la renta corresponde al mes siguiente, por si el cliente "
                "estaba comparando periodos distintos.",
                "Ofrecer el detalle línea por línea.",
            ],
            guia_narrativa=(
                "Buena parte de estas consultas se resuelven aclarando qué periodo cobra el recibo."
            ),
            error_frecuente="Dar por resuelta la consulta sin aclarar el periodo cobrado.",
            accion_sugerida="VER_DETALLE",
            conceptos=[],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_COMPUESTO_PLAN_Y_EQUIPO",
            titulo="Cambio de plan y equipo financiado en el mismo ciclo",
            causas=[TipoMovimiento.ALTA_EQUIPO_FINANCIADO, TipoMovimiento.CAMBIO_PLAN],
            modalidad=ModalidadRenta.ADELANTADA,
            signo_delta="+",
            situacion=(
                "Dos cosas cambiaron a la vez y el cliente atribuye toda la subida a la más "
                "visible, normalmente el equipo."
            ),
            estructura=[
                "Anunciar que hay dos motivos, no uno.",
                "Cuantificar cada motivo por separado y en orden de peso.",
                "Aclarar cuál de los dos es temporal y cuál se mantendrá.",
                "Cerrar con lo que verá el mes siguiente.",
            ],
            guia_narrativa=(
                "Cuando hay dos causas, la peor respuesta es la que menciona una sola. "
                "Enumerarlas explícitamente evita la repregunta."
            ),
            error_frecuente=(
                "Atribuir toda la variación a la línea de mayor monto. Es la trampa "
                "clásica de la atribución por tamaño."
            ),
            accion_sugerida="VER_DETALLE",
            conceptos=["RENTA_PLAN_MOVIL", "CUOTA_EQUIPO_FINANCIADO"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_COMPUESTO_DESCUENTO_Y_DEUDA",
            titulo="Fin de promoción y saldo pendiente a la vez",
            causas=[TipoMovimiento.FIN_DESCUENTO],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "Se acabó la promoción y además arrastra un recibo impago. El cliente ve una "
                "subida grande y sospecha de un error."
            ),
            estructura=[
                "Separar el recibo del mes del total a pagar.",
                "Explicar la subida del mes por el fin de la promoción.",
                "Presentar aparte el saldo arrastrado.",
                "Advertir del riesgo de suspensión si no se regulariza.",
            ],
            guia_narrativa=(
                "Dos causas de naturaleza distinta: una explica el recibo, la otra explica "
                "el monto a pagar. Mezclarlas duplica la confusión."
            ),
            error_frecuente=(
                "Sumar ambos efectos en una sola cifra. El cliente necesita saber cuánto es "
                "consumo y cuánto es deuda."
            ),
            accion_sugerida="PAGAR",
            conceptos=["DESCUENTO_PROMOCIONAL", "DEUDA_ANTERIOR"],
        ),
        CasuisticaSeed(
            casuistica_id="CAS_SIN_CAUSA_ATRIBUIBLE",
            titulo="Variación sin movimiento que la explique",
            causas=[],
            modalidad=ModalidadRenta.VENCIDA,
            signo_delta="+",
            situacion=(
                "El recibo varió pero no hay ninguna orden en el historial que lo justifique "
                "con certeza."
            ),
            estructura=[
                "Decir qué concepto varió, que es lo que sí se sabe.",
                "No afirmar una causa: indicar que se está revisando el motivo.",
                "Derivar a un asesor con el contexto ya cargado.",
            ],
            guia_narrativa=(
                "La respuesta correcta aquí es una derivación, no una explicación. Media "
                "explicación con una causa inventada es peor que ninguna."
            ),
            error_frecuente=(
                "Elegir la causa más probable y presentarla como cierta. Con confianza baja "
                "corresponde derivar."
            ),
            accion_sugerida="DERIVAR_ASESOR",
            conceptos=[],
        ),
    ]
    if len(casuisticas) < MINIMO_CASUISTICAS:
        raise ValueError(
            f"el corpus de casuísticas tiene {len(casuisticas)} entradas y la "
            f"especificación exige al menos {MINIMO_CASUISTICAS}"
        )
    return casuisticas


# --------------------------------------------------------------------------- #
# Validación y volcado
# --------------------------------------------------------------------------- #
def validar_sin_cifras(
    faqs: list[FaqSeed] | None = None,
    casuisticas: list[CasuisticaSeed] | None = None,
) -> list[str]:
    """Comprueba que ni las FAQ ni las casuísticas contienen cifras.

    Devuelve la lista de infracciones (vacía si todo está limpio). El corpus recuperado
    entra al prompt: cualquier número que sobreviva aquí sería una cifra ajena al
    cliente que el verificador encontraría en el texto final.
    """
    infracciones: list[str] = []
    for faq in faqs or []:
        for campo in ("pregunta", "respuesta"):
            encontrado = _PATRON_CIFRA.findall(getattr(faq, campo))
            if encontrado:
                infracciones.append(
                    f"{faq.faq_id}.{campo} contiene cifras: {sorted(set(encontrado))}"
                )
    for caso in casuisticas or []:
        textos = [caso.titulo, caso.situacion, caso.guia_narrativa, caso.error_frecuente]
        textos.extend(caso.estructura)
        for indice, texto in enumerate(textos):
            encontrado = _PATRON_CIFRA.findall(texto)
            if encontrado:
                infracciones.append(
                    f"{caso.casuistica_id}[{indice}] contiene cifras: {sorted(set(encontrado))}"
                )
    return infracciones


def _volcar(ruta: str | Path, registros: list[dict[str, object]]) -> Path:
    """Escribe una lista de registros como JSON legible en UTF-8."""
    destino = Path(ruta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(registros, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destino


def escribir_faqs(ruta: str | Path) -> Path:
    """Escribe ``faqs.json`` y devuelve la ruta.

    Raises:
        ValueError: si alguna FAQ contiene cifras.
    """
    faqs = construir_faqs()
    infracciones = validar_sin_cifras(faqs=faqs)
    if infracciones:
        raise ValueError("el corpus de FAQ contiene cifras:\n" + "\n".join(infracciones))
    registros = []
    for faq in faqs:
        registro = faq.model_dump(mode="json")
        registro["texto_indexable"] = faq.texto_indexable
        registros.append(registro)
    return _volcar(ruta, registros)


def escribir_casuisticas(ruta: str | Path) -> Path:
    """Escribe ``casuisticas.json`` y devuelve la ruta.

    Raises:
        ValueError: si alguna casuística contiene cifras.
    """
    casuisticas = construir_casuisticas()
    infracciones = validar_sin_cifras(casuisticas=casuisticas)
    if infracciones:
        raise ValueError("el corpus de casuísticas contiene cifras:\n" + "\n".join(infracciones))
    registros = []
    for caso in casuisticas:
        registro = caso.model_dump(mode="json")
        registro["texto_indexable"] = caso.texto_indexable
        registros.append(registro)
    return _volcar(ruta, registros)
