"""Clasificación de la intención del cliente, **antes** de tocar el recibo.

Por qué existe este módulo
--------------------------
Hasta ahora ``POST /v1/explicar`` daba por supuesto que toda frase del cliente
significaba «explícame el recibo». Con eso, un «hola», una cadena vacía o una
pregunta sobre la capital de Francia devolvían la explicación completa de la
factura. Peor: **«quiero cancelar mi servicio» también la devolvía**, porque la
única defensa era una comparación por subcadena contra ``"cancelar el servicio"``
y un posesivo la burlaba.

Un asistente de facturación que responde lo mismo pase lo que pase no es un
asistente: es un botón. Y una regla de cumplimiento regulatorio que se cae con un
«mi» en vez de un «el» es peor que no tenerla, porque da falsa confianza.

Cómo clasifica
--------------
Determinístico primero, y en este orden de prioridad, porque el orden **es** la
política:

1. ``REGULATORIA`` — baja, portabilidad, reclamo formal. Manda siempre, aunque la
   frase también pida explicar el recibo. Deriva sin explicar.
2. ``PEDIR_HUMANO`` — el cliente pide una persona. Se le da, sin regatear.
3. ``VACIO`` — no hay nada que interpretar.
4. ``SALUDO`` — cortesía sin pregunta.
5. ``EXPLICAR_RECIBO`` — la intención principal del desafío.
6. ``CONSULTA_CONCEPTO`` — «¿qué es un prorrateo?», sin pedir su recibo.
7. ``FUERA_DE_DOMINIO`` — el resto.

La coincidencia es **por tokens, no por subcadena**: se exige que todos los
tokens significativos del patrón estén presentes en la frase, en cualquier orden
y con cualquier palabra de por medio. Así «cancelar mi servicio», «cancelar el
servicio» y «quiero cancelarlo, el servicio ya no me sirve» disparan la misma
regla, sin depender de un modelo ni de una lista infinita de variantes.

Cómo escribe un cliente peruano
-------------------------------
Los patrones están en el castellano con el que la gente escribe de verdad en un chat,
no en el que imagina un redactor: ``xq``, ``pq``, ``q paso``, *me llegó caro*, *no me
cuadra*, *me cobraron de más*, *cuánto tengo q pagar*, *ya pagué*, *me cortaron el
servicio*. Sin esto, "xq me cobran tanto" caía en ``FUERA_DE_DOMINIO``, porque "por" y
"que" son palabras de enlace y "xq" no figuraba en ningún patrón.

Un cuidado que **no** es cosmético: en Perú **cancelar significa pagar**. "ya cancelé
mi recibo" quiere decir que ya lo pagó, y tratarlo como una baja sería un falso
positivo regulatorio de los caros. Aquí no hay ambigüedad por construcción: la regla de
baja exige dos raíces, ``cancel-`` **y** ``servici-`` (o ``contrat-``), así que "ya
cancelé mi recibo" —``cancel-`` + ``recib-``— no la dispara y cae, como debe, en
``EXPLICAR_RECIBO``. Por eso el patrón se escribe "cancelar el servicio" y nunca
"cancelar" a secas.

Este módulo **no llama a ningún modelo de lenguaje** y no conoce el recibo. Es
aritmética de conjuntos sobre texto normalizado.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import NamedTuple

from packages.core_domain.enums import MotivoDerivacion
from packages.facts_engine.jerga import expandir
from packages.facts_engine.peticion_humano import pide_humano

__all__ = [
    "PATRONES",
    "Intencion",
    "ResultadoIntencion",
    "clasificar_intencion",
    "coincide_patron",
    "detectar_manipulacion",
    "tokens_significativos",
]


# --------------------------------------------------------------------------- #
# Normalización
# --------------------------------------------------------------------------- #
_TOKENIZADOR = re.compile(r"[a-z0-9]+")

#: Palabras de enlace que no aportan intención. Deliberadamente corta: quitar de
#: más hace que patrones distintos colisionen.
_VACIAS: frozenset[str] = frozenset(
    {
        "a", "al", "de", "del", "el", "en", "es", "esta", "este", "la", "las", "lo",
        "los", "me", "mi", "mis", "para", "pero", "por", "que", "se", "su", "sus",
        "te", "tu", "tus", "un", "una", "y", "ya",
    }
)

#: Raíces que se comparan por prefijo: cubren conjugaciones y plurales sin
#: arrastrar un lematizador. ``cancel`` cubre cancelar, cancelarlo, cancelación.
_LONGITUD_RAIZ = 5


def _normalizar(texto: str) -> str:
    """Minúsculas sin tildes: la forma canónica para comparar."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def tokens_significativos(texto: str) -> set[str]:
    """Tokens de la frase sin palabras de enlace.

    Si al quitar las vacías no queda nada, se devuelven todas: es preferible
    comparar contra algo que contra el conjunto vacío.
    """
    palabras = set(_TOKENIZADOR.findall(_normalizar(texto)))
    utiles = palabras - _VACIAS
    return utiles or palabras


