"""Cuándo el cliente **pide** una persona. Fuente única de verdad de la regla (c).

La política de derivación tiene tres casos y este módulo resuelve el tercero: *si el
cliente pide un asesor, se le da, sin regatear y sin mirar el score*. Es una regla dura y
determinista, y por eso vive en su propio módulo: la consultan el umbral de incomprensión
(``facts_engine.confianza``), el clasificador de intención (``facts_engine.intencion``) y
el generador de la suite golden (``eval.generar_golden``). Cuando cada uno tenía su
propia lista de palabras, los tres discrepaban entre sí y el sistema se contradecía en el
mismo turno: la intención derivaba «el asesor de la tienda me dijo…» mientras el score
decía que no había ninguna petición.

MENCIONAR NO ES PEDIR
---------------------
La regla era «¿aparece la palabra *asesor* en la frase?». Con lenguaje real de cliente
peruano eso deriva de más, que es lo contrario de lo que se busca::

    «el asesor de la tienda me dijo que mi plan costaba 55 soles»    → derivaba
    «me atendió un agente y no me explicó nada»                      → derivaba
    «llamé al call center y no me resolvieron, por eso escribo aquí» → derivaba
    «NO hace falta que me pases con una persona»                     → derivaba

Las cuatro **mencionan** a una persona; ninguna la **pide**. Dos cuentan algo que ya
pasó y una es un rechazo explícito del traspaso. Derivar ahí no protege a nadie: le
quita al cliente la respuesta que venía a buscar y hunde la Precisión del Hand-off con
falsos positivos.

La regla pasa a ser gramatical y sigue siendo determinista, sin modelo ni umbral: hace
falta un **verbo de petición** y, después, un **sustantivo de persona**, en la misma
cláusula y sin una negación pegada al verbo. Es una expresión regular que se puede leer
en voz alta delante de un jurado y defender línea a línea.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "PATRONES_PETICION_HUMANO",
    "normalizar_texto",
    "pide_humano",
]


def normalizar_texto(texto: str) -> str:
    """Minúsculas sin tildes ni signos: la forma canónica para comparar frases."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(caracter for caracter in descompuesto if unicodedata.category(caracter) != "Mn")


#: Sustantivos con los que un cliente peruano se refiere a la persona que le atendería.
_PERSONA = (
    r"(?:asesor(?:a|es|as)?|humano|persona(?:\s+real)?|gente|alguien|operador(?:a)?"
    r"|ejecutiv[oa]|representante|agente|supervisor(?:a)?|teleoperador(?:a)?"
    r"|atencion\s+al\s+cliente|call\s*center|telefono\s+de\s+atencion)"
)

#: Verbos con los que se PIDE algo, en presente, imperativo, infinitivo o subjuntivo.
#: Deliberadamente **no** incluyen formas de pasado («me atendió», «llamé»): contar lo
#: que ya pasó no es pedir que vuelva a pasar.
_VERBO_PETICION = (
    r"(?:quiero|quisiera|deseo|necesito|requiero|deme|dame|permitame"
    r"|puede[sn]?|podria[sn]?|puedo|podria"
    r"|pas(?:a|as|e|es|ame|eme|arme|armelo|arle)|derive(?:me)?|deriva(?:me|r|rme)?"
    r"|transfiere(?:me)?|transferir(?:me)?|comunica(?:me|r|rme)?|comuniqueme"
    r"|contacta(?:me|r|rme)?|conectame|ponme|pongame|poner(?:me)?"
    r"|habla(?:r)?|hable|conversar|converse|atienda(?:me)?|atender(?:me)?"
    # «llamé» y «llame» son la misma cadena una vez quitadas las tildes, y la primera es
    # un pretérito: «llamé al call center y no me resolvieron» cuenta un pasado, no pide
    # nada. Solo se aceptan las formas con clítico, inequívocamente de petición.
    r"|llamarme|llamenme|llamame|revise|explique)"
)

#: Locuciones con las que el cliente RECHAZA el traspaso. Si aparecen en la cláusula, esa
#: cláusula no cuenta como petición por mucho que nombre a una persona.
_RECHAZO_HUMANO = re.compile(
    r"\bno\s+(?:hace\s+falta|es\s+necesario|hace\s+falta\s+que|quiero\s+que\s+me\s+pas"
    r"|me\s+pase[sn]?|me\s+derive[sn]?|me\s+transfiera[sn]?)"
    r"|\bsin\s+(?:pasar|hablar|derivar|que\s+me\s+pase)"
)

