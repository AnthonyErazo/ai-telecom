"""Verificador numérico (sección 5.3). **Código, no modelo.**

Este módulo es la razón por la que el proyecto puede prometer *"cero invenciones
financieras comprobables mediante logs de la terminal"*. Funciona en cuatro pasos,
sin ninguna llamada externa y sin ningún juicio subjetivo:

1. :func:`construir_permitidos` levanta el conjunto ``ALLOWED`` **exclusivamente**
   desde el FactSet: los valores literales, sus renderizados en las tres escrituras
   peruanas (``S/ 124.90`` · ``124,90`` · ``124.90``), los días, los porcentajes, las
   fechas, los números de cuota y —lista cerrada— lo que de ahí se deriva por
   **álgebra permitida**: suma, resta, diferencia de fechas en días, cociente
   ``días/D``, porcentaje y redondeo al céntimo. Cada derivación queda **registrada**
   con su regla y sus operandos.
2. :func:`extraer_aserciones` recorre el texto final con una única expresión regular
   maestra y saca todas las cifras: importes en formato peruano, porcentajes, fechas
   en cuatro formatos, cantidades de días y "cuota N de M".
3. Cada cifra se normaliza al mismo vocabulario de tokens que usa el FactSet
   (``cent:`` · ``num:`` · ``pct:`` · ``fecha:`` · ``periodo:``).
4. Si un token no está en ``ALLOWED``, la aserción queda **NO_ANCLADA** y el veredicto
   es **FAIL**. Una respuesta con veredicto FAIL no se entrega: se reintenta una vez y,
   si vuelve a fallar, se sustituye por la plantilla determinística.

Además se validan dos cosas de la salida estructurada del modelo:
``cifras_usadas ⊆ ALLOWED`` y ``Σ causas.monto_cent_citado == delta_total_cent``
(con la misma tolerancia de ±1 céntimo que la especificación concede al invariante de
conciliación; por encima de eso es un descuadre y bloquea).

Métrica comprometida: ``TA_respuesta = 0``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.dinero import (
    a_centimos,
    aplicar_porcentaje,
    formatear_soles,
    prorratear,
    variantes_monto,
)
from packages.core_domain.enums import EstadoAsercion, VeredictoVerificacion
from packages.core_domain.esquemas.factset import (
    TOLERANCIA_RESIDUAL_CENT,
    FactSet,
    token_entero,
    token_fecha,
    token_monto,
    token_periodo,
    token_porcentaje,
)
from packages.core_domain.esquemas.recibo import MESES_ES
from packages.core_domain.esquemas.respuesta import Asercion, Cita
from packages.llm_layer.providers.base import ExplicacionLLM

__all__ = [
    "MAXIMO_DERIVADOS",
    "REGLAS_ALGEBRA",
    "VAR_ESTRICTO",
    "ConjuntoPermitido",
    "DerivacionNumerica",
    "ResultadoVerificacion",
    "construir_permitidos",
    "extraer_aserciones",
    "extraer_numeros",
    "inyectar_alucinacion",
    "verificar",
]

_LOG = logging.getLogger(__name__)

#: Variable de entorno que activa el modo estricto (por defecto, activo).
VAR_ESTRICTO = "VERIFICADOR_ESTRICTO"

#: **Lista cerrada** de álgebra permitida. Nada fuera de esto deriva un número.
REGLAS_ALGEBRA: tuple[str, ...] = (
    "suma",
    "resta",
    "diferencia_fechas_dias",
    "cociente_dias_ciclo",
    "porcentaje",
    "redondeo_centimo",
)

#: Tope de seguridad del conjunto derivado (evita explosiones combinatorias).
MAXIMO_DERIVADOS = 60_000

#: Tope de operandos monetarios que entran en las combinaciones por pares.
TOPE_OPERANDOS = 80


def _estricto_por_defecto() -> bool:
    """Lee ``VERIFICADOR_ESTRICTO`` del entorno (por defecto ``True``)."""
    bruto = os.getenv(VAR_ESTRICTO)
    if bruto is None:
        return True
    return bruto.strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


# --------------------------------------------------------------------------- #
# Conjunto permitido
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DerivacionNumerica:
    """Una cifra que no está en el FactSet pero se obtiene de él por álgebra permitida.

    Se registra íntegra —regla, operandos y fuentes— porque el compromiso del
    proyecto no es "el número parece razonable" sino "aquí está de dónde sale".
    """

    token: str
    regla: str
    operandos: tuple[str, ...]
    fuentes: tuple[str, ...]
    explicacion: str

    def a_dict(self) -> dict[str, Any]:
        """Representación serializable para el evento ``VERIFY`` de la auditoría."""
        return {
            "token": self.token,
            "regla": self.regla,
            "operandos": list(self.operandos),
            "fuentes": list(self.fuentes),
            "explicacion": self.explicacion,
        }


class ConjuntoPermitido:
    """``ALLOWED``: todo lo que el texto puede decir sin mentir.

    Se construye **solo** desde el FactSet (regla innegociable nº 4). Distingue dos
    grados de justificación:

    * **anclado** — el token está literalmente en el FactSet, con sus ``fact_id``.
    * **derivado** — se obtiene por una de las seis reglas de :data:`REGLAS_ALGEBRA`,
      con la derivación registrada.

    Lo que no es ni una cosa ni la otra es una alucinación numérica.
    """

    __slots__ = ("_anclados", "_derivados", "_literales", "factset_sha256", "parametros")

    def __init__(
        self,
        anclados: dict[str, list[str]],
        derivados: dict[str, DerivacionNumerica],
        literales: set[str],
        *,
        factset_sha256: str = "",
        parametros: dict[str, Any] | None = None,
    ) -> None:
        self._anclados = anclados
        self._derivados = derivados
        self._literales = literales
        self.factset_sha256 = factset_sha256
        self.parametros = parametros or {}

    # -- consulta ------------------------------------------------------- #
    @property
    def anclados(self) -> dict[str, list[str]]:
        """Tokens literales del FactSet con sus ``fact_id`` de respaldo."""
        return self._anclados

    @property
    def derivados(self) -> dict[str, DerivacionNumerica]:
        """Tokens obtenidos por álgebra permitida, con su derivación registrada."""
        return self._derivados

    @property
    def literales(self) -> set[str]:
        """Renderizados textuales admitidos (``"S/ 124.90"``, ``"124,90"``, ``"124.90"``)."""
        return self._literales

    def tokens(self) -> set[str]:
        """Todos los tokens admisibles: anclados ∪ derivados."""
        return set(self._anclados) | set(self._derivados)

    def __contains__(self, token: object) -> bool:
        return str(token) in self._anclados or str(token) in self._derivados

    def __len__(self) -> int:
        return len(self._anclados) + len(self._derivados)

    def estado(self, token: str) -> EstadoAsercion:
        """Estado de anclaje de un token."""
        if token in self._anclados:
            return EstadoAsercion.ANCLADA
        if token in self._derivados:
            return EstadoAsercion.DERIVADA
        return EstadoAsercion.NO_ANCLADA

    def fuentes(self, token: str) -> list[str]:
        """``fact_id`` que respaldan un token (los de la derivación, si es derivado)."""
        if token in self._anclados:
            return list(self._anclados[token])
        derivacion = self._derivados.get(token)
        return list(derivacion.fuentes) if derivacion else []

    def fuente(self, token: str) -> str | None:
        """Primer ``fact_id`` de respaldo, que es el que se cita."""
        fuentes = self.fuentes(token)
        return fuentes[0] if fuentes else None

    def derivacion(self, token: str) -> DerivacionNumerica | None:
        """Derivación registrada de un token, si lo es."""
        return self._derivados.get(token)

    def registro_derivaciones(self, limite: int | None = None) -> list[dict[str, Any]]:
        """Registro completo de derivaciones (o las ``limite`` primeras) para el log."""
        items = [derivacion.a_dict() for derivacion in self._derivados.values()]
        return items if limite is None else items[:limite]


# --------------------------------------------------------------------------- #
# Construcción de ALLOWED
# --------------------------------------------------------------------------- #
_NUMEROS_EN_TEXTO = re.compile(r"\d+")


def _valores_por_prefijo(anclados: dict[str, list[str]], prefijo: str) -> dict[str, str]:
    """Devuelve ``{cuerpo_del_token: token}`` para todos los tokens de un prefijo."""
    salida: dict[str, str] = {}
    for token in anclados:
        if token.startswith(prefijo):
            salida[token[len(prefijo) :]] = token
    return salida


def construir_permitidos(
    factset: FactSet,
    *,
    con_algebra: bool = True,
    tope_operandos: int = TOPE_OPERANDOS,
) -> ConjuntoPermitido:
    """Construye el conjunto ``ALLOWED`` del verificador a partir del FactSet.

    Args:
        factset: la única fuente de cifras admitida.
        con_algebra: si es ``False``, solo se admite lo literal (modo de diagnóstico:
            sirve para medir cuánto aporta realmente el álgebra permitida).
        tope_operandos: cuántos importes entran en las combinaciones por pares.

    Returns:
        El conjunto con los tokens anclados, los derivados y sus registros.

    **Qué se ancla además de** :meth:`FactSet.mapa_tokens`: los enteros que forman
    parte de un nombre propio del propio FactSet (``"Paquete 5 GB"``). Son cifras que
    están literalmente en los hechos, así que se anclan —pero **solo** como enteros
    adimensionales (``num:``), nunca como importes: un monto no puede "colarse" a
    través de un nombre comercial.
    """
    anclados: dict[str, list[str]] = {
        token: list(fuentes) for token, fuentes in factset.mapa_tokens().items()
    }
    _anclar_enteros_de_textos(factset, anclados)

    literales: set[str] = set()
    for cuerpo in _valores_por_prefijo(anclados, "cent:"):
        try:
            literales |= variantes_monto(int(cuerpo))
        except ValueError:  # pragma: no cover - los tokens los genera el propio FactSet
            continue

    derivados: dict[str, DerivacionNumerica] = {}
    if con_algebra:
        _derivar(factset, anclados, derivados, tope_operandos)

    return ConjuntoPermitido(
        anclados,
        derivados,
        literales,
        factset_sha256=factset.sha256 or factset.calcular_sha256(),
        parametros={
            "con_algebra": con_algebra,
            "tope_operandos": tope_operandos,
            "reglas": list(REGLAS_ALGEBRA),
            "anclados": len(anclados),
            "derivados": len(derivados),
        },
    )


def textos_nominales(factset: FactSet) -> list[tuple[str, str]]:
    """Nombres propios que el FactSet aporta, con su ``fact_id``.

    Son los nombres comerciales de las líneas, las etiquetas de causa, los equipos
    financiados, los beneficios y el plan vigente: texto del propio recibo, no redacción
    nuestra. Se publica como función porque tiene dos consumidores que deben ver
    exactamente la misma lista —el anclaje de enteros y la protección de nombres propios
    de :func:`verificar`—, y dos listas paralelas acabarían discrepando.
    """
    textos: list[tuple[str, str]] = []
    for linea in factset.lineas:
        textos.append((f"texto:linea:{linea.concepto_id}.nombre_comercial", linea.nombre_comercial))
    for causa in factset.causas_agregadas:
        textos.append(
            (f"texto:causa:{causa.causa or 'SIN_CAUSA'}.etiqueta", causa.etiqueta_cliente)
        )
    for plan in factset.financiamientos:
        textos.append((f"texto:financiamiento:{plan.equipo}", plan.equipo))
    for indice, beneficio in enumerate(factset.beneficios_vigentes):
        textos.append((f"texto:beneficio:{indice}", beneficio))
    if factset.plan_vigente:
        textos.append(("texto:factset:plan_vigente", factset.plan_vigente))
    return textos


def _anclar_enteros_de_textos(factset: FactSet, anclados: dict[str, list[str]]) -> None:
    """Ancla como ``num:`` los dígitos contenidos en los textos del FactSet."""
    for fact_id, texto in textos_nominales(factset):
        for bruto in _NUMEROS_EN_TEXTO.findall(texto or ""):
            token = token_entero(int(bruto))
            fuentes = anclados.setdefault(token, [])
            if fact_id not in fuentes:
                fuentes.append(fact_id)


def _derivar(
    factset: FactSet,
    anclados: dict[str, list[str]],
    derivados: dict[str, DerivacionNumerica],
    tope_operandos: int,
) -> None:
    """Aplica la lista cerrada de álgebra permitida y registra cada derivación."""

    def registrar(
        token: str,
        regla: str,
        operandos: tuple[str, ...],
        explicacion: str,
    ) -> None:
        if token in anclados or token in derivados or len(derivados) >= MAXIMO_DERIVADOS:
            return
        fuentes: list[str] = []
        for operando in operandos:
            for fuente in anclados.get(operando, []):
                if fuente not in fuentes:
                    fuentes.append(fuente)
        derivados[token] = DerivacionNumerica(
            token=token,
            regla=regla,
            operandos=operandos,
            fuentes=tuple(fuentes[:6]),
            explicacion=explicacion,
        )

    montos = sorted(
        {int(cuerpo) for cuerpo in _valores_por_prefijo(anclados, "cent:")},
        key=lambda valor: (-abs(valor), valor),
    )[:tope_operandos]
    enteros = sorted({int(cuerpo) for cuerpo in _valores_por_prefijo(anclados, "num:")})
    fechas: list[date] = []
    for cuerpo in _valores_por_prefijo(anclados, "fecha:"):
        try:
            fechas.append(date.fromisoformat(cuerpo))
        except ValueError:  # pragma: no cover
            continue
    porcentajes: list[Decimal] = []
    for cuerpo in _valores_por_prefijo(anclados, "pct:"):
        try:
            porcentajes.append(Decimal(cuerpo))
        except InvalidOperation:  # pragma: no cover
            continue

    # 1 y 2. Suma y resta de importes anclados (por pares).
    for indice, primero in enumerate(montos):
        tk_a = token_monto(primero)
        for segundo in montos[indice:]:
            tk_b = token_monto(segundo)
            registrar(
                token_monto(primero + segundo),
                "suma",
                (tk_a, tk_b),
                f"{primero} + {segundo} = {primero + segundo}",
            )
            registrar(
                token_monto(primero - segundo),
                "resta",
                (tk_a, tk_b),
                f"{primero} - {segundo} = {primero - segundo}",
            )
            registrar(
                token_monto(segundo - primero),
                "resta",
                (tk_b, tk_a),
                f"{segundo} - {primero} = {segundo - primero}",
            )

    # 3. Diferencia de fechas en días.
    for indice, primera in enumerate(fechas):
        for segunda in fechas[indice + 1 :]:
            dias = abs((primera - segunda).days)
            registrar(
                token_entero(dias),
                "diferencia_fechas_dias",
                (token_fecha(primera), token_fecha(segunda)),
                f"({primera} - {segunda}) = {dias} días",
            )

    # 4. Cociente días/D: la proporción del ciclo y el prorrateo que de ella resulta.
    dias_ciclo = factset.dias_ciclo
    if dias_ciclo > 0:
        tk_d = token_entero(dias_ciclo)
        for dias in enteros:
            if not 0 < dias <= dias_ciclo * 2:
                continue
            proporcion = (Decimal(dias) * 100 / Decimal(dias_ciclo)).quantize(Decimal("0.01"))
            registrar(
                token_porcentaje(proporcion),
                "cociente_dias_ciclo",
                (token_entero(dias), tk_d),
                f"{dias}/{dias_ciclo} = {proporcion} %",
            )
            for monto in montos:
                prorrateado = prorratear(monto, dias, dias_ciclo)
                registrar(
                    token_monto(prorrateado),
                    "cociente_dias_ciclo",
                    (token_monto(monto), token_entero(dias), tk_d),
                    f"{monto} · {dias}/{dias_ciclo} = {prorrateado}",
                )
                # 6. Redondeo al céntimo: el reparto por mayor resto puede mover ±1.
                for ajuste in (-1, 1):
                    registrar(
                        token_monto(prorrateado + ajuste),
                        "redondeo_centimo",
                        (token_monto(prorrateado),),
                        f"{prorrateado} ± 1 céntimo por redondeo de reparto",
                    )

    # 5. Porcentaje: participaciones sobre denominadores con significado en el recibo,
    #    y aplicación de un porcentaje anclado sobre un importe anclado.
    denominadores = [
        ("factset:total_actual_cent", factset.total_actual_cent),
        ("factset:total_previo_cent", factset.total_previo_cent),
        ("factset:delta_total_cent", abs(factset.delta_total_cent)),
        ("factset:total_a_pagar_cent", factset.total_a_pagar_cent),
    ]
    for fact_id, total in denominadores:
        if total == 0:
            continue
        for monto in montos:
            participacion = (Decimal(abs(monto)) * 100 / Decimal(abs(total))).quantize(
                Decimal("0.01")
            )
            registrar(
                token_porcentaje(participacion),
                "porcentaje",
                (token_monto(monto), token_monto(total)),
                f"{abs(monto)} sobre {abs(total)} = {participacion} % ({fact_id})",
            )
    for porcentaje in porcentajes:
        puntos_basicos = int((porcentaje * 100).to_integral_value())
        for monto in montos:
            aplicado = aplicar_porcentaje(monto, puntos_basicos)
            registrar(
                token_monto(aplicado),
                "porcentaje",
                (token_monto(monto), token_porcentaje(porcentaje)),
                f"{porcentaje} % de {monto} = {aplicado}",
            )

    if len(derivados) >= MAXIMO_DERIVADOS:  # pragma: no cover - tope defensivo
        _LOG.warning("conjunto derivado truncado en %s tokens", MAXIMO_DERIVADOS)


# --------------------------------------------------------------------------- #
# Extracción de aserciones
# --------------------------------------------------------------------------- #
_MESES_ALT = "|".join([*MESES_ES.values(), "septiembre"])
_MES_A_NUMERO: dict[str, int] = {nombre: numero for numero, nombre in MESES_ES.items()}
_MES_A_NUMERO["septiembre"] = 9

_NUM = r"\d+(?:[.,]\d+)*"

#: Expresión maestra: un solo recorrido, sin solapamientos. El orden de las
#: alternativas ES la prioridad (porcentaje antes que importe, importe antes que
#: entero suelto), de modo que cada carácter del texto se consume una sola vez.
PATRON_ASERCIONES = re.compile(
    rf"""
    (?P<periodo>\b\d{{4}}-(?:0[1-9]|1[0-2])\b(?!-\d))
  | (?P<fecha_iso>\b\d{{4}}-\d{{1,2}}-\d{{1,2}}\b)
  | (?P<fecha_num>\b\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}\b)
  | (?P<porcentaje>-?\d+(?:[.,]\d+)?\s*%)
  | (?P<monto>-?\s*S/\.?\s*-?{_NUM}|-?\d{{1,3}}(?:[.,]\d{{3}})*[.,]\d+\b)
  | (?P<cuota>\bcuota\s+(?P<cuota_n>\d+)\s+de\s+(?P<cuota_m>\d+)\b)
  | (?P<fecha_txt>\b(?P<dia_txt>\d{{1,2}})\s+de\s+(?P<mes_txt>{_MESES_ALT})
        (?:\s+de\s+(?P<anio_txt>\d{{4}}))?\b)
  | (?P<entero>(?<![\w.,/:-])\d+(?![\w.,/:-]))
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: Token que se emite cuando una cifra se reconoce pero no se puede normalizar.
#: Nunca pertenece a ``ALLOWED``: ante la duda, se bloquea.
PREFIJO_SIN_NORMALIZAR = "sin_normalizar:"


