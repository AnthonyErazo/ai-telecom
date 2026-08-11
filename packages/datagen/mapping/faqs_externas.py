"""Ingesta del corpus de FAQs **externo y real** para el RAG.

Por qué hace falta una fuente externa
-------------------------------------
La ficha del desafío prometía *«ejemplos de conversaciones o preguntas frecuentes
anonimizadas»* que **no se entregaron**: el dataset trae cargos, clientes, diccionario y
vídeos, pero ninguna conversación. Hasta ahora ese hueco lo tapábamos con 36 FAQs
**escritas por nosotros**, lo que convertía la métrica de *Retrieval Accuracy* en una
medida sobre vocabulario propio: el sistema recuperaba bien porque preguntábamos con las
mismas palabras con que habíamos escrito el corpus.

Los conceptos NO vienen de aquí
-------------------------------
Conviene separarlo, porque son dos problemas distintos con dos soluciones distintas:

* **Catálogo de conceptos** → sale del dataset del desafío (585 conceptos reales, con la
  descripción que el cliente lee en su recibo). Ver ``catalogo_desafio1.py``. Ningún
  corpus externo mejora eso.
* **Preguntas de clientes** → no vienen en el dataset, y es lo que se ingiere aquí.

Fuente
------
``bitext/Bitext-telco-llm-chatbot-training-dataset``: 26 000 pares pregunta/respuesta del
sector telecomunicaciones, con taxonomía de intención e **erratas reales de cliente**
(«there are charges on my phone bill that i do not *recogniae*»), que son justo lo que un
clasificador de intención tiene que aguantar.

Dos limitaciones que hay que declarar, no esconder
--------------------------------------------------
1. **Está en inglés.** Se busca corpus equivalente en español y no existe con licencia
   utilizable: el único español del dominio está bajo GPL-3.0, prohibida por BASES §9 al
   cederse la propiedad intelectual a Integratel. Por eso el texto se traduce, y la
   traducción queda marcada en cada entrada: nadie debe poder confundir una FAQ traducida
   con una FAQ recogida en Perú.
2. **Licencia ``cdla-sharing-1.0``.** Permite uso y obras derivadas; obliga a compartir
   *los datos* redistribuidos bajo la misma licencia. Aquí no se redistribuyen: el corpus
   se descarga a ``data/``, que el repositorio ignora entero. `[POR VALIDAR con asesoría
   legal si el proyecto se industrializa]`.

Lo que este módulo NO hace
--------------------------
No inventa preguntas. Si la traducción no está disponible, la entrada se guarda en
inglés y marcada como tal, en vez de rellenarla con una redacción nuestra: una FAQ
inventada con apariencia de dato externo es peor que no tener FAQ.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "CATEGORIAS_DE_INTERES",
    "FUENTE_ID",
    "FUENTE_LICENCIA",
    "FUENTE_URL",
    "FaqExterna",
    "descargar",
    "escribir_corpus",
    "muestrear",
]

_LOG = logging.getLogger(__name__)

FUENTE_ID = "bitext/Bitext-telco-llm-chatbot-training-dataset"
FUENTE_URL = (
    "https://huggingface.co/datasets/bitext/Bitext-telco-llm-chatbot-training-dataset"
    "/resolve/main/bitext-telco-llm-chatbot-training-dataset.csv"
)
FUENTE_LICENCIA = "cdla-sharing-1.0"

#: Categorías del corpus que caen dentro del Desafío 1. Se dejan fuera SERVICES,
#: SUBSCRIPTION y CONTACT: son altas, bajas y datos de contacto, y precisamente el
#: comportamiento correcto ante ellas es **derivar**, no explicar el recibo.
CATEGORIAS_DE_INTERES: frozenset[str] = frozenset({"BILLING", "PAYMENT", "COMPLAINTS", "CONSUMPTION"})

#: Cuántas preguntas se conservan por intención. El RAG no necesita volumen sino
#: cobertura: 26 000 filas con 12 intenciones son la misma pregunta reformulada mil
#: veces, y vectorizarlas todas encarece el índice sin añadir un solo matiz.
POR_INTENCION = 40

#: Lenguaje malsonante en el corpus de origen. **189 de las primeras 480 muestras** lo
#: traían («the fucking excess data charges»), lo cual es realista —los clientes enfadados
#: escriben así— pero inaceptable dentro del índice.
#:
#: El motivo no es pudor: el corpus del RAG entra en el **contexto del modelo**. Un
#: fragmento recuperado marca el registro de la respuesta, y arrastrar insultos al
#: contexto de un asistente que la ficha exige *«con tono humano, transparente y
#: horizontal»* es pedirle que los imite. Se filtra la ENTRADA del índice; el clasificador
#: de intención, en cambio, sí debe seguir entendiendo a un cliente que insulta.
#: Ojo con las fronteras de palabra: el corpus trae el insulto **pegado** al término
#: anterior («checkfucking mobile payments», «myfucking usage»), fruto de cómo se
#: generaron las variantes. Con `\bf+u+c+k` se colaban cuatro de 480, porque no hay
#: frontera entre «check» y «fucking». Las raíces inequívocas van sin ancla inicial; las
#: que sí pueden aparecer dentro de palabras corrientes la conservan.
_MALSONANTE = re.compile(
    r"f+u+c+k|\bshit\b|\bdamn\b|\bcrap\b|\bbloody\b|\bass(hole)?\b|\bhell\b|\bstupid\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FaqExterna:
    """Una pregunta frecuente del corpus externo, con su procedencia a la vista."""

    faq_id: str
    pregunta: str
    respuesta: str
    intencion: str
    categoria: str
    idioma: str
    fuente: str = FUENTE_ID
    licencia: str = FUENTE_LICENCIA
    traducida: bool = False

    def a_dict(self) -> dict[str, object]:
        """Forma serializable para el índice del RAG."""
        return asdict(self)


def descargar(destino: Path, *, tiempo_max: float = 120.0) -> Path:
    """Descarga el CSV del corpus a ``destino`` si no está ya.

    Se cachea en disco a propósito: son ~14 MB y volver a pedirlos en cada ejecución
    convertiría una prueba local en una dependencia de red.
    """
    if destino.exists() and destino.stat().st_size > 0:
        _LOG.info("corpus ya descargado en %s", destino)
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    peticion = urllib.request.Request(FUENTE_URL, headers={"User-Agent": "recibo-claro/1.0"})
    with urllib.request.urlopen(peticion, timeout=tiempo_max) as respuesta:
        destino.write_bytes(respuesta.read())
    _LOG.info("corpus descargado: %s KB", destino.stat().st_size // 1024)
    return destino


def muestrear(csv_origen: Path, *, por_intencion: int = POR_INTENCION) -> list[FaqExterna]:
    """Toma una muestra **diversa** del corpus, no las primeras N filas.

    Dentro de cada intención las filas están agrupadas y son casi idénticas entre sí, así
    que se recorre con paso constante en vez de cortar por arriba. El paso es
    determinista —sin ``random``— para que dos ejecuciones den el mismo corpus y el
    índice vectorial sea reproducible.
    """
    por_intent: dict[str, list[dict[str, str]]] = defaultdict(list)
    with csv_origen.open(encoding="utf-8", errors="replace", newline="") as fh:
        for fila in csv.DictReader(fh):
            if (fila.get("category") or "").strip() not in CATEGORIAS_DE_INTERES:
                continue
            texto = f"{fila.get('instruction', '')} {fila.get('response', '')}"
            if _MALSONANTE.search(texto):
                continue
            por_intent[(fila.get("intent") or "").strip()].append(fila)

    salida: list[FaqExterna] = []
    for intencion in sorted(por_intent):
        filas = por_intent[intencion]
        paso = max(1, len(filas) // por_intencion)
        for indice, fila in enumerate(filas[::paso][:por_intencion]):
            pregunta = (fila.get("instruction") or "").strip()
            respuesta = (fila.get("response") or "").strip()
            if not pregunta or not respuesta:
                continue
            salida.append(
                FaqExterna(
                    faq_id=f"EXT-{intencion.upper()}-{indice:03d}",
                    pregunta=pregunta,
                    respuesta=respuesta,
                    intencion=intencion,
                    categoria=(fila.get("category") or "").strip(),
                    idioma="en",
                )
            )
    return salida


def escribir_corpus(destino: Path, faqs: list[FaqExterna]) -> Path:
    """Vuelca el corpus a JSON bajo ``data/`` (que el repositorio ignora)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fuente": FUENTE_ID,
        "url": FUENTE_URL,
        "licencia": FUENTE_LICENCIA,
        "aviso": (
            "Corpus externo en inglés. Las entradas con traducida=true se tradujeron "
            "automáticamente al español; no son preguntas recogidas en Perú."
        ),
        "total": len(faqs),
        "faqs": [f.a_dict() for f in faqs],
    }
    destino.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _LOG.info("corpus escrito en %s: %d FAQs", destino, len(faqs))
    return destino