#: Negación **pegada** al verbo de petición («no quiero», «no me pases»). La adyacencia
#: se exige a propósito: en «no entiendo nada, quiero hablar con un asesor» el «no» niega
#: «entiendo», no «quiero», y esa frase tiene que derivar.
_NEGACION_ADYACENTE = re.compile(
    rf"\b(?:no|nunca|jamas|tampoco)\s+(?:me\s+|te\s+|se\s+|lo\s+)?{_VERBO_PETICION}\b"
)

#: Petición = verbo de petición y, hasta seis palabras después, una persona.
_PATRON_PETICION_HUMANO = re.compile(
    rf"\b{_VERBO_PETICION}\b(?:\s+\w+){{0,6}}?\s+(?:un[ao]?s?\s+|el\s+|la\s+|mi\s+)?{_PERSONA}\b"
)

#: «otro operador» es la competencia, no una persona: en Perú "operador" nombra también a
#: la propia telco. La portabilidad deriva igual, pero por INTENCION_REGULATORIA, que es
#: su motivo real: mandar al asesor un expediente que dice «pidió hablar con alguien»
#: cuando lo que pidió fue irse a otra compañía es enviarle a la conversación equivocada.
_OPERADOR_COMPETENCIA = re.compile(r"\b(?:otr[oa]|nuev[oa]|distint[oa])\s+operador(?:a)?\b")

#: Separadores de cláusula. La petición se evalúa cláusula a cláusula para que
#: «no quiero pagar de más, quiero hablar con un asesor» derive por la segunda.
_SEPARADOR_CLAUSULA = re.compile(r"[,;.:!?¡¿]+|\bpero\b|\baunque\b|\bsin\s+embargo\b")

#: Lista legible de los sustantivos de persona que reconoce :func:`pide_humano`. Es
#: documentación y cobertura de la suite golden —hay un caso por forma de pedir—, **no**
#: la regla: la regla es la gramática de arriba, y comprobar pertenencia a esta tupla no
#: equivale a llamar a :func:`pide_humano`.
PATRONES_PETICION_HUMANO: tuple[str, ...] = (
    "asesor",
    "humano",
    "persona real",
    "una persona",
    "operador",
    "ejecutivo",
    "representante",
    "agente",
    "alguien",
    "supervisor",
    "atencion al cliente",
    "call center",
    "telefono de atencion",
)


def pide_humano(texto: str) -> str | None:
    """Devuelve la frase con la que el cliente **pide** una persona, o ``None``.

    Es la regla dura ``PETICION_HUMANO`` y el punto (c) de la política de derivación: si
    el cliente pide un asesor se deriva siempre, sin regatear y sin mirar el score. Lo
    que esta función añade es la distinción entre pedir y mencionar:

    * pide       → *"quiero hablar con una persona"*, *"¿me pasa con un asesor?"*
    * menciona   → *"el asesor de la tienda me dijo…"*, *"llamé al call center"*
    * rechaza    → *"no hace falta que me pases con una persona"*

    Sólo la primera deriva. El texto se parte en cláusulas y cada una se examina por
    separado, porque *"no quiero pagar de más, quiero hablar con un asesor"* es una
    petición aunque empiece por una negación.

    Args:
        texto: el mensaje del cliente tal cual. Se normaliza aquí dentro.

    Returns:
        El trozo de frase que constituye la petición —va literal a la bitácora y al
        resumen del asesor, para que la decisión se pueda auditar— o ``None``.
    """
    normalizado = normalizar_texto(texto or "")
    if not normalizado.strip():
        return None
    for clausula in _SEPARADOR_CLAUSULA.split(normalizado):
        if not clausula or not clausula.strip():
            continue
        if _RECHAZO_HUMANO.search(clausula) or _NEGACION_ADYACENTE.search(clausula):
            continue
        # "otro operador" se tapa para que no cuente como persona.
        limpia = _OPERADOR_COMPETENCIA.sub(" ", clausula)
        encontrado = _PATRON_PETICION_HUMANO.search(limpia)
        if encontrado:
            return encontrado.group(0).strip()
    return None