def _raices(tokens: set[str]) -> set[str]:
    """Recorta cada token a su raíz para tolerar conjugaciones y plurales."""
    return {t[:_LONGITUD_RAIZ] for t in tokens}


def coincide_patron(patron: str, utterance: str) -> bool:
    """¿Están todos los tokens significativos del patrón en la frase?

    Comparación por **raíces**, no por subcadena. Es lo que hace que
    ``"cancelar el servicio"`` case con *«quiero cancelar mi servicio»*, que era
    exactamente el fallo que motivó este módulo.
    """
    esperados = _raices(tokens_significativos(patron))
    if not esperados:
        return False
    return esperados <= _raices(tokens_significativos(utterance))


# --------------------------------------------------------------------------- #
# Intenciones
# --------------------------------------------------------------------------- #
class Intencion(StrEnum):
    """Qué quiere el cliente. El orden de declaración no implica prioridad."""

    SOSPECHOSA = "SOSPECHOSA"
    REGULATORIA = "REGULATORIA"
    DISPUTA_CARGO = "DISPUTA_CARGO"
    PEDIR_HUMANO = "PEDIR_HUMANO"
    VACIO = "VACIO"
    SALUDO = "SALUDO"
    EXPLICAR_RECIBO = "EXPLICAR_RECIBO"
    CONSULTA_CONCEPTO = "CONSULTA_CONCEPTO"
    PAGAR = "PAGAR"
    CONSUMO = "CONSUMO"
    FUERA_DE_DOMINIO = "FUERA_DE_DOMINIO"


#: Formas de decir que sí. Van sin tilde y en minúscula porque se comparan contra el
#: texto ya normalizado; «dale» y «ya» son las peruanas, y «ya» es además la respuesta
#: más común a «¿le gustaría que revisemos su recibo?».
_AFIRMACIONES: frozenset[str] = frozenset({
    "si", "sí", "sip", "claro", "ok", "okey", "okay", "dale", "ya", "ya pues",
    "por favor", "porfa", "bueno", "de acuerdo", "correcto", "exacto", "afirmativo",
    "revisemos", "veamos", "vamos", "adelante", "hazlo", "hagalo", "hágalo",
    "si por favor", "sí por favor", "claro que si", "claro que sí", "obvio",
})