def _asercion(texto: str, token: str, inicio: int, fin: int) -> Asercion:
    """Crea una aserción pendiente de veredicto (el estado lo fija :func:`verificar`)."""
    return Asercion(
        texto_original=texto,
        token=token,
        estado=EstadoAsercion.NO_ANCLADA,
        inicio=inicio,
        fin=fin,
    )


def extraer_aserciones(texto: str) -> list[Asercion]:
    """Extrae del texto **todas** las cifras, ya normalizadas a tokens.

    Reconoce importes en formato peruano (``S/ 1,234.50``, ``S/. 124,90``, ``124.90``,
    con signo o entre paréntesis), porcentajes, fechas en cuatro formatos
    (``2026-07-12``, ``12/07/2026``, ``12-07-2026``, ``12 de julio de 2026``),
    cantidades de días, ``"cuota N de M"``, periodos ``YYYY-MM`` y cualquier entero
    suelto que quede.

    Las aserciones vuelven con estado ``NO_ANCLADA``: el veredicto lo asigna
    :func:`verificar` contra el conjunto permitido.
    """
    aserciones: list[Asercion] = []
    for encontrado in PATRON_ASERCIONES.finditer(texto or ""):
        crudo = encontrado.group(0)
        inicio, fin = encontrado.span()
        grupo = encontrado.lastgroup or ""

        if encontrado.group("periodo"):
            aserciones.append(_asercion(crudo, token_periodo(crudo), inicio, fin))
            continue

        if encontrado.group("fecha_iso"):
            aserciones.append(_asercion(crudo, _token_fecha_segura(crudo), inicio, fin))
            continue

        if encontrado.group("fecha_num"):
            aserciones.append(_asercion(crudo, _token_fecha_numerica(crudo), inicio, fin))
            continue

        if encontrado.group("porcentaje"):
            aserciones.append(_asercion(crudo, _token_porcentaje_seguro(crudo), inicio, fin))
            continue

        if encontrado.group("monto"):
            aserciones.append(_asercion(crudo, _token_monto_seguro(crudo), inicio, fin))
            continue

        if encontrado.group("cuota"):
            numero = encontrado.group("cuota_n")
            total = encontrado.group("cuota_m")
            aserciones.append(_asercion(f"cuota {numero}", token_entero(int(numero)), inicio, fin))
            aserciones.append(_asercion(f"de {total}", token_entero(int(total)), inicio, fin))
            continue

        if encontrado.group("fecha_txt"):
            aserciones.extend(_aserciones_fecha_textual(encontrado, inicio, fin))
            continue

        if grupo == "entero" or encontrado.group("entero"):
            aserciones.append(_asercion(crudo, token_entero(int(crudo)), inicio, fin))

    return aserciones


