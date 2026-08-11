"""Catálogo de **cómo habla el cliente peruano**, no de códigos de facturación.

El error que corrige este módulo
--------------------------------
Hubo una versión anterior que extraía los 732 ``CHARGE_CODE_ID`` del dataset y los
llamaba «catálogo de conceptos». Era el catálogo equivocado: eso son códigos internos
del facturador, y ningún cliente escribe ``RC_PLANRE577`` en un chat.

Lo que la ficha pide es otra cosa, y lo dice con todas las letras:

    *«categorizando los motivos de consulta en **lenguaje cliente** alineado al de la
    atención humana Movistar (ej. prorrateos, reconexiones)»*

Es decir: el puente entre **cómo escribe una persona en Lima** y **cómo se llama eso en
facturación**. Sin ese puente, «xq me llegó más caro» no encuentra nada y «ya cancelé mi
recibo» se interpreta como una baja.

De dónde sale cada término (ninguno inventado por el equipo)
------------------------------------------------------------
========================  ===================================================
``TRANSCRIPCION``         Vídeos oficiales de formación del Desafío 1. Es
                          lenguaje de atención Movistar Perú, literal.
``DATASET``               ``CHARGE_CODE_DESC`` del export de cargos: el nombre
                          comercial que el cliente lee en su propio recibo.
``FICHA``                 Términos citados en la ficha del desafío.
``USO_PERUANO``           Rasgos del español peruano que cambian el sentido y
                          que están documentados en el propio material.
========================  ===================================================

La marca de procedencia viaja en cada entrada. Un catálogo de jerga sin procedencia es
indistinguible de una lista de ocurrencias, y aquí la diferencia importa: si mañana el
jurado pregunta de dónde sale «cancelar = pagar», la respuesta no puede ser «nos pareció».

El caso que más cara cuesta
---------------------------
**En Perú «cancelar» significa pagar.** «Ya cancelé mi recibo» quiere decir *ya lo pagué*,
no *quiero darme de baja*. Un asistente que lo confunda deriva a un cliente satisfecho a
un flujo de bajas: el peor resultado posible de toda la solución.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "PROCEDENCIAS",
    "VOCABULARIO",
    "TerminoCliente",
    "escribir_vocabulario",
    "terminos_desde_dataset",
]

#: Etiquetas de procedencia admitidas. Toda entrada lleva una.
PROCEDENCIAS: frozenset[str] = frozenset({"TRANSCRIPCION", "DATASET", "FICHA", "USO_PERUANO"})


@dataclass(slots=True)
class TerminoCliente:
    """Un término tal y como lo usa el cliente, con su equivalencia y su origen."""

    termino: str
    significa: str
    concepto_id: str | None
    procedencia: str
    nota: str = ""
    variantes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.procedencia not in PROCEDENCIAS:
            raise ValueError(f"procedencia desconocida: {self.procedencia!r}")

    def a_dict(self) -> dict[str, Any]:
        """Forma serializable para indexar en el RAG."""
        return asdict(self)


#: Vocabulario del cliente peruano. Cada entrada cita de dónde sale.
VOCABULARIO: tuple[TerminoCliente, ...] = (
    # --- El falso amigo que puede arruinar una demostración --------------------
    TerminoCliente(
        termino="cancelar",
        significa="pagar",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota=(
            "En Perú «cancelar» es PAGAR. «Ya cancelé mi recibo» = ya lo pagué. Solo "
            "significa dar de baja si va acompañado de «el servicio», «la línea» o «el "
            "contrato». Por eso la regla de baja exige DOS raíces: cancel- Y servici-."
        ),
        variantes=["cancele", "cancelé", "ya cancele", "cancelado"],
    ),
    # --- Cómo se nombra el documento -------------------------------------------
    TerminoCliente(
        termino="recibo",
        significa="documento de cobro mensual",
        concepto_id=None,
        procedencia="TRANSCRIPCION",
        nota=(
            "Movistar Perú dice «recibo», no «factura». El vídeo «Planta» lo usa 14 veces "
            "y «factura» ninguna en boca del cliente. Responder «factura» suena a otro país."
        ),
        variantes=["mi recibo", "el recibo", "recibito"],
    ),
    # --- Los dos conceptos que la ficha nombra como ejemplo ---------------------
    TerminoCliente(
        termino="prorrateo",
        significa="cobro proporcional por los días usados",
        concepto_id="PRORRATEO_PLAN",
        procedencia="TRANSCRIPCION",
        nota=(
            "Literal del vídeo: «se cobrará solo la porción de los días consumidos: "
            "prorrateo, que serán 3 días a 6 soles». El cliente rara vez usa la palabra; "
            "dice «me cobraron unos días» o «medio mes»."
        ),
        variantes=["prorrateado", "proporcional", "me cobraron unos dias", "medio mes"],
    ),
    TerminoCliente(
        termino="reconexion",
        significa="cargo por reactivar el servicio tras un corte",
        concepto_id="CARGO_RECONEXION",
        procedencia="FICHA",
        nota="La ficha lo cita como ejemplo de concepto en lenguaje cliente.",
        variantes=["reconexion", "me reconectaron", "por reconectar", "reactivacion"],
    ),
    # --- Las dos modalidades de renta, del vídeo oficial ------------------------
    TerminoCliente(
        termino="renta adelantada",
        significa="se factura antes de disfrutar el plan",
        concepto_id=None,
        procedencia="TRANSCRIPCION",
        nota="Vídeo «Alta y porta», 00:22. El cliente no usa el término: dice «me cobran por adelantado».",
        variantes=["renta adelantada", "RA", "me cobran adelantado", "pago por adelantado"],
    ),
    TerminoCliente(
        termino="renta vencida",
        significa="se disfruta el plan y se factura después",
        concepto_id=None,
        procedencia="TRANSCRIPCION",
        nota=(
            "Vídeo «Alta y porta», 00:27. Aparece como «RV » al inicio de la descripción "
            "en el propio recibo del cliente, así que sí puede citarlo textualmente."
        ),
        variantes=["renta vencida", "RV", "pago despues", "primero uso y luego pago"],
    ),
    TerminoCliente(
        termino="cargo fijo",
        significa="la renta mensual del plan",
        concepto_id="RENTA_PLAN_MOVIL",
        procedencia="TRANSCRIPCION",
        nota=(
            "Término que el asesor usa con el cliente en el vídeo «Planta» («información "
            "sobre su cargo fijo»). Es además el ÚNICO concepto sobre el que aplica el "
            "descuento de alta o portabilidad."
        ),
        variantes=["cargo fijo", "CF", "la renta", "mi plan", "lo fijo"],
    ),
    # --- Cómo se pregunta de verdad --------------------------------------------
    TerminoCliente(
        termino="por que me llego mas caro",
        significa="pregunta canónica de variación de recibo",
        concepto_id=None,
        procedencia="FICHA",
        nota=(
            "La ficha la cita literal como una de las tres preguntas canónicas. En chat "
            "se escribe casi siempre abreviada y sin tildes."
        ),
        variantes=["xq me llego mas caro", "pq subio mi recibo", "porque me vino mas caro",
                   "q paso con mi recibo", "porque esta mas caro"],
    ),
    TerminoCliente(
        termino="que me estan cobrando",
        significa="pregunta canónica de desglose",
        concepto_id=None,
        procedencia="FICHA",
        variantes=["q me cobran", "que es este cobro", "de que es este monto"],
    ),
    # --- Dinero, en peruano ------------------------------------------------------
    TerminoCliente(
        termino="plata",
        significa="dinero",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Coloquial y neutro en Perú; no es vulgar. «Me cobraron más plata».",
        variantes=["plata", "lucas"],
    ),
    TerminoCliente(
        termino="soles",
        significa="moneda peruana (PEN)",
        concepto_id=None,
        procedencia="TRANSCRIPCION",
        nota="El vídeo dice «60 soles», nunca «S/ 60.00» hablado. En texto sí aparece «S/».",
        variantes=["soles", "S/", "sol"],
    ),
    # --- Jerga conversacional peruana -----------------------------------------
    # Sin esto, «me botaron del plan» y «pásame con un pata» caían en
    # FUERA_DE_DOMINIO: la primera es una consulta de recibo y la segunda una
    # petición de asesor, que es precisamente lo que mide la precisión de hand-off.
    # `significa` se redacta con el vocabulario de PATRONES, no con una definición
    # de diccionario: la expansión solo sirve si aporta tokens que el clasificador
    # ya reconoce.
    TerminoCliente(
        termino="achorado",
        significa="molesto",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Enfadado. Señal de escalamiento.",
        variantes=("achorarse",),
    ),
    TerminoCliente(
        termino="al toque",
        significa="urgente",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="De inmediato. Señal de urgencia.",
        variantes=("altoque",),
    ),
    TerminoCliente(
        termino="bamba",
        significa="cobro que no reconozco",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Falso. «Este cobro es bamba» = no lo reconozco.",
        variantes=("bambeado",),
    ),
    TerminoCliente(
        termino="botar",
        significa="cortaron el servicio",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Echar. «Me botaron del plan» = me cortaron el servicio.",
        variantes=("botaron", "me botaron"),
    ),
    TerminoCliente(
        termino="chancar",
        significa="consumo",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Agotar. «Me chancaron los megas» = consumo agotado.",
        variantes=("chancaron", "chancado"),
    ),
    TerminoCliente(
        termino="chapar",
        significa="contratar",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Contratar o conseguir. «Chapé el plan de 39» = contraté ese plan.",
        variantes=("chape", "chapo", "chapé"),
    ),
    TerminoCliente(
        termino="china",
        significa="medio sol",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Cincuenta céntimos.",
        variantes=("chinas",),
    ),
    TerminoCliente(
        termino="choro",
        significa="cobro que no reconozco",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Robo. Aplicado al recibo es una impugnación, no una duda.",
        variantes=("choreo", "me chorearon"),
    ),
    TerminoCliente(
        termino="cobrar de mas",
        significa="cobro que no reconozco",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Impugnación explícita del importe.",
        variantes=("cobraron de mas", "cobro de mas"),
    ),
    TerminoCliente(
        termino="de frente",
        significa="directamente",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Directamente, sin rodeos.",
        variantes=("defrente",),
    ),
    TerminoCliente(
        termino="figurar",
        significa="aparece cobro",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Aparecer en el recibo. «No figura mi descuento».",
        variantes=("figura", "figuraba"),
    ),
    TerminoCliente(
        termino="floro",
        significa="explicacion confusa",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Palabrería. Señal de que la explicación anterior no convenció.",
        variantes=("florear", "me floreo"),
    ),
    TerminoCliente(
        termino="harto",
        significa="mucho",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Mucho. «Me cobraron harto».",
        variantes=("harta",),
    ),
    TerminoCliente(
        termino="jalar",
        significa="no funciona",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Funcionar. «No jala el internet» = el servicio no funciona.",
        variantes=("jala", "no jala", "jalaba"),
    ),
    TerminoCliente(
        termino="luca",
        significa="sol",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Un sol. «Me cobraron 50 lucas» = 50 soles.",
        variantes=("lucas",),
    ),
    TerminoCliente(
        termino="meter",
        significa="cobro adicional",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="«Me metieron un cobro» = añadieron un cargo que no esperaba.",
        variantes=("metieron", "me metieron"),
    ),
    TerminoCliente(
        termino="misio",
        significa="no puedo pagar",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Sin dinero. Señal de intención de pago, no de disputa.",
        variantes=("misia", "estoy misio"),
    ),
    TerminoCliente(
        termino="mostro",
        significa="muy bueno",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Muy bueno. Elogio, no queja: evita leerlo como reclamo.",
        variantes=("mostra",),
    ),
    TerminoCliente(
        termino="nel",
        significa="no",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="No.",
        variantes=("nelson",),
    ),
    TerminoCliente(
        termino="palta",
        significa="problema",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Problema o apuro. «Qué palta con mi recibo».",
        variantes=("paltearse", "paltiado", "paltiada"),
    ),
    TerminoCliente(
        termino="pata",
        significa="asesor",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Amigo. «Pásame con un pata» = pide una persona.",
        variantes=("patas", "causa", "brother"),
    ),
    TerminoCliente(
        termino="plan pe",
        significa="plan",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="«Pe» es muletilla de cierre peruana, sin significado propio.",
        variantes=("pe", "pues"),
    ),
    TerminoCliente(
        termino="recontra",
        significa="muy",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Intensificador. «Recontra caro» = muy caro.",
        variantes=("rercontra",),
    ),
    TerminoCliente(
        termino="roche",
        significa="problema",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Vergüenza o problema.",
        variantes=("rochoso",),
    ),
    TerminoCliente(
        termino="sale caro",
        significa="cobro mas caro",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="El recibo llegó por encima de lo esperado.",
        variantes=("salio caro", "me sale caro"),
    ),
    TerminoCliente(
        termino="subir",
        significa="cobro mas caro",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="«Me subieron el recibo» = el cobro salió más caro.",
        variantes=("subio", "me subieron"),
    ),
    TerminoCliente(
        termino="tumbar",
        significa="cortaron el servicio",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Cortar el servicio. «Me tumbaron la señal» = me cortaron el servicio.",
        variantes=("tumbaron", "tumbo", "me tumbaron"),
    ),
    TerminoCliente(
        termino="yapa",
        significa="cobro adicional",
        concepto_id=None,
        procedencia="USO_PERUANO",
        nota="Añadido. En un recibo, «me metieron una yapa» = un cobro adicional.",
        variantes=("de yapa", "yapita"),
    ),
)


#: Abreviaturas de chat peruano. No son términos de facturación: son **la forma de
#: escribir**, y sin ellas el clasificador no entiende un mensaje real.
#:
#: Salen de observar el propio corpus telco (que trae erratas auténticas) y del uso
#: documentado en el material; no se inventan formas que nadie escriba.
ABREVIATURAS: dict[str, str] = {
    "xq": "por que",
    "pq": "por que",
    "q": "que",
    "k": "que",
    "tmb": "tambien",
    "xfa": "por favor",
    "porfa": "por favor",
    "dnd": "donde",
    "cnd": "cuando",
    "x": "por",
    "d": "de",
    "toy": "estoy",
    "ta": "esta",
    "pa": "para",
    "ntc": "no te creas",
}


#: Ruido comercial que el cliente NO teclea. «RV Plan Mi Movistar S/29.9 VII» es como
#: figura en el recibo; nadie escribe eso en un chat: escribe «mi plan movistar». Quitar
#: el precio, el numeral romano y la marca de renta multiplica las coincidencias.
_RUIDO_COMERCIAL = (
    re.compile(r"^RV\s+", re.IGNORECASE),          # marca de renta vencida
    re.compile(r"\s*S/\s*[\d.,]+"),                # precio incrustado
    re.compile(r"\s*\(VR\s*[^)]*\)", re.IGNORECASE),  # valor de referencia del bono
    re.compile(r"\s+(?:[IVX]{1,4})$"),             # numeral romano de versión
    re.compile(r"\s+x\s*\d+\s*m(?:es(?:es)?)?\b", re.IGNORECASE),  # «x 6 Meses»
)


def _sin_tildes(texto: str) -> str:
    """Forma sin tildes: como se teclea en un chat peruano la mayoría de las veces."""
    import unicodedata

    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def nombre_tecleable(descripcion: str) -> str:
    """Convierte el nombre del recibo en lo que el cliente escribiría.

    «RV Plan Mi Movistar S/29.9 VII» → «plan mi movistar». Sin esto, el término indexado
    lleva un precio dentro y no casa nunca con la pregunta real.
    """
    limpio = descripcion
    for patron in _RUIDO_COMERCIAL:
        limpio = patron.sub(" ", limpio)
    return re.sub(r"\s+", " ", limpio).strip().lower()


def variantes_de(termino: str) -> list[str]:
    """Formas en que ese término aparecería escrito en un chat.

    Se generan por reglas y no a mano: son cientos de términos y escribirlos uno a uno
    garantiza que el catálogo deje de crecer el día que nadie tenga tiempo.
    """
    base = termino.lower().strip()
    formas = {base, _sin_tildes(base)}
    # Abreviaturas de chat aplicadas palabra a palabra.
    inverso = {v: k for k, v in ABREVIATURAS.items() if len(v) > 2}
    for largo, corto in inverso.items():
        if largo in base:
            formas.add(_sin_tildes(base.replace(largo, corto)))
    # Sin artículos iniciales: el cliente dice «plan movistar», no «el plan movistar».
    formas.add(re.sub(r"^(el|la|los|las|mi|mis)\s+", "", _sin_tildes(base)))
    return sorted(f for f in formas if f and f != base)


def terminos_desde_dataset(catalogo_json: Path, *, minimo_apariciones: int = 5) -> list[TerminoCliente]:
    """Extrae del dataset los nombres comerciales que el cliente **sí** ve en su recibo.

    Esto es lo único que el catálogo de códigos aporta al vocabulario: no los códigos,
    sino la ``CHARGE_CODE_DESC``, que es texto que el cliente puede citar literalmente
    («¿qué es este *Plan Ahorro Elige más*?»).

    Se filtra por frecuencia porque de 732 códigos la mayoría aparece un puñado de veces:
    un nombre que sale en 12 recibos no es vocabulario, es una excepción.
    """
    if not catalogo_json.exists():
        return []
    datos = json.loads(catalogo_json.read_text(encoding="utf-8"))
    salida: list[TerminoCliente] = []
    vistos: set[str] = set()
    for concepto in datos.get("conceptos", []):
        if not concepto.get("considerado") or concepto.get("apariciones", 0) < minimo_apariciones:
            continue
        nombre = (concepto.get("nombre_comercial") or "").strip()
        limpio = nombre_tecleable(nombre)
        if not limpio or limpio in vistos:
            continue
        vistos.add(limpio)
        salida.append(
            TerminoCliente(
                termino=limpio,
                significa=f"nombre comercial del concepto {concepto['concepto_id']}",
                concepto_id=concepto["concepto_id"],
                procedencia="DATASET",
                nota=(
                    f"aparece en {concepto['apariciones']:,} cargos · "
                    f"grupo {concepto.get('grupo', '')} · en el recibo: «{nombre}»"
                ),
                variantes=variantes_de(limpio),
            )
        )
    return salida


def terminos_desde_supabase(minimo_apariciones: int = 5) -> list[TerminoCliente]:
    """Igual que :func:`terminos_desde_dataset`, pero leyendo la vista ``v_concepto_real``.

    Es la vía que hace el catálogo **automático**: al recargar el dataset en Supabase, la
    vista se recalcula sola y volver a ejecutar esto trae los términos nuevos sin tocar
    código. La versión sobre fichero se conserva para trabajar sin red.

    Devuelve lista vacía —en vez de fallar— si no hay conexión configurada: generar el
    vocabulario no debe ser un requisito duro para arrancar el proyecto.
    """
    import os

    cadena = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not cadena:
        return []
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg es opcional aquí
        return []
    consulta = """
        SELECT concepto_id, nombre_comercial, grupo, apariciones
        FROM v_concepto_real
        WHERE considerado AND apariciones >= %s
        ORDER BY apariciones DESC
    """
    salida: list[TerminoCliente] = []
    vistos: set[str] = set()
    with psycopg.connect(cadena, connect_timeout=20) as conexion:
        for concepto_id, nombre, grupo, apariciones in conexion.execute(consulta, (minimo_apariciones,)):
            limpio = nombre_tecleable(nombre or "")
            if not limpio or limpio in vistos:
                continue
            vistos.add(limpio)
            salida.append(
                TerminoCliente(
                    termino=limpio,
                    significa=f"nombre comercial del concepto {concepto_id}",
                    concepto_id=concepto_id,
                    procedencia="DATASET",
                    nota=f"aparece en {apariciones:,} cargos · grupo {grupo} · en el recibo: «{nombre}»",
                    variantes=variantes_de(limpio),
                )
            )
    return salida


def escribir_vocabulario(destino: Path, catalogo_json: Path | None = None) -> Path:
    """Vuelca el vocabulario completo a JSON, listo para Supabase y para el RAG."""
    terminos = list(VOCABULARIO)
    # Supabase primero: es la fuente que se actualiza sola al recargar el dataset. El
    # fichero es el respaldo para trabajar sin conexión.
    desde_bd = terminos_desde_supabase()
    if desde_bd:
        terminos.extend(desde_bd)
    elif catalogo_json is not None:
        terminos.extend(terminos_desde_dataset(catalogo_json))
    destino.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "descripcion": "Cómo habla el cliente peruano y a qué concepto de facturación equivale",
        "procedencias": dict(Counter(t.procedencia for t in terminos)),
        "total": len(terminos),
        "abreviaturas": ABREVIATURAS,
        "terminos": [t.a_dict() for t in terminos],
    }
    destino.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destino