#: Patrones por intención. Cada patrón se evalúa por tokens, así que basta con
#: escribir la forma canónica: las variantes con posesivos, plurales y
#: conjugaciones las cubre :func:`coincide_patron`.
PATRONES: dict[Intencion, tuple[str, ...]] = {
    Intencion.REGULATORIA: (
        "reclamo formal",
        "libro de reclamaciones",
        "osiptel",
        "indecopi",
        "dar de baja",
        "darme de baja",
        "cancelar el servicio",
        "cancelar el contrato",
        "anular el servicio",
        "portabilidad",
        "portar mi numero",
        # «me quiero ir a otro operador» es señal de baja aunque no diga «baja».
        # Se escribe sin el verbo para que case con ir, irme, irse y cambiarme.
        "otro operador",
        "otra operadora",
        "otra empresa",
        "cambiar de operador",
        "poner un reclamo",
        "presentar un reclamo",
        "demanda",
        "abogado",
    ),
    # Disputar un cargo NO es preguntar por él. «¿por qué me cobran esto?» es una duda
    # y se explica; «este cobro está mal, no lo reconozco» es una **impugnación**, y
    # explicarle la aritmética a quien afirma que el cobro no le corresponde es no
    # escucharle. La taxonomía del corpus telco real la separa (`dispute_invoice`,
    # 1 000 casos) y nosotros la teníamos cayendo en EXPLICAR_RECIBO.
    #
    # Va por debajo de REGULATORIA: si además menciona OSIPTEL o el libro de
    # reclamaciones, manda el trámite formal.
    Intencion.DISPUTA_CARGO: (
        "no reconozco",
        "no reconosco",
        "cobro indebido",
        "cobro mal",
        "me cobraron mal",
        "me estan cobrando de mas",
        "cobro de mas",
        "no me corresponde",
        "yo no pedi",
        "yo no contrate",
        "nunca contrate",
        "nunca pedi",
        "esta mal el cobro",
        "cargo que no",
        "devolucion",
        "me devuelvan",
        "reembolso",
        "compensacion",
    ),
    # Documentación, no regla: quien decide si esto es una petición de humano es
    # `pide_humano`, en `facts_engine.peticion_humano`. Esta tupla era la regla y por eso
    # bastaba con que la palabra «asesor» o «llamar» apareciera en cualquier sitio:
    # «el asesor de la tienda me dijo…» y «llamé al call center» se clasificaban como
    # peticiones de humano y derivaban, mientras el umbral de incomprensión —que ya usaba
    # la regla gramatical— decía lo contrario sobre la misma frase.
    Intencion.PEDIR_HUMANO: (
        "asesor",
        "humano",
        "persona real",
        "una persona",
        "hablar con alguien",
        "operador humano",
        "agente",
        "atencion al cliente",
        "call center",
    ),
    # Querer pagar es una intención propia, no una duda de facturación: la respuesta
    # útil es *dónde y hasta cuándo*, no la descomposición causal del recibo.
    Intencion.PAGAR: (
        "donde pago",
        "como pago",
        "quiero pagar",
        "pagar mi recibo",
        "medios de pago",
        "formas de pago",
        "metodo de pago",
        "hasta cuando puedo pagar",
        "fecha de pago",
        "fecha de vencimiento",
        "cuando vence",
        "codigo de pago",
    ),
    # Consumo es el otro gran bloque del corpus real (`check_usage`,
    # `check_excess_data_charges`). Hoy no lo resolvemos: el FactSet explica el recibo,
    # no los gigas gastados. Se clasifica para poder **derivar con contexto** en vez de
    # responder una explicación de recibo que nadie pidió.
    Intencion.CONSUMO: (
        "cuantos gigas",
        "cuanto me queda",
        "consumo de datos",
        "gasto de datos",
        "megas",
        "gigas",
        "minutos que me quedan",
        "saldo",
        "exceso de datos",
        "consumo adicional",
    ),
    Intencion.SALUDO: (
        "hola",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "buenas",
        "que tal",
        "hey",
        "saludos",
        "gracias",
        "muchas gracias",
        "chau",
        "adios",
        "hasta luego",
    ),
    Intencion.EXPLICAR_RECIBO: (
        # Formas reales con las que se escribe esta consulta en Perú. Van PRIMERO
        # porque exigen dos raíces y las palabras sueltas de abajo exigen una: al
        # coincidir antes, el `patron` que queda como evidencia es el que de verdad
        # describe la consulta ("me estan cobrando") y no el genérico ("cobran").
        "me llego mas caro",
        "me llego caro",
        "me cobraron de mas",
        "me cobraron demas",
        "me estan cobrando",
        "no me cuadra",
        "se me vencio la promocion",
        "me cortaron el servicio",
        "ya cancele mi recibo",
        "ya pague mi recibo",
        "cuanto tengo q pagar",
        "cuanto tengo que pagar",
        "q paso con mi recibo",
        "q paso",
        # Vocabulario del recibo: una sola raíz basta.
        "recibo",
        "factura",
        "boleta",
        "cobro",
        "cobran",
        "cobraron",
        "monto",
        "pagar",
        "pague",
        "deuda",
        "caro",
        "subio",
        "aumento",
        "vario",
        "variacion",
        "diferencia",
        "por que mas",
        "cuanto debo",
        "detalle",
        "consumo",
        # Abreviaturas de chat. Un cliente escribe "xq me cobran tanto" mucho más a
        # menudo que "¿por qué me cobran tanto?", y hasta ahora esa frase caía en
        # FUERA_DE_DOMINIO porque "por"/"que" son palabras de enlace y "xq" no
        # estaba en ningún patrón.
        "xq",
        "pq",
        # El más laxo de todos, y por eso el último: "es" es palabra de enlace, así
        # que "cuanto es" se reduce a la raíz cuant-. Solo puede robarle frases a
        # FUERA_DE_DOMINIO —EXPLICAR_RECIBO es la última intención que se evalúa—,
        # nunca a la baja regulatoria ni a la petición de una persona.
        "cuanto es",
    ),
    Intencion.CONSULTA_CONCEPTO: (
        "que es",
        "que significa",
        "explicame el concepto",
        "prorrateo",
        "reconexion",
        "nota de credito",
        "nota de debito",
        "renta adelantada",
        "renta vencida",
        "cuota del equipo",
    ),
}