#: Un nombre propio solo protege si es lo bastante específico para no ser un comodín.
#: Un supuesto "nombre" corto o sin una sola letra no ampara nada: la protección tiene
#: que ser imposible de provocar desde el texto generado.
_LONGITUD_MINIMA_NOMBRE = 6


def tramos_de_nombres_propios(texto: str, factset: FactSet) -> list[tuple[int, int, str]]:
    """Tramos del texto ocupados por un nombre propio del FactSet citado literalmente.

    Por qué hace falta
    ------------------
    El dataset real trae nombres comerciales con cifras dentro: la cuenta 100032914
    factura un concepto llamado literalmente ``"RV Plan Mi Movistar S/55.9 VII"``. Al
    citarlo —que es lo correcto: es el nombre que el cliente ve impreso en su recibo— el
    extractor leía ``S/55.9`` como un importe afirmado sobre el recibo, no lo encontraba
    en ``ALLOWED`` y bloqueaba una explicación por lo demás impecable. El sistema acababa
    derivando a un asesor por haber llamado a las cosas por su nombre.

    Por qué es seguro
    -----------------
    Esto **no amplía** ``ALLOWED``: no se añade ni un token al conjunto permitido, de
    modo que ``S/55.9`` sigue sin poder aparecer en ninguna otra parte del texto. Lo que
    se reconoce es una propiedad **posicional**: los caracteres que caen dentro de una
    cita literal y completa de un nombre propio del FactSet forman parte de ese nombre y
    no son una afirmación sobre importes. Escribir *"su plan cuesta S/55.9"* sigue siendo
    FAIL; escribir el nombre entero del producto, no.

    Returns:
        Tríos ``(inicio, fin, fact_id)`` para que cada cifra amparada quede citada con
        el hecho exacto que la ampara, igual que cualquier otra cifra anclada.
    """
    tramos: list[tuple[int, int, str]] = []
    if not texto:
        return tramos
    for fact_id, nombre in textos_nominales(factset):
        limpio = (nombre or "").strip()
        if len(limpio) < _LONGITUD_MINIMA_NOMBRE or not any(c.isalpha() for c in limpio):
            continue
        # Se admite cualquier separación de espacios porque las plantillas normalizan el
        # texto, pero el resto del nombre tiene que aparecer al pie de la letra.
        patron = re.compile(r"\s+".join(re.escape(parte) for parte in limpio.split()), re.IGNORECASE)
        tramos.extend((hallado.start(), hallado.end(), fact_id) for hallado in patron.finditer(texto))
    return tramos


