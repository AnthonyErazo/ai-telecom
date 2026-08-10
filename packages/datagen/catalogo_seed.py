"""Catálogo semilla de conceptos de facturación en lenguaje de cliente.

Es uno de los tres corpus recuperables del RAG (los otros dos están en ``faq_seed``).
El recibo **no** se vectoriza: se consulta por clave. Lo que sí se indexa es esto, la
explicación de qué significa cada concepto, que es información general y no depende
de ningún cliente.

Regla dura de este módulo: **ningún texto contiene cifras**. Ni importes, ni
porcentajes, ni fechas, ni cantidades de días. El motivo está en la sección 6 de la
especificación: si una definición dijera "por ejemplo, un descuento de tanto", ese
número no es del cliente que pregunta, y el verificador lo encontraría en el texto
final sin poder anclarlo al FactSet. Todas las cifras salen del FactSet y solo del
FactSet. ``validar_sin_cifras`` comprueba la regla y ``generar.py`` aborta si falla.

Las definiciones están escritas como habla y escribe una persona en Lima, de usted, sin
jerga corporativa ni español neutro de manual. Tres reglas de redacción que no son de
estilo sino de comprensión:

* Se dice **recibo**. Nunca *factura*: en atención a personas, la factura es el
  documento de una empresa y decirlo así aleja al cliente del texto.
* **Cuidado con "cancelar".** En Perú cancelar significa *pagar*: "ya cancelé mi
  recibo" quiere decir que ya lo pagó. Por eso en estos textos "cancelar" no se usa
  jamás con el sentido de anular —para eso se dice *dar de baja*—, y donde antes ponía
  "un recibo sin cancelar" ahora pone "un recibo sin pagar", que no se puede leer al
  revés.
* Se prefiere la forma en que el cliente lo cuenta: *le llegó más caro*, *le cobraron
  de más*, *se le venció la promoción*, *le cortaron el servicio*, *se le acabaron los
  gigas*. Además de sonar a persona, mete en el corpus las palabras exactas con las que
  la gente escribe, que es lo que busca el índice léxico.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import CausaOficial, FamiliaConcepto
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas

__all__ = [
    "MINIMO_CONCEPTOS",
    "TEXTOS_CONCEPTO",
    "ConceptoSeed",
    "TextoConcepto",
    "a_registros",
    "construir_catalogo_seed",
    "escribir_catalogo",
    "validar_sin_cifras",
]

#: Mínimo exigido por la especificación (sección 8).
MINIMO_CONCEPTOS = 25

#: Cifras prohibidas en cualquier texto del corpus: dígitos, símbolo de moneda, porcentaje.
_PATRON_CIFRA = re.compile(r"[0-9]|S/|%")


class TextoConcepto(BaseModel):
    """Los tres textos de cliente de un concepto, sin una sola cifra."""

    model_config = ConfigDict(extra="forbid")

    explicacion_simple: str = Field(description="Una o dos frases; es lo que lee el cliente")
    explicacion_detalle: str = Field(description="Ampliación para quien quiere entender el porqué")
    cuando_aparece: str = Field(description="En qué situación aparece o varía este concepto")


class ConceptoSeed(BaseModel):
    """Entrada del corpus ``concepto_catalogo`` lista para indexar."""

    model_config = ConfigDict(extra="forbid")

    concepto_id: str
    nombre_comercial: str
    nombre_tecnico: str = ""
    familia: FamiliaConcepto
    causa_oficial: CausaOficial | None = None
    explicacion_simple: str
    explicacion_detalle: str
    cuando_aparece: str
    sinonimos: list[str] = Field(default_factory=list)
    prorrateable: bool = False
    afecto_igv: bool = True
    visible_cliente: bool = True

    @property
    def texto_indexable(self) -> str:
        """Texto que se manda al índice léxico y al vectorial."""
        return " ".join(
            [
                self.nombre_comercial,
                self.explicacion_simple,
                self.explicacion_detalle,
                self.cuando_aparece,
                " ".join(self.sinonimos),
            ]
        )


#: Textos de cliente por concepto. Se escriben aquí, no en ``rules.yaml``, porque son
#: contenido editorial (lo revisa atención al cliente) y no reglas de cálculo.
TEXTOS_CONCEPTO: dict[str, TextoConcepto] = {
    "RENTA_PLAN_MOVIL": TextoConcepto(
        explicacion_simple=(
            "Es el cobro mensual de su plan móvil. Cubre lo que su plan trae incluido: "
            "sus minutos, sus mensajes y sus datos."
        ),
        explicacion_detalle=(
            "Se cobra una sola vez al mes y cubre su ciclo de facturación completo. Si a "
            "mitad de mes usted cambió de plan, esta línea se parte en dos y cada parte "
            "se cobra por los días que estuvo vigente cada plan."
        ),
        cuando_aparece=(
            "Aparece en todos sus recibos. Se mueve cuando usted cambia de plan, cuando "
            "le suspendieron el servicio o cuando se le venció un descuento del plan."
        ),
    ),
    "RENTA_HOGAR_INTERNET": TextoConcepto(
        explicacion_simple="Es el cobro mensual del internet de su casa.",
        explicacion_detalle=(
            "Corresponde a la velocidad que usted tiene contratada. Si subió o bajó de "
            "plan a mitad de mes, el cobro se reparte según los días que estuvo con cada "
            "velocidad."
        ),
        cuando_aparece=(
            "Aparece en todos sus recibos mientras tenga internet fijo. Se mueve si "
            "cambió de plan, si estuvo unos días sin servicio o si se le venció una "
            "promoción."
        ),
    ),
    "RENTA_TV": TextoConcepto(
        explicacion_simple="Es el cobro mensual de su servicio de televisión.",
        explicacion_detalle=(
            "Incluye los canales del paquete que tiene contratado. Los canales que usted "
            "suma aparte se cobran en su propia línea del recibo."
        ),
        cuando_aparece=(
            "Aparece mientras tenga televisión contratada. Se mueve si cambió de paquete "
            "o si le suspendieron el servicio parte del mes."
        ),
    ),
    "RENTA_LINEA_FIJA": TextoConcepto(
        explicacion_simple="Es el cobro mensual de su teléfono fijo.",
        explicacion_detalle=(
            "Cubre el alquiler de la línea y los minutos que su plan trae incluidos. Las "
            "llamadas que su plan no cubre se le cobran aparte."
        ),
        cuando_aparece=(
            "Aparece mientras tenga teléfono fijo. Se mueve si cambió el paquete de "
            "minutos o si hubo días sin servicio."
        ),
    ),
    "RENTA_MOVISTAR_TOTAL": TextoConcepto(
        explicacion_simple=(
            "Es el cobro mensual de su paquete Movistar Total, que junta sus servicios "
            "de móvil y de hogar en un solo plan."
        ),
        explicacion_detalle=(
            "Como está todo en un mismo paquete, esta línea reemplaza a la renta de cada "
            "servicio por separado. Por eso su recibo tiene menos líneas que antes, "
            "aunque usted siga teniendo los mismos servicios."
        ),
        cuando_aparece=(
            "Aparece desde el mes en que se juntaron sus servicios. Se mueve si cambia de "
            "paquete o si deja de cumplir las condiciones del beneficio."
        ),
    ),
    "PRORRATEO_PLAN": TextoConcepto(
        explicacion_simple=(
            "Es el cobro por los días, no por el mes entero. Se usa cuando algo cambió en "
            "medio de su ciclo y no justo el primer día."
        ),
        explicacion_detalle=(
            "Su recibo no cobra meses de calendario, cobra su ciclo de facturación. "
            "Cuando algo cambia a mitad del ciclo no se le cobra el mes completo de cada "
            "plan: se le cobra la parte de días que le toca a cada uno, y las dos partes "
            "juntas suman un mes."
        ),
        cuando_aparece=(
            "Aparece cuando usted cambió de plan, contrató o dio de baja un servicio "
            "cualquier día que no sea el primero de su ciclo."
        ),
    ),
    "AJUSTE_RETROACTIVO_RENTA": TextoConcepto(
        explicacion_simple=(
            "Su plan se paga por adelantado. Si algo cambió a mitad del mes que ya se le "
            "había cobrado, este ajuste corrige la diferencia en el recibo siguiente."
        ),
        explicacion_detalle=(
            "Con renta adelantada, el recibo de este mes cobra el mes que viene. Si usted "
            "cambió de plan cuando el mes en curso ya estaba pagado, hay que devolverle lo "
            "que se le cobró de más, o cobrarle lo que faltó por los días que ya corrieron "
            "con el plan nuevo. Por eso en un mismo recibo conviven la renta del mes que "
            "viene y la corrección del mes que pasó, y el total le puede llegar más caro "
            "aunque su plan nuevo cueste menos que el anterior."
        ),
        cuando_aparece=(
            "Aparece en el recibo siguiente a un cambio de plan, solo si su renta es "
            "adelantada."
        ),
    ),
    "AJUSTE_DIAS_SUSPENSION": TextoConcepto(
        explicacion_simple=(
            "Es la devolución por los días en que le cortaron el servicio y usted no lo "
            "pudo usar."
        ),
        explicacion_detalle=(
            "Los días sin servicio no se cobran. Si su renta se paga por adelantado, esos "
            "días ya se los habían cobrado en el recibo anterior, así que la devolución le "
            "aparece como una línea que resta en el recibo siguiente."
        ),
        cuando_aparece=(
            "Aparece en el recibo del ciclo en que le suspendieron el servicio, o en el "
            "siguiente si su renta es adelantada."
        ),
    ),
    "CARGO_RECONEXION": TextoConcepto(
        explicacion_simple=(
            "Es el cobro por volver a activarle el servicio después de un corte. Se cobra "
            "una sola vez, en el recibo del mes en que se lo reactivaron."
        ),
        explicacion_detalle=(
            "Es un importe fijo: no depende de cuántos días estuvo cortado el servicio ni "
            "del plan que usted tenga. No se reparte por días y no se repite en los meses "
            "siguientes."
        ),
        cuando_aparece=(
            "Aparece únicamente en el recibo del ciclo en que le reconectaron el servicio."
        ),
    ),
    "CUOTA_EQUIPO_FINANCIADO": TextoConcepto(
        explicacion_simple=(
            "Es la cuota mensual del equipo que compró en cuotas. Siempre se cobra "
            "completa: no se parte por días. Se acaba cuando paga la última."
        ),
        explicacion_detalle=(
            "El equipo se paga aparte del servicio, aunque le llegue en el mismo recibo. "
            "La cuota es la misma todos los meses y el recibo le dice en qué cuota va y "
            "cuántas le faltan. Cuando paga la última, esa línea desaparece y su recibo "
            "baja para siempre."
        ),
        cuando_aparece=(
            "Aparece desde el recibo del mes en que compró el equipo y hasta la última "
            "cuota, así lo haya comprado el último día del ciclo."
        ),
    ),
    "INTERES_FINANCIAMIENTO": TextoConcepto(
        explicacion_simple="Es la parte de intereses que va dentro de la cuota de su equipo.",
        explicacion_detalle=(
            "Cuando el financiamiento tiene intereses, la cuota se arma con dos partes: la "
            "que va al precio del equipo y la que corresponde al interés. La parte de "
            "interés va bajando mes a mes conforme usted va pagando el equipo."
        ),
        cuando_aparece=(
            "Aparece solo si su financiamiento tiene intereses. Si compró en cuotas sin "
            "intereses, esta línea no existe en su recibo."
        ),
    ),
    "PAQUETE_DATOS_ADICIONAL": TextoConcepto(
        explicacion_simple=(
            "Es el cobro por los gigas que compró aparte durante el mes, además de los que "
            "ya trae su plan."
        ),
        explicacion_detalle=(
            "Es una compra suelta: se cobra completa en el recibo del mes en que la activó "
            "y no se repite al mes siguiente, salvo que la vuelva a comprar. Si compró "
            "varias veces en el mismo mes, verá la suma de todas."
        ),
        cuando_aparece=("Aparece únicamente en el recibo del mes en que compró el paquete."),
    ),
    "PAQUETE_ROAMING": TextoConcepto(
        explicacion_simple=(
            "Es el cobro del paquete que activó para usar su línea fuera del país."
        ),
        explicacion_detalle=(
            "Cubre el uso de su línea en el extranjero mientras el paquete esté vigente. "
            "Es una compra suelta y no se renueva sola."
        ),
        cuando_aparece="Aparece en el recibo del mes en que activó el paquete para su viaje.",
    ),
    "PAQUETE_TV_PREMIUM": TextoConcepto(
        explicacion_simple=(
            "Es el cobro mensual de los canales o del paquete de streaming que contrató "
            "aparte de su plan."
        ),
        explicacion_detalle=(
            "Es un servicio que se renueva mes a mes hasta que usted lo da de baja. El "
            "primer recibo suele llegarle por menos, porque se le cobra solo desde el día "
            "en que lo contrató y no el mes entero."
        ),
        cuando_aparece=(
            "Aparece desde el mes en que lo contrató y se repite mientras lo mantenga "
            "activo."
        ),
    ),
    "SERVICIO_ADICIONAL_SEGURO": TextoConcepto(
        explicacion_simple=(
            "Es el cobro mensual del servicio de protección que contrató para su equipo y "
            "para sus datos."
        ),
        explicacion_detalle=(
            "Se renueva todos los meses mientras el servicio esté vigente. El primer cobro "
            "puede llegarle por menos, porque se calcula desde el día en que lo contrató."
        ),
        cuando_aparece=(
            "Aparece desde el mes en que lo contrató y se repite hasta que usted lo dé de baja."
        ),
    ),
    "ALQUILER_EQUIPO_HOGAR": TextoConcepto(
        explicacion_simple=(
            "Es el alquiler mensual del decodificador o del router que tiene en su casa."
        ),
        explicacion_detalle=(
            "Los equipos siguen siendo de Movistar y usted paga por usarlos. Si suma o "
            "devuelve un equipo a mitad de mes, el cobro se ajusta a los días de uso."
        ),
        cuando_aparece=(
            "Aparece mientras tenga equipos instalados. Se mueve si sumó o devolvió alguno."
        ),
    ),
    "INSTALACION_HOGAR": TextoConcepto(
        explicacion_simple=(
            "Es el cobro por la instalación de su servicio en casa. Se cobra una sola vez."
        ),
        explicacion_detalle=(
            "Cubre la visita del técnico y dejarle el servicio funcionando. Se cobra una "
            "sola vez y no vuelve a aparecer."
        ),
        cuando_aparece="Aparece solo en el primer recibo de su servicio de hogar.",
    ),
    "CARGO_TRASLADO": TextoConcepto(
        explicacion_simple=(
            "Es el cobro por mudar su servicio a otra dirección. Se cobra una sola vez."
        ),
        explicacion_detalle=(
            "Cubre la visita del técnico y la reinstalación en su nuevo domicilio. Se "
            "cobra una sola vez, en el recibo del mes en que se hizo el traslado."
        ),
        cuando_aparece="Aparece únicamente en el mes en que trasladó su servicio.",
    ),
    "LLAMADAS_FUERA_DE_PLAN": TextoConcepto(
        explicacion_simple=(
            "Son las llamadas que hizo por encima de lo que trae su plan, o a destinos que "
            "su plan no cubre."
        ),
        explicacion_detalle=(
            "Su plan trae una bolsa de minutos y ciertos destinos. Lo que se sale de esa "
            "bolsa se le cobra aparte, según el tiempo que habló y a dónde llamó."
        ),
        cuando_aparece=(
            "Aparece en los meses en que habló más de lo que trae su plan. Es la línea que "
            "más se mueve de un recibo a otro."
        ),
    ),
    "CONSUMO_DATOS_ADICIONAL": TextoConcepto(
        explicacion_simple="Son los datos que usó por encima de los que trae su plan.",
        explicacion_detalle=(
            "Cuando se le acaban los gigas de su plan y usted sigue navegando sin comprar "
            "un paquete, ese consumo se le cobra aparte."
        ),
        cuando_aparece=(
            "Aparece en los meses en que se le acabaron los gigas incluidos y siguió "
            "navegando."
        ),
    ),
    "SMS_PREMIUM": TextoConcepto(
        explicacion_simple=("Son los mensajes a números especiales o a servicios de contenido."),
        explicacion_detalle=(
            "Estos mensajes los cobra la empresa que da el contenido y Movistar se los "
            "traslada a su recibo. Casi siempre vienen de suscripciones que se activan "
            "desde el mismo celular."
        ),
        cuando_aparece=(
            "Aparece si usted mandó mensajes a servicios especiales o si tiene alguna "
            "suscripción de contenido activa."
        ),
    ),
    "LARGA_DISTANCIA": TextoConcepto(
        explicacion_simple="Son las llamadas que hizo a otros países.",
        explicacion_detalle=(
            "Se cobran por destino y por el tiempo que habló. No entran en la bolsa de "
            "minutos de su plan, salvo que su plan diga lo contrario."
        ),
        cuando_aparece="Aparece solo si hizo llamadas al extranjero durante el mes.",
    ),
    "DESCUENTO_PROMOCIONAL": TextoConcepto(
        explicacion_simple=(
            "Es el descuento de una promoción con fecha de fin. Mientras está vigente le "
            "resta del recibo; cuando se le vence, su recibo vuelve al precio de lista."
        ),
        explicacion_detalle=(
            "Las promociones se contratan por un tiempo pactado. Cuando llega la fecha de "
            "fin no se le cobra nada nuevo: simplemente deja de restarse el descuento, y "
            "por eso el recibo le llega más caro sin que aparezca ninguna línea nueva. Si "
            "la promoción se le vence a mitad del ciclo, ese mes el descuento se aplica "
            "solo por los días en que estuvo vigente."
        ),
        cuando_aparece=(
            "Aparece mientras la promoción esté vigente. Su recibo sube el mes en que se "
            "le vence."
        ),
    ),
    "DESCUENTO_MOVISTAR_TOTAL": TextoConcepto(
        explicacion_simple=(
            "Es el descuento que le hacen por tener sus servicios juntos en Movistar Total."
        ),
        explicacion_detalle=(
            "El beneficio existe porque usted mantiene el paquete completo. Si da de baja "
            "alguno de los servicios que lo forman, el descuento deja de aplicarse aunque "
            "se quede con los demás."
        ),
        cuando_aparece=(
            "Aparece mientras mantenga todos los servicios del paquete. Se pierde si "
            "desarma el paquete."
        ),
    ),
    "DESCUENTO_EQUIPO": TextoConcepto(
        explicacion_simple="Es el descuento de campaña que se aplica a la cuota de su equipo.",
        explicacion_detalle=(
            "Va amarrado a la campaña con la que compró el equipo y dura un tiempo "
            "limitado. Cuando la campaña se acaba, la cuota vuelve a su importe de siempre."
        ),
        cuando_aparece=(
            "Aparece mientras dure la campaña con la que compró el equipo financiado."
        ),
    ),
    "NOTA_CREDITO": TextoConcepto(
        explicacion_simple=(
            "Es un documento que resta de su recibo. Se emite para devolverle un cobro que "
            "no correspondía o para compensarlo por algo."
        ),
        explicacion_detalle=(
            "Es un documento tributario, no un descuento comercial: se emite porque la ley "
            "lo exige cuando hay que corregir hacia abajo algo que ya se le cobró. Por eso "
            "le aparece con su propio número de documento."
        ),
        cuando_aparece=(
            "Aparece en el recibo del mes en que se aplica la corrección. Es una de las "
            "razones por las que un recibo puede llegarle más barato de lo normal."
        ),
    ),
    "NOTA_DEBITO": TextoConcepto(
        explicacion_simple=(
            "Es un documento que suma a su recibo. Se emite para cobrarle algo que no se "
            "le había cobrado antes."
        ),
        explicacion_detalle=(
            "Igual que la nota de crédito, es un documento tributario que corrige un cobro "
            "anterior, pero hacia arriba. Se refiere a un consumo o a un cargo de un mes "
            "pasado, no del mes que usted está mirando."
        ),
        cuando_aparece=(
            "Aparece en el recibo del mes en que se regulariza ese cobro pendiente."
        ),
    ),
    "INTERES_MORATORIO": TextoConcepto(
        explicacion_simple=(
            "Es el interés por haber pagado un recibo después de la fecha de vencimiento."
        ),
        explicacion_detalle=(
            "Se calcula sobre el saldo que quedó pendiente y se sigue generando mientras "
            "la deuda no se pague. Deja de generarse apenas usted se pone al día."
        ),
        cuando_aparece=(
            "Aparece cuando un recibo anterior se pagó fuera de fecha o sigue pendiente."
        ),
    ),
    "DEUDA_ANTERIOR": TextoConcepto(
        explicacion_simple=(
            "Es lo que quedó pendiente de recibos anteriores. No es un cobro nuevo de este "
            "mes: se arrastra hasta que se paga."
        ),
        explicacion_detalle=(
            "No forma parte del total del periodo, y esa diferencia importa: su consumo "
            "del mes puede ser igualito al del mes pasado y aun así el total a pagar le "
            "sale más alto, porque arrastra el saldo anterior. Si usted ya pagó, el pago "
            "puede demorar unos días en verse reflejado."
        ),
        cuando_aparece=(
            "Aparece mientras haya un recibo anterior sin pagar, y desaparece apenas se "
            "pone al día."
        ),
    ),
    "IGV": TextoConcepto(
        explicacion_simple=(
            "Es el impuesto general a las ventas que se aplica sobre los servicios afectos "
            "de su recibo. Es un tributo de ley, no un cobro de Movistar."
        ),
        explicacion_detalle=(
            "Se calcula sobre la suma de los conceptos afectos del recibo. Como se aplica "
            "sobre esa base, cuando sus servicios suben o bajan el impuesto se mueve en la "
            "misma dirección. Algunos conceptos, como las cuotas de equipos financiados, "
            "no entran en esa base."
        ),
        cuando_aparece=(
            "Aparece en todos sus recibos y se mueve junto con el importe de los servicios "
            "afectos."
        ),
    ),
    "REDONDEO": TextoConcepto(
        explicacion_simple=(
            "Es un ajuste de céntimos para que el total de su recibo cuadre exacto."
        ),
        explicacion_detalle=(
            "Cuando un importe se reparte entre varias líneas, la división no siempre da "
            "un número exacto de céntimos. Este ajuste reparte esa diferencia mínima para "
            "que la suma de las líneas dé el total, ni un céntimo de más ni de menos."
        ),
        cuando_aparece=(
            "Es una línea técnica de control interno; normalmente no se le muestra al "
            "cliente."
        ),
    ),
}


def construir_catalogo_seed(reglas: ConfiguracionReglas | None = None) -> list[ConceptoSeed]:
    """Construye el corpus de catálogo cruzando ``rules.yaml`` con los textos de cliente.

    La estructura (familia, causa oficial, si se prorratea, si es afecto a impuesto) sale
    de ``rules.yaml``, que es la fuente única de reglas; los textos salen de este módulo,
    que es contenido editorial. Así nunca se desincronizan: si alguien añade un concepto
    al catálogo de reglas sin escribir su texto, esta función falla en el acto.

    Raises:
        KeyError: si un concepto de ``rules.yaml`` no tiene texto en ``TEXTOS_CONCEPTO``.
        ValueError: si el catálogo resultante no llega al mínimo exigido.
    """
    configuracion = reglas or cargar_reglas()
    faltantes = sorted(set(configuracion.concepto_ids()) - set(TEXTOS_CONCEPTO))
    if faltantes:
        raise KeyError(
            "conceptos de rules.yaml sin texto de cliente en catalogo_seed: " + ", ".join(faltantes)
        )

    catalogo: list[ConceptoSeed] = []
    for concepto in configuracion.catalogo:
        texto = TEXTOS_CONCEPTO[concepto.concepto_id]
        catalogo.append(
            ConceptoSeed(
                concepto_id=concepto.concepto_id,
                nombre_comercial=concepto.nombre_comercial,
                nombre_tecnico=concepto.nombre_tecnico,
                familia=concepto.familia,
                causa_oficial=concepto.causa_oficial,
                explicacion_simple=texto.explicacion_simple,
                explicacion_detalle=texto.explicacion_detalle,
                cuando_aparece=texto.cuando_aparece,
                sinonimos=list(concepto.sinonimos),
                prorrateable=concepto.prorrateable,
                afecto_igv=concepto.afecto_igv,
                visible_cliente=concepto.visible_cliente,
            )
        )

    if len(catalogo) < MINIMO_CONCEPTOS:
        raise ValueError(
            f"el catálogo semilla tiene {len(catalogo)} conceptos y la especificación "
            f"exige al menos {MINIMO_CONCEPTOS}"
        )
    return catalogo


def validar_sin_cifras(conceptos: list[ConceptoSeed]) -> list[str]:
    """Comprueba que ningún texto de cliente contiene cifras.

    Devuelve la lista de infracciones (vacía si todo está bien). Es la contraparte de
    ``retriever/saneador.py``: aquí se garantiza en origen lo que allí se garantiza en
    tiempo de consulta.
    """
    infracciones: list[str] = []
    for concepto in conceptos:
        for campo in ("explicacion_simple", "explicacion_detalle", "cuando_aparece"):
            texto = getattr(concepto, campo)
            encontrado = _PATRON_CIFRA.findall(texto)
            if encontrado:
                infracciones.append(
                    f"{concepto.concepto_id}.{campo} contiene cifras: {sorted(set(encontrado))}"
                )
    return infracciones


def a_registros(conceptos: list[ConceptoSeed]) -> list[dict[str, object]]:
    """Proyecta el catálogo a diccionarios listos para volcar a JSON o insertar en BD."""
    registros: list[dict[str, object]] = []
    for concepto in conceptos:
        registro = concepto.model_dump(mode="json")
        registro["texto_indexable"] = concepto.texto_indexable
        registros.append(registro)
    return registros


def escribir_catalogo(ruta: str | Path, reglas: ConfiguracionReglas | None = None) -> Path:
    """Escribe ``catalogo.json`` y devuelve la ruta.

    Raises:
        ValueError: si algún texto contiene cifras. Es un error de contenido, no de
            formato: una cifra en el corpus podría llegar al texto final del cliente.
    """
    conceptos = construir_catalogo_seed(reglas)
    infracciones = validar_sin_cifras(conceptos)
    if infracciones:
        raise ValueError(
            "el catálogo semilla contiene cifras y ninguna cifra puede salir del corpus:\n"
            + "\n".join(infracciones)
        )
    destino = Path(ruta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(a_registros(conceptos), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destino