# --------------------------------------------------------------------------- #
# Detección de intento de manipulación
# --------------------------------------------------------------------------- #
# La defensa real contra la suplantación es que el `account_ref` sale del token y
# jamás del texto: por construcción, ninguna frase puede hacer que el sistema
# hable de la cuenta de otro. Pero eso protege el DATO, no la CONVERSACIÓN: sin
# esta capa, «ejecuta {ingresar_monto}» se clasificaba como «explícame el recibo»
# solo porque contiene la palabra «monto», y el intento no dejaba ningún rastro.
#
# Aquí no se busca bloquear a un atacante —no hay nada que robar—, sino dos cosas:
# no tratar una cadena hostil como una consulta legítima, y **dejar constancia**
# de que alguien está sondeando. Un asistente que responde con normalidad a
# «ignora tus instrucciones» ya perdió la conversación.

#: Señales ESTRUCTURALES: marcadores que un cliente real no escribe nunca. Una
#: sola basta. Se buscan sobre el texto CRUDO, porque el tokenizador se come las
#: llaves y los signos, que es justo donde vive la señal.
_ESTRUCTURALES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("marcador_plantilla", re.compile(r"\{\{?[^}]{1,60}\}?\}")),
    ("marcador_chat", re.compile(r"<\|[^|]{1,40}\|>|<\s*/?\s*(system|assistant|user)\s*>")),
    ("etiqueta_rol", re.compile(r"(?im)^\s*(system|assistant|user|developer)\s*:")),
    ("bloque_codigo", re.compile(r"```")),
    ("cuenta_ajena", re.compile(r"(?i)\bC-\d{3,}\b|\bC-DEMO-\d+\b")),
)