def extraer_numeros(texto: str) -> set[str]:
    """Conjunto de tokens numéricos presentes en el texto.

    Es el ayudante que usa el test golden: ``extraer_numeros(resp.texto) -
    fs.tokens_permitidos()`` debe ser vacío.
    """
    return {asercion.token for asercion in extraer_aserciones(texto)}


def _token_monto_seguro(crudo: str) -> str:
    """Normaliza un importe escrito a ``cent:``; si no se puede, token no anclable."""
    try:
        return token_monto(a_centimos(crudo.strip()))
    except (ValueError, TypeError):
        return f"{PREFIJO_SIN_NORMALIZAR}{crudo.strip()}"


def _token_porcentaje_seguro(crudo: str) -> str:
    """Normaliza un porcentaje escrito a ``pct:`` con dos decimales."""
    try:
        return token_porcentaje(crudo.replace("%", "").strip())
    except (ValueError, InvalidOperation):
        return f"{PREFIJO_SIN_NORMALIZAR}{crudo.strip()}"


def _token_fecha_segura(crudo: str) -> str:
    """Normaliza una fecha ISO (admite ``2026-7-2``)."""
    try:
        anio, mes, dia = (int(parte) for parte in crudo.split("-"))
        return token_fecha(date(anio, mes, dia))
    except ValueError:
        return f"{PREFIJO_SIN_NORMALIZAR}{crudo}"


