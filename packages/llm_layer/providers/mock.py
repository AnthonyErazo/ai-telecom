"""``MockProvider``: el proveedor determinístico de la demo. **No toca la red.**

Emula a un modelo de lenguaje sin serlo: lee el mismo prompt que recibiría Gemini,
recupera de él el bloque ``FACTSET``, elige la plantilla de la causa dominante,
rellena los huecos con cifras ya formateadas y aplica un *jitter* léxico
determinístico —tres sinónimos por frase, sorteados con una semilla derivada de
``sha256(factset_id)``— para que el texto no parezca una plantilla.

Tres propiedades lo hacen útil de verdad:

* **Byte-reproducible.** La misma cuenta y el mismo periodo producen exactamente el
  mismo texto, hoy y en el escenario del jurado.
* **Sin canales laterales.** Solo usa lo que va dentro del prompt: si el mock puede
  redactar la respuesta, un proveedor externo también podría.
* **Numéricamente inerte.** El jitter solo sustituye palabras; un guardián compara
  los dígitos antes y después y descarta cualquier variación que los altere.

Es, además, la red de seguridad de la demo: con ``LLM_MODE=mock`` no hace falta ni
API key ni conexión a internet.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any

from packages.llm_layer.plantillas import (
    DatosPlantilla,
    construir_datos,
    recortar_seguro,
    renderizar_explicacion,
)
from packages.llm_layer.prompts import extraer_bloque
from packages.llm_layer.providers.base import (
    ErrorRespuestaInvalida,
    ExplicacionLLM,
)

__all__ = ["SINONIMOS", "MockProvider", "aplicar_jitter", "semilla_de"]

#: Tres variantes por frase. La primera es la redacción base, de modo que "sin
#: jitter" sigue siendo una de las salidas posibles. Ninguna variante contiene
#: dígitos: el jitter no puede alterar una cifra ni por accidente.
SINONIMOS: tuple[tuple[str, tuple[str, str, str]], ...] = (
    (r"\bmás alto que\b", ("más alto que", "mayor que", "más caro que")),
    (r"\bmás bajo que\b", ("más bajo que", "menor que", "más barato que")),
    (r"\bporque\b", ("porque", "debido a que", "y esto se debe a que")),
    (r"\bexplica\b", ("explica", "corresponde a", "representa")),
    (r"\ben este recibo\b", ("en este recibo", "en el recibo de este mes", "en este documento")),
    (r"\bse cobra\b", ("se cobra", "se factura", "se carga")),
    (r"\bsuma\b", ("suma", "aporta", "agrega")),
    (r"\bdescuenta\b", ("descuenta", "resta", "abona a su favor")),
    (r"\bRecuerde que\b", ("Recuerde que", "Tenga presente que", "No olvide que")),
    (r"\bSi desea\b", ("Si desea", "Si gusta", "Cuando guste")),
    (r"\ble muestro\b", ("le muestro", "le detallo", "le comparto")),
    (r"\bAdemás,", ("Además,", "Adicionalmente,", "Sumado a eso,")),
)

_PATRONES_JITTER: tuple[tuple[re.Pattern[str], tuple[str, str, str]], ...] = tuple(
    (re.compile(patron), variantes) for patron, variantes in SINONIMOS
)

_DIGITOS = re.compile(r"\d+")


def semilla_de(factset_id: str, turno_numero: int = 0) -> int:
    """Semilla determinística del jitter: los 64 bits altos de ``sha256(factset_id)``.

    ``turno_numero`` (0 en el primer turno) se mezcla en la semilla a partir del
    segundo turno, para que la segunda pregunta sobre el mismo recibo no elija
    exactamente los mismos sinónimos —y por tanto no repita el mismo texto— que la
    primera. En el turno 0 la semilla es **idéntica** a como era antes de esto: no
    se le agrega sufijo, para no invalidar ninguna salida ya fijada como golden.
    """
    base = str(factset_id) if turno_numero <= 0 else f"{factset_id}#{turno_numero}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def aplicar_jitter(texto: str, azar: random.Random) -> str:
    """Sustituye frases por sinónimos, sin tocar una sola cifra.

    Si la sustitución alterase los números del texto (no debería: ninguna variante
    lleva dígitos), se descarta y se devuelve el original. Es una guarda barata
    frente a un futuro sinónimo mal escrito.
    """
    resultado = texto
    for patron, variantes in _PATRONES_JITTER:
        resultado = patron.sub(lambda _m, v=variantes: v[azar.randrange(len(v))], resultado)
    if _DIGITOS.findall(resultado) != _DIGITOS.findall(texto):  # pragma: no cover - guarda
        return texto
    return resultado


class MockProvider:
    """Proveedor determinístico basado en plantillas. Cumple :class:`ProveedorLLM`."""

    nombre = "mock"
    version_modelo = "mock-plantillas-1.0.0"

    def __init__(self, *, jitter: bool = True, **_opciones: Any) -> None:
        """Crea el proveedor.

        Args:
            jitter: si es ``False``, el texto es la plantilla literal (útil para
                comparar salidas byte a byte entre versiones de la plantilla).
        """
        self.jitter = jitter

    # ------------------------------------------------------------------ #
    # Contrato ProveedorLLM
    # ------------------------------------------------------------------ #
    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict:
        """Genera la explicación a partir del propio prompt. Nunca abre una conexión.

        Raises:
            ErrorRespuestaInvalida: si el prompt no trae el bloque ``FACTSET`` (no se
                inventa nada: el generador degradará a la plantilla determinística).
        """
        del esquema, timeout_s  # la salida se valida contra ExplicacionLLM, no hay red
        datos, turno_numero = self._datos_del_prompt(prompt)
        explicacion = renderizar_explicacion(datos)
        if self.jitter:
            explicacion = self._aplicar_jitter(explicacion, datos, turno_numero)
        # La salida SIEMPRE es válida: se revalida antes de devolverla.
        return ExplicacionLLM.model_validate(explicacion.model_dump()).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # Interno
    # ------------------------------------------------------------------ #
    def _datos_del_prompt(self, prompt: str) -> tuple[DatosPlantilla, int]:
        """Reconstruye los datos de plantilla leyendo los bloques del prompt.

        Devuelve también ``turno_numero`` (0 en el primer turno de la conversación),
        que ``construir_prompt`` ya deja en ``PARAMETROS`` — es cuántas respuestas
        previas del asistente había al armar el prompt.
        """
        bruto_factset = extraer_bloque(prompt, "FACTSET")
        if not bruto_factset:
            raise ErrorRespuestaInvalida(
                "el prompt no contiene el bloque FACTSET: el mock no genera sin hechos",
                proveedor=self.nombre,
            )
        try:
            resumen = json.loads(bruto_factset)
        except json.JSONDecodeError as exc:
            raise ErrorRespuestaInvalida(
                f"el bloque FACTSET del prompt no es JSON válido: {exc}", proveedor=self.nombre
            ) from exc

        try:
            parametros = json.loads(extraer_bloque(prompt, "PARAMETROS") or "{}")
        except json.JSONDecodeError:
            parametros = {}

        datos = construir_datos(
            resumen,
            verbosidad=parametros.get("verbosidad", "CORTO"),
            factset_id=str(parametros.get("factset_id", "")),
        )
        turno_numero = int(parametros.get("turno_numero") or 0)
        return datos, turno_numero

    def _aplicar_jitter(
        self, explicacion: ExplicacionLLM, datos: DatosPlantilla, turno_numero: int = 0
    ) -> ExplicacionLLM:
        """Aplica el jitter al resumen y a cada frase, con una única semilla."""
        azar = random.Random(semilla_de(datos.factset_id, turno_numero))
        return explicacion.model_copy(
            update={
                "resumen": recortar_seguro(aplicar_jitter(explicacion.resumen, azar), 180),
                "causas": [
                    causa.model_copy(update={"frase": aplicar_jitter(causa.frase, azar)})
                    for causa in explicacion.causas
                ],
            }
        )