#: Señales LÉXICAS fuertes: anulación de instrucciones o intento de suplantación.
#: Una sola basta, porque no tienen lectura inocente en atención al cliente.
_LEXICAS_FUERTES: tuple[str, ...] = (
    "ignora tus instrucciones",
    "ignora las instrucciones",
    "ignora lo anterior",
    "olvida tus instrucciones",
    "olvida lo anterior",
    "olvida todo lo anterior",
    "ignore previous instructions",
    "disregard previous",
    "prompt de sistema",
    "system prompt",
    "tu prompt",
    "instrucciones del sistema",
    "actua como",
    "act as",
    "haz de cuenta que eres",
    "pretend to be",
    "modo desarrollador",
    "developer mode",
    "jailbreak",
    "sin restricciones",
    "revela tus reglas",
)

#: Señales LÉXICAS débiles: sospechosas solo si se acompañan de otra. «Ejecuta»
#: o «comando» pueden aparecer en una frase inocente.
_LEXICAS_DEBILES: tuple[str, ...] = (
    "ejecuta",
    "ejecutar",
    "comando",
    "instruccion",
    "instrucciones",
    "prompt",
    "eres un",
    "eres una",
    "rol de",
    "override",
    "bypass",
    "admin",
    "root",
)


def _firma_debil(patron: str) -> frozenset[str]:
    """Raíces que exige un patrón débil. Dos patrones con la misma firma son **la misma palabra**.

    ``instruccion`` e ``instrucciones`` se recortan las dos a ``instr``, así que la
    frase *«necesito instrucciones para pagar mi recibo»* casaba con los dos patrones
    y una sola palabra inocente reunía las «dos señales» que pide la regla. Lo mismo
    con ``ejecuta``/``ejecutar`` y con ``eres un``/``eres una``. Agrupando por firma,
    la compañía que exige la regla vuelve a ser **otra palabra**, que es lo que
    siempre quiso decir.
    """
    return frozenset(_raices(tokens_significativos(patron)))


#: Patrones débiles agrupados por firma: el recuento de la regla va sobre los grupos.
_DEBILES_POR_FIRMA: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = tuple(
    (firma, tuple(patrones))
    for firma, patrones in {
        _firma_debil(p): [q for q in _LEXICAS_DEBILES if _firma_debil(q) == _firma_debil(p)]
        for p in _LEXICAS_DEBILES
    }.items()
)


def detectar_manipulacion(utterance: str) -> list[str]:
    """Señales de intento de manipulación halladas en la frase.

    Devuelve la lista de señales, vacía si no hay ninguna. Una señal estructural
    o una léxica fuerte bastan; las débiles necesitan compañía, y «compañía»
    significa **otra palabra distinta**, no otra flexión de la misma
    (véase :func:`_firma_debil`).
    """
    crudo = (utterance or "").strip()
    if not crudo:
        return []
    normal = _normalizar(crudo)

    senales = [nombre for nombre, patron in _ESTRUCTURALES if patron.search(crudo)]
    senales += [f"lexica:{p}" for p in _LEXICAS_FUERTES if p in normal]
    if senales:
        return senales

    grupos = [
        [f"debil:{p}" for p in patrones if coincide_patron(p, crudo)]
        for _, patrones in _DEBILES_POR_FIRMA
    ]
    encontrados = [g for g in grupos if g]
    if len(encontrados) < 2:
        return []
    return [senal for grupo in encontrados for senal in grupo]