def _token_fecha_numerica(crudo: str) -> str:
    """Normaliza ``DD/MM/YYYY`` o ``DD-MM-YYYY`` (convención peruana: día primero)."""
    partes = re.split(r"[/-]", crudo)
    try:
        dia, mes, anio = (int(parte) for parte in partes)
        if anio < 100:
            anio += 2000
        return token_fecha(date(anio, mes, dia))
    except ValueError:
        return f"{PREFIJO_SIN_NORMALIZAR}{crudo}"


def _aserciones_fecha_textual(encontrado: re.Match[str], inicio: int, fin: int) -> list[Asercion]:
    """Convierte ``"12 de julio [de 2026]"`` en tokens.

    Con año se ancla la fecha completa; sin año se ancla solo el número de día, que es
    exactamente lo que el FactSet publica para las etiquetas de tramo ("del 1 al 12 de
    julio"): ``tramo.inicio.dia`` y ``tramo.fin_inclusivo.dia``.
    """
    dia = int(encontrado.group("dia_txt"))
    mes = _MES_A_NUMERO.get((encontrado.group("mes_txt") or "").lower(), 0)
    anio = encontrado.group("anio_txt")
    crudo = encontrado.group(0)

    if anio and mes:
        try:
            return [_asercion(crudo, token_fecha(date(int(anio), mes, dia)), inicio, fin)]
        except ValueError:
            return [_asercion(crudo, f"{PREFIJO_SIN_NORMALIZAR}{crudo}", inicio, fin)]

    salida = [_asercion(f"{dia} de …", token_entero(dia), inicio, fin)]
    if anio:
        salida.append(_asercion(anio, token_entero(int(anio)), inicio, fin))
    return salida