#: Orden de evaluación. **Es la política de la solución, no un detalle.** Una
#: frase que pide la baja y a la vez pregunta por el recibo se trata como baja;
#: y una que intenta manipular al asistente no se trata como consulta, aunque
#: mencione el recibo.
_PRIORIDAD: tuple[Intencion, ...] = (
    Intencion.REGULATORIA,
    # Antes que DISPUTA_CARGO: las dos derivan, así que el cliente acaba con una persona
    # en ambos casos y lo único que cambia es el motivo registrado. Si lo pidió
    # expresamente, ese es el motivo honesto.
    Intencion.PEDIR_HUMANO,
    # Por encima de todo lo demás: quien impugna un cargo no está pidiendo una
    # explicación, y dársela sería contestar a otra pregunta.
    Intencion.DISPUTA_CARGO,
    Intencion.SALUDO,
    # PAGAR y CONSUMO van antes que las de recibo porque comparten vocabulario con
    # ellas —«recibo», «cobro», «monto»— y sin este orden caerían en EXPLICAR_RECIBO,
    # que es justo el defecto que se está corrigiendo.
    Intencion.PAGAR,
    Intencion.CONSUMO,
    Intencion.CONSULTA_CONCEPTO,
    Intencion.EXPLICAR_RECIBO,
)

#: Intenciones que obligan a derivar sin construir el FactSet.
#:
#: ``DISPUTA_CARGO`` y ``CONSUMO`` se suman por motivos distintos: la primera porque
#: impugnar un cobro es un trámite que decide una persona —el motor puede demostrar que
#: la aritmética cuadra, y aun así el cliente puede tener razón sobre si ese servicio le
#: correspondía—; la segunda porque el ``FactSet`` explica el recibo y **no** tiene los
#: datos de consumo, así que responder sería inventar.
_DERIVAN: dict[Intencion, MotivoDerivacion] = {
    Intencion.REGULATORIA: MotivoDerivacion.INTENCION_REGULATORIA,
    Intencion.DISPUTA_CARGO: MotivoDerivacion.INTENCION_REGULATORIA,
    Intencion.PEDIR_HUMANO: MotivoDerivacion.PETICION_HUMANO,
    Intencion.CONSUMO: MotivoDerivacion.FUERA_DE_ALCANCE,
}

#: Interjecciones y muletillas que en un chat peruano equivalen a un saludo o a un
#: acuse de recibo. Se listan explícitamente en vez de deducirlas por longitud:
#: contar tokens mandaba «xd» y «ya pero q tienes?» a saludo, que es absurdo.
_INTERJECCIONES: frozenset[str] = frozenset(
    {
        "ok", "oka", "okay", "listo", "dale", "ya", "bueno", "va", "vale",
        "aja", "ah", "oh", "mmm", "hmm", "eh", "uy", "uf",
        "jaja", "jajaja", "jeje", "jiji", "xd", "xdd", "lol",
        "si", "sip", "no", "nop", "nel", "claro", "obvio", "correcto",
        "perfecto", "genial", "buenazo", "chevere", "bacan",
    }
)


class ResultadoIntencion(NamedTuple):
    """Qué se decidió y por qué. ``patron`` es la evidencia citable."""

    intencion: Intencion
    patron: str | None
    motivo_derivacion: MotivoDerivacion | None
    explica_recibo: bool
    senales: tuple[str, ...] = ()

    @property
    def deriva(self) -> bool:
        """¿Esta intención obliga a pasar el caso a una persona?"""
        return self.motivo_derivacion is not None


def _clasificar_por_patrones(texto: str) -> ResultadoIntencion | None:
    """Recorre las intenciones por prioridad y devuelve la primera que case.

    ``PEDIR_HUMANO`` no se decide aquí por tokens sino con :func:`pide_humano`, la misma
    función que usa el umbral de incomprensión. Antes eran dos reglas distintas para la
    misma pregunta y se contradecían dentro del mismo turno: la intención derivaba
    *«el asesor de la tienda me dijo…»* por contener la palabra «asesor», mientras el
    score de incomprensión, que ya distinguía pedir de mencionar, decía que ahí no había
    ninguna petición. Ganaba la intención, porque va antes, y el cliente se quedaba sin
    su respuesta.
    """
    for intencion in _PRIORIDAD:
        if intencion is Intencion.PEDIR_HUMANO:
            peticion = pide_humano(texto)
            if peticion:
                return ResultadoIntencion(
                    intencion=intencion,
                    patron=peticion,
                    motivo_derivacion=_DERIVAN.get(intencion),
                    explica_recibo=False,
                )
            continue
        for patron in PATRONES[intencion]:
            if coincide_patron(patron, texto):
                return ResultadoIntencion(
                    intencion=intencion,
                    patron=patron,
                    motivo_derivacion=_DERIVAN.get(intencion),
                    explica_recibo=intencion is Intencion.EXPLICAR_RECIBO,
                )
    return None