# --------------------------------------------------------------------------- #
# Veredicto
# --------------------------------------------------------------------------- #
class ResultadoVerificacion(BaseModel):
    """Veredicto completo del verificador. Es lo que se escribe en el log ``VERIFY``."""

    model_config = ConfigDict(extra="forbid")

    veredicto: VeredictoVerificacion
    aserciones: list[Asercion] = Field(default_factory=list)
    aserciones_totales: int = 0
    ancladas: int = 0
    derivadas: int = 0
    no_ancladas: int = 0
    infractores: list[str] = Field(
        default_factory=list, description="Texto literal de las cifras no ancladas"
    )
    tokens_infractores: list[str] = Field(default_factory=list)
    citas: list[Cita] = Field(default_factory=list)
    derivaciones: list[DerivacionNumerica] = Field(
        default_factory=list, description="Solo las efectivamente utilizadas por el texto"
    )
    errores_estructurales: list[str] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)
    factset_sha256: str = ""
    estricto: bool = True

    @property
    def anclado(self) -> bool:
        """``True`` si ninguna cifra quedó sin anclar y no hay errores estructurales."""
        return self.no_ancladas == 0 and not self.errores_estructurales

    @property
    def paso(self) -> bool:
        """``True`` si el veredicto no es FAIL (PASS o NO_APLICA)."""
        return self.veredicto is not VeredictoVerificacion.FAIL

    def a_evento_auditoria(self) -> dict[str, Any]:
        """Carga útil del evento ``VERIFY``: la lista completa de aserciones."""
        return {
            "veredicto": str(self.veredicto),
            "estricto": self.estricto,
            "factset_sha256": self.factset_sha256,
            "aserciones_totales": self.aserciones_totales,
            "ancladas": self.ancladas,
            "derivadas": self.derivadas,
            "no_ancladas": self.no_ancladas,
            "infractores": list(self.infractores),
            "errores_estructurales": list(self.errores_estructurales),
            "avisos": list(self.avisos),
            "aserciones": [
                {
                    "texto": asercion.texto_original,
                    "token": asercion.token,
                    "estado": str(asercion.estado),
                    "fuente": asercion.fuente,
                    "derivacion": asercion.derivacion,
                }
                for asercion in self.aserciones
            ],
            "derivaciones": [derivacion.a_dict() for derivacion in self.derivaciones],
        }

    def lineas_terminal(self) -> list[str]:
        """Vista de terminal (máx. 6 líneas) — la prueba que exige la ficha.

        *"Tasa de Alucinación: cero invenciones financieras comprobables mediante logs
        de la terminal."*
        """
        lineas = [
            f"VERIFICACION {self.veredicto}  factset={self.factset_sha256[:12]}",
            (
                f"AFIRMACIONES NUMÉRICAS {self.aserciones_totales} · "
                f"ANCLADAS {self.ancladas} · DERIVADAS {self.derivadas} · "
                f"NO ANCLADAS {self.no_ancladas}"
            ),
        ]
        for derivacion in self.derivaciones[:2]:
            lineas.append(f"  derivada [{derivacion.regla}] {derivacion.explicacion}")
        if self.infractores:
            lineas.append(f"  NO ANCLADAS: {', '.join(self.infractores[:6])}")
        for error in self.errores_estructurales[:1]:
            lineas.append(f"  ESTRUCTURAL: {error}")
        return lineas[:6]