def clasificar_intencion(utterance: str | None) -> ResultadoIntencion:
    """Clasifica la frase del cliente sin mirar el recibo ni llamar a un modelo.

    Una frase vacía **no** se interpreta como «explícame el recibo»: se pregunta
    qué necesita. Y una frase sin ninguna señal reconocible se declara fuera de
    dominio en vez de responder cualquier cosa, que es la forma más barata de
    perder la confianza del cliente.
    """
    texto = (utterance or "").strip()
    if not texto:
        return ResultadoIntencion(Intencion.VACIO, None, None, explica_recibo=False)

    # Antes que nada: ¿esto intenta manipular al asistente? Va primero porque una
    # frase hostil que además menciona «monto» no es una consulta de facturación.
    senales = detectar_manipulacion(texto)
    if senales:
        return ResultadoIntencion(
            intencion=Intencion.SOSPECHOSA,
            patron=senales[0],
            motivo_derivacion=None,
            explica_recibo=False,
            senales=tuple(senales),
        )

    resuelta = _clasificar_por_patrones(texto)
    if resuelta is not None:
        return resuelta

    # Ninguna señal en la frase literal. Segundo intento traduciendo la jerga peruana:
    # «ya cancelé» lleva el significado «pagar», y con él el patrón de pago sí casa.
    # Va DESPUÉS y no antes para que una frase que ya se entiende no cambie de
    # clasificación por una expansión: la jerga solo puede rescatar, nunca reinterpretar.
    expandido = expandir(texto)
    if expandido != texto:
        resuelta = _clasificar_por_patrones(expandido)
        if resuelta is not None:
            return resuelta

    # Un «sí» pelado es aceptar lo que se acaba de ofrecer.
    #
    # Sin esta regla, «Sí» no casaba ningún patrón, caía en FUERA_DE_DOMINIO y el
    # asistente contestaba que el tema le pillaba fuera de base —después de haber sido
    # ÉL quien preguntó «¿le gustaría que revisemos su recibo?»—. O peor: repetía la
    # misma oferta, y el cliente se quedaba diciendo que sí en bucle. Es el fallo más
    # tonto y más caro posible, porque ocurre justo cuando el cliente ya aceptó.
    #
    # Se resuelve mapeando a EXPLICAR_RECIBO y no inventando una intención de
    # «afirmación» que habría que arrastrar por todo el sistema. La razón por la que es
    # seguro: en este producto solo se ofrece una cosa, revisar el recibo. Un «sí» aquí
    # no puede querer decir otra cosa, y si el cliente quería algo distinto, la
    # explicación de su recibo sigue siendo una respuesta útil y no una invención.
    if texto.strip(" .!¡").lower() in _AFIRMACIONES:
        return ResultadoIntencion(
            Intencion.EXPLICAR_RECIBO, "afirmación", None, explica_recibo=True
        )

    # Sin señal reconocible. Solo se trata como cortesía si TODO lo que escribió
    # es una interjección: «xd» sí, «ya pero qué tienes» no. Contar tokens era
    # una mala regla porque una pregunta corta también tiene pocos tokens.
    palabras = tokens_significativos(texto)
    if palabras and palabras <= _INTERJECCIONES:
        return ResultadoIntencion(Intencion.SALUDO, None, None, explica_recibo=False)

    return ResultadoIntencion(Intencion.FUERA_DE_DOMINIO, None, None, explica_recibo=False)