def _nombre_que_ampara(
    asercion: Asercion, tramos: Sequence[tuple[int, int, str]]
) -> str | None:
    """``fact_id`` del nombre propio que contiene la aserción, si alguno la contiene.

    Se exige que la cifra caiga **entera** dentro del tramo: una aserción que solo
    solapa parcialmente con el nombre no está amparada, porque entonces la cifra no es
    la del nombre sino otra pegada a él.
    """
    if asercion.inicio is None or asercion.fin is None:
        return None
    for inicio, fin, fact_id in tramos:
        if inicio <= asercion.inicio and asercion.fin <= fin:
            return fact_id
    return None


def verificar(
    texto: str,
    factset: FactSet,
    *,
    permitidos: ConjuntoPermitido | None = None,
    salida_llm: ExplicacionLLM | None = None,
    estricto: bool | None = None,
    tolerancia_cent: int = TOLERANCIA_RESIDUAL_CENT,
) -> ResultadoVerificacion:
    """Audita el texto final contra el FactSet y emite el veredicto.

    Args:
        texto: exactamente lo que vería el cliente (``RespuestaCanalAgnostica.texto``).
        factset: los hechos verificados.
        permitidos: conjunto ya construido (se reutiliza entre intentos).
        salida_llm: salida estructurada del modelo, para las validaciones del paso 5.
        estricto: si ``True`` (por defecto, o ``VERIFICADOR_ESTRICTO``), una sola
            aserción no anclada basta para FAIL.
        tolerancia_cent: descuadre admitido entre ``Σ causas`` y ``delta_total_cent``.
            Es la misma tolerancia de ±1 céntimo que la especificación concede al
            invariante de conciliación; por encima, bloquea.

    Returns:
        El veredicto con la lista completa de aserciones, sus fuentes y las citas.
    """
    modo_estricto = _estricto_por_defecto() if estricto is None else estricto
    conjunto = permitidos or construir_permitidos(factset)

    aserciones = extraer_aserciones(texto)
    # Cifras que viven DENTRO del nombre propio de un concepto ("RV Plan Mi Movistar
    # S/55.9 VII"). No son afirmaciones sobre el recibo: son parte del nombre que el
    # operador imprime, y el FactSet las trae textualmente.
    tramos_nominales = tramos_de_nombres_propios(texto, factset)
    citas: list[Cita] = []
    derivaciones: list[DerivacionNumerica] = []
    infractores: list[str] = []
    tokens_infractores: list[str] = []
    ancladas = derivadas = no_ancladas = 0

    for asercion in aserciones:
        estado = conjunto.estado(asercion.token)
        # El amparo solo se plantea cuando el token no estaba ya permitido: nunca
        # sustituye a una fuente real, solo cubre lo que si no quedaría sin anclar.
        amparo = (
            _nombre_que_ampara(asercion, tramos_nominales)
            if estado is EstadoAsercion.NO_ANCLADA
            else None
        )
        if amparo is not None:
            estado = EstadoAsercion.ANCLADA
        asercion.estado = estado
        if estado is EstadoAsercion.ANCLADA:
            ancladas += 1
            asercion.fuente = amparo or conjunto.fuente(asercion.token)
        elif estado is EstadoAsercion.DERIVADA:
            derivadas += 1
            derivacion = conjunto.derivacion(asercion.token)
            if derivacion is not None:
                asercion.fuente = derivacion.fuentes[0] if derivacion.fuentes else None
                asercion.derivacion = f"[{derivacion.regla}] {derivacion.explicacion}"
                if derivacion not in derivaciones:
                    derivaciones.append(derivacion)
        else:
            no_ancladas += 1
            infractores.append(asercion.texto_original.strip())
            tokens_infractores.append(asercion.token)
            continue

        if asercion.inicio is not None and asercion.fin is not None:
            citas.append(
                Cita(
                    inicio=asercion.inicio,
                    fin=asercion.fin,
                    fact_id=asercion.fuente or f"derivada:{asercion.token}",
                    token=asercion.token,
                )
            )

    errores, avisos = _validar_estructura(factset, conjunto, salida_llm, tolerancia_cent)

    if not (texto or "").strip():
        veredicto = VeredictoVerificacion.NO_APLICA
    elif errores or (no_ancladas > 0 and modo_estricto):
        veredicto = VeredictoVerificacion.FAIL
    elif no_ancladas > 0:
        # Modo no estricto: se informa pero no se bloquea. NUNCA es el modo por
        # defecto y jamás debe usarse en la demo ni en la evaluación.
        _LOG.warning("verificador en modo no estricto: %s cifras sin anclar", no_ancladas)
        veredicto = VeredictoVerificacion.PASS
    else:
        veredicto = VeredictoVerificacion.PASS

    return ResultadoVerificacion(
        veredicto=veredicto,
        aserciones=aserciones,
        aserciones_totales=len(aserciones),
        ancladas=ancladas,
        derivadas=derivadas,
        no_ancladas=no_ancladas,
        infractores=list(dict.fromkeys(infractores)),
        tokens_infractores=list(dict.fromkeys(tokens_infractores)),
        citas=citas,
        derivaciones=derivaciones,
        errores_estructurales=errores,
        avisos=avisos,
        factset_sha256=conjunto.factset_sha256,
        estricto=modo_estricto,
    )


def _validar_estructura(
    factset: FactSet,
    conjunto: ConjuntoPermitido,
    salida: ExplicacionLLM | None,
    tolerancia_cent: int,
) -> tuple[list[str], list[str]]:
    """Paso 5 de la sección 5.3 sobre la salida estructurada del modelo."""
    errores: list[str] = []
    avisos: list[str] = []
    if salida is None:
        return errores, avisos

    fuera: list[int] = []
    for cifra in salida.cifras_usadas:
        if token_monto(cifra) in conjunto or token_entero(cifra) in conjunto:
            continue
        fuera.append(cifra)
    if fuera:
        errores.append(
            "cifras_usadas contiene valores que no están en el FactSet: "
            + ", ".join(str(valor) for valor in fuera)
        )

    suma = salida.suma_citada_cent()
    descuadre = suma - factset.delta_total_cent
    if abs(descuadre) > tolerancia_cent:
        errores.append(
            f"Σ causas.monto_cent_citado = {suma} pero delta_total_cent = "
            f"{factset.delta_total_cent} (descuadre de {descuadre} céntimos)"
        )
    elif descuadre != 0:
        avisos.append(f"descuadre de {descuadre} céntimo(s) dentro de la tolerancia")

    conocidos = {linea.concepto_id for linea in factset.lineas}
    for causa in salida.causas:
        if causa.concepto_id and causa.concepto_id not in conocidos:
            avisos.append(f"concepto_id citado fuera del FactSet: {causa.concepto_id}")

    return errores, avisos


# --------------------------------------------------------------------------- #
# Demo adversaria
# --------------------------------------------------------------------------- #
def inyectar_alucinacion(texto: str, factset: FactSet, *, delta_cent: int = 731) -> str:
    """Introduce a propósito un importe falso pero plausible en el texto.

    ⚠️ **EXCLUSIVAMENTE PARA LA DEMO ADVERSARIA.** No la llama ninguna ruta de
    producción: existe para poder enseñar en vivo, ante el jurado, que el verificador
    atrapa una cifra inventada y que la respuesta se bloquea. Su único uso legítimo es
    ``make demo`` / el guion de la presentación.

    Estrategia: se toma el primer importe del texto y se sustituye por otro que se
    parece (mismo orden de magnitud, distinto en unos céntimos) comprobando antes que
    **no** pertenece al conjunto permitido; si no hubiera importes, se añade una frase
    con una cifra igualmente no anclada.

    Returns:
        El texto contaminado. Pasarlo por :func:`verificar` debe dar ``FAIL`` con al
        menos una aserción ``NO_ANCLADA``.
    """
    permitidos = construir_permitidos(factset)
    encontrado = next(
        (
            asercion
            for asercion in extraer_aserciones(texto)
            if asercion.token.startswith("cent:") and asercion.inicio is not None
        ),
        None,
    )

    if encontrado is not None:
        original = int(encontrado.token.removeprefix("cent:"))
        falso = _importe_no_anclado(original, permitidos, delta_cent)
        inicio, fin = int(encontrado.inicio or 0), int(encontrado.fin or 0)
        return texto[:inicio] + formatear_soles(falso) + texto[fin:]

    falso = _importe_no_anclado(factset.total_actual_cent, permitidos, delta_cent)
    return (
        f"{texto}\n[DEMO ADVERSARIA] Además, este mes se le aplicó un cargo de "
        f"{formatear_soles(falso)} por un servicio adicional."
    )


def _importe_no_anclado(base: int, permitidos: ConjuntoPermitido, salto: int) -> int:
    """Busca un importe cercano a ``base`` que NO esté en el conjunto permitido."""
    candidato = abs(base) + abs(salto)
    for intento in range(1, 500):
        if token_monto(candidato) not in permitidos:
            return candidato
        candidato = abs(base) + abs(salto) + intento * 13
    return candidato  # pragma: no cover - imposible con un FactSet real
