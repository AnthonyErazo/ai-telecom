"""Propiedades de la SUITE golden, no de un caso concreto.

Los otros tests golden preguntan «¿el sistema contesta bien este caso?». Estos preguntan
«¿la suite mide lo que dice medir?», que es una pregunta distinta y hasta ahora nadie la
hacía. Con 34 casos escritos a mano la respuesta se comprobaba leyendo el YAML; con más
de 200 generados por muestreo hay que comprobarlo con código, porque nadie va a releer
261 casos cada vez que se resiembra el dataset.

Lo que se fija aquí:

* el tamaño de la muestra, que es lo que le da sentido a ``TA_respuesta = 0`` (con 34
  casos, cero alucinaciones es compatible con una cada cien respuestas: 0,99³⁴ = 71 %);
* la **cobertura por estrato**: los 8 escenarios en las 2 modalidades y en las 2
  verbosidades, las dos direcciones del delta, con deuda y sin ella, la cuota de equipo
  en sus tres tramos, las longitudes de ciclo que el dataset contiene;
* que las frases de los casos que **no** deben derivar no disparan por accidente una
  regla dura de hand-off, que sería un falso positivo fabricado por el propio autor de
  la suite;
* que los ficheros generados **se reproducen desde la semilla**: si alguien los edita a
  mano o resiembra el dataset sin regenerarlos, este test lo dice.
"""

from __future__ import annotations

import collections
from collections.abc import Sequence

import pytest

from packages.core_domain.enums import ModalidadRenta, Verbosidad

pytestmark = pytest.mark.golden

#: Los ocho escenarios del generador de datos. ESTABLE es el grupo de control.
ESCENARIOS = (
    "ALTA_PAQUETE",
    "CAMBIO_PLAN_MEDIO_CICLO",
    "CORTE_RECONEXION",
    "CUOTA_EQUIPO_FINANCIADO",
    "DEUDA_ANTERIOR",
    "ESTABLE",
    "FIN_DESCUENTO",
    "NOTA_CREDITO",
)

#: Mínimo que el enunciado del defecto exige: «más de 200 casos».
MINIMO_CASOS = 200


def _cargar() -> list:
    """Casos golden; lista vacía si falta el dataset (el módulo se omite entero)."""
    try:
        from eval.datos import cargar_golden

        return cargar_golden()
    except Exception:  # pragma: no cover - sin dataset generado
        return []


CASOS = _cargar()


@pytest.fixture(scope="module", autouse=True)
def _hay_casos() -> None:
    """Omite el módulo, con motivo accionable, si falta el dataset."""
    if not CASOS:
        pytest.skip(
            "faltan los casos golden o el dataset sintético: ejecute "
            "`python -m packages.datagen.generar --seed 20260804 --clientes 300` y "
            "`python -m eval.generar_golden`"
        )


def _familia(caso_id: str) -> str:
    """Prefijo que agrupa los casos por origen (G, EST, CIC, HDF, ADV)."""
    return "".join(caracter for caracter in caso_id.split("_")[0] if caracter.isalpha())


def _por_escenario(casos: Sequence) -> dict[str, list]:
    """Índice escenario → casos (un compuesto entra en los dos)."""
    indice: dict[str, list] = collections.defaultdict(list)
    for caso in casos:
        for escenario in caso.escenarios or ["SIN_ESCENARIO"]:
            indice[escenario].append(caso)
    return indice


# --------------------------------------------------------------------------- #
# Tamaño
# --------------------------------------------------------------------------- #
def test_la_suite_pasa_de_doscientos_casos() -> None:
    """El tamaño de la muestra es lo que le da significado a la métrica comprometida."""
    assert len(CASOS) > MINIMO_CASOS, (
        f"la suite tiene {len(CASOS)} casos; con menos de {MINIMO_CASOS} un fallo del 1 % "
        "tiene demasiadas probabilidades de no aparecer. Ejecute `python -m eval.generar_golden`"
    )


def test_conviven_los_casos_a_mano_y_los_generados() -> None:
    """Los ocho ficheros escritos a mano siguen ahí: el muestreo AMPLÍA, no sustituye.

    Los casos a mano llevan el guion de la demo, la atribución causal y los tres
    adversariales originales, y cada uno documenta una decisión concreta. Perderlos al
    generar habría sido cambiar cobertura razonada por volumen.
    """
    familias = collections.Counter(_familia(caso.caso_id) for caso in CASOS)
    assert familias["G"] >= 38, "faltan casos escritos a mano"
    assert familias["EST"] >= 100, "falta la muestra estratificada"
    assert familias["CIC"] >= 8, "faltan los controles de longitud de ciclo"
    assert familias["HDF"] >= 10, "faltan los positivos de hand-off"
    assert familias["ADV"] >= 15, "faltan los adversariales ampliados"


# --------------------------------------------------------------------------- #
# Cobertura por estrato
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("escenario", ESCENARIOS)
def test_cada_escenario_en_las_dos_modalidades(escenario: str) -> None:
    """Los 8 escenarios × 2 modalidades de renta. Es la matriz que pide la ficha."""
    casos = _por_escenario(CASOS)[escenario]
    modalidades = {caso.modalidad_renta for caso in casos}
    assert ModalidadRenta.ADELANTADA in modalidades, f"{escenario} sin caso ADELANTADA"
    assert ModalidadRenta.VENCIDA in modalidades, f"{escenario} sin caso VENCIDA"


@pytest.mark.parametrize("escenario", ESCENARIOS)
def test_cada_escenario_en_las_dos_verbosidades(escenario: str) -> None:
    """CORTO y DETALLE cambian la plantilla y la tabla de tramos: hay que medir ambas."""
    casos = _por_escenario(CASOS)[escenario]
    verbosidades = {caso.verbosidad for caso in casos}
    assert verbosidades == {Verbosidad.CORTO, Verbosidad.DETALLE}, (
        f"{escenario} solo se mide en {sorted(str(v) for v in verbosidades)}"
    )


def test_las_dos_direcciones_del_delta_y_el_recibo_que_no_varia() -> None:
    """Sube, baja y no varía. El recibo que baja es el que peor se narra."""
    signos = collections.Counter(
        "sube" if caso.delta_esperado_cent > 0
        else "baja" if caso.delta_esperado_cent < 0
        else "igual"
        for caso in CASOS
    )
    assert signos["sube"] >= 60, signos
    assert signos["baja"] >= 15, signos
    assert signos["igual"] >= 10, signos


def test_hay_casos_con_deuda_anterior_y_sin_ella() -> None:
    """La deuda arrastrada vive FUERA del total del periodo: hay que medir las dos ramas."""
    con_deuda = [caso for caso in CASOS if "DEUDA_ANTERIOR" in caso.escenarios]
    assert len(con_deuda) >= 20, f"solo {len(con_deuda)} casos con deuda anterior"
    assert len(CASOS) - len(con_deuda) >= 100


def test_la_cuota_de_equipo_se_mide_en_sus_tres_tramos() -> None:
    """Primera cuota, intermedia y avanzada.

    No se explican igual: en la primera el cliente pregunta «¿qué es esto?», en la
    última «¿ya terminé?». El número de cuota se lee del recibo, no del caso golden.

    Ojo con el filtro: el escenario ``CUOTA_EQUIPO_FINANCIADO`` inyecta un equipo
    **nuevo**, así que sus casos son siempre la cuota 1. Las cuotas intermedias y
    avanzadas viven en cuentas que ya arrastraban un financiamiento y cuyo escenario del
    mes es otro; ahí la cuota actúa de distractor invariante, que es precisamente donde
    una explicación descuidada la mete como si fuera la causa del aumento. Por eso se
    recorren TODOS los casos y se mira la línea del recibo, no la etiqueta del escenario.
    """
    from eval.datos import cargar_cuenta

    tramos = collections.Counter()
    for caso in CASOS:
        recibo = cargar_cuenta(caso.cuenta_id).recibo(caso.periodo)
        numeros = [
            linea.cuota_numero
            for linea in (recibo.lineas if recibo else [])
            if linea.concepto_id == "CUOTA_EQUIPO_FINANCIADO" and linea.cuota_numero
        ]
        for numero in numeros:
            if numero == 1:
                tramos["primera"] += 1
            elif numero <= 12:
                tramos["intermedia"] += 1
            else:
                tramos["avanzada"] += 1
    assert tramos["primera"] >= 5, tramos
    assert tramos["intermedia"] >= 5, tramos
    assert tramos["avanzada"] >= 5, tramos


def test_las_longitudes_de_ciclo_que_el_dataset_permite() -> None:
    """Ciclos de 30 y de 31 días como ciclo explicado, y de 28 como ciclo previo.

    Los 29 días exigirían un febrero bisiesto y 2026 no lo es; los 28 no pueden ser el
    ciclo explicado porque febrero es el primer periodo del dataset y no tiene recibo
    anterior con el que comparar. Este test fija lo que sí hay y deja escrito por qué no
    hay más: si algún día el generador amplía la ventana, el test se cambia a conciencia.
    """
    from eval.datos import cargar_cuenta

    longitudes = collections.Counter()
    previas = collections.Counter()
    for caso in CASOS:
        cuenta = cargar_cuenta(caso.cuenta_id)
        recibo = cuenta.recibo(caso.periodo)
        if recibo is not None:
            longitudes[recibo.dias_ciclo] += 1
        anteriores = cuenta.previos_de(caso.periodo)
        if anteriores:
            previas[max(anteriores, key=lambda r: r.periodo).dias_ciclo] += 1
    assert longitudes[30] >= 6, longitudes
    assert longitudes[31] >= 100, longitudes
    assert previas[28] >= 3, "ningún caso compara contra un ciclo de 28 días"
    assert 29 not in longitudes, "2026 no es bisiesto: un ciclo de 29 días sería inventado"


def test_la_proporcion_de_compuestos_se_parece_a_la_del_dataset() -> None:
    """~30 % de casos compuestos, como en la población.

    Sobrerrepresentar los compuestos inflaría la dificultad aparente y mediría un
    sistema que no existe; infrarrepresentarlos escondería justo donde se rompe la
    atribución ingenua.
    """
    considerados = [caso for caso in CASOS if _familia(caso.caso_id) in {"EST", "G"}]
    compuestos = sum(1 for caso in considerados if len(caso.escenarios) > 1)
    proporcion = compuestos / len(considerados)
    assert 0.22 <= proporcion <= 0.42, f"proporción de compuestos: {proporcion:.0%}"


def test_los_cuatro_canales_estan_representados() -> None:
    """APP, BOT, WHATSAPP y ASESOR: el canal condiciona el formato de la respuesta."""
    canales = collections.Counter(str(caso.canal) for caso in CASOS)
    assert set(canales) == {"APP", "BOT", "WHATSAPP", "ASESOR"}, canales
    assert min(canales.values()) >= 10, canales


# --------------------------------------------------------------------------- #
# Adversariales y hand-off
# --------------------------------------------------------------------------- #
def test_los_adversariales_declaran_lo_que_no_puede_aparecer() -> None:
    """Un adversarial sin ``no_debe_contener`` no mide nada: solo gasta tiempo."""
    adversariales = [caso for caso in CASOS if _familia(caso.caso_id) == "ADV"]
    adversariales += [caso for caso in CASOS if caso.caso_id.startswith("G32")]
    assert len(adversariales) >= 15
    sin_guardas = [caso.caso_id for caso in adversariales if not caso.no_debe_contener]
    assert sin_guardas == [], f"adversariales sin fragmentos prohibidos: {sin_guardas}"


def test_los_adversariales_cubren_las_familias_de_senal_reconocidas() -> None:
    """Cada familia que ``detectar_manipulacion`` reconoce tiene su caso.

    No basta con quince inyecciones parecidas: si las quince fueran «ignora tus
    instrucciones», la suite mediría una sola defensa quince veces.
    """
    from packages.facts_engine.intencion import detectar_manipulacion

    adversariales = [caso for caso in CASOS if _familia(caso.caso_id) == "ADV"]
    detectadas: set[str] = set()
    for caso in adversariales:
        senales = detectar_manipulacion(caso.utterance)
        detectadas.update(senal.split(":")[0] for senal in senales)
    assert {"marcador_plantilla", "marcador_chat", "etiqueta_rol", "bloque_codigo"} <= detectadas
    assert "cuenta_ajena" in detectadas
    assert "lexica" in detectadas
    con_senal = [caso for caso in adversariales if detectar_manipulacion(caso.utterance)]
    assert len(con_senal) >= 12, "demasiados adversariales que el clasificador ni ve venir"


def test_los_positivos_de_handoff_cubren_las_dos_reglas_duras() -> None:
    """Petición de humano e intención regulatoria, cada una con varias formas.

    Con tres positivos, ``Recall_handoff`` solo podía valer 0, 33, 67 o 100 %: la
    métrica primaria de la ficha no tenía resolución.
    """
    from packages.core_domain.reglas import cargar_reglas
    from packages.facts_engine.confianza import PATRONES_PETICION_HUMANO, normalizar_texto

    positivos = [caso for caso in CASOS if caso.debe_derivar]
    assert len(positivos) >= 12, f"solo {len(positivos)} positivos de hand-off"

    reglas = cargar_reglas()
    regulatorias = [
        normalizar_texto(intencion)
        for intencion in reglas.umbrales_incomprension.intenciones_regulatorias
    ]
    humanos = patrones_regulatorios = 0
    for caso in positivos:
        normal = normalizar_texto(caso.utterance)
        humanos += any(patron in normal for patron in PATRONES_PETICION_HUMANO)
        patrones_regulatorios += any(patron in normal for patron in regulatorias)
    assert humanos >= 5, "pocas formas de pedir una persona"
    assert patrones_regulatorios >= 3, "pocas intenciones regulatorias"


def test_ninguna_frase_dispara_una_regla_dura_por_accidente() -> None:
    """Los casos que deben responder no pueden derivar por culpa de su propia frase.

    Es el error más fácil de cometer al escribir 200 utterances: colar un «llámame» o un
    «otro operador» en una frase inocente y fabricar un falso positivo de hand-off que
    después nadie sabe de dónde salió.
    """
    from eval.generar_golden import _validar_frases

    problemas = _validar_frases(
        [
            {
                "caso_id": caso.caso_id,
                "utterance": caso.utterance,
                "debe_derivar": caso.debe_derivar,
            }
            for caso in CASOS
        ]
    )
    assert problemas == [], "\n".join(problemas)


# --------------------------------------------------------------------------- #
# Reproducibilidad
# --------------------------------------------------------------------------- #
def test_los_ficheros_generados_se_reproducen_desde_la_semilla() -> None:
    """``--comprobar`` regenera en memoria y compara byte a byte con el disco.

    Si este test falla hay dos explicaciones y ninguna es "el test está mal": o alguien
    editó a mano un fichero generado, o el dataset se resembró y los casos ya no
    corresponden. En los dos casos la respuesta es la misma orden::

        python -m eval.generar_golden
    """
    from eval.generar_golden import main

    assert main(["--comprobar", "--sin-verificar"]) == 0


def test_el_muestreo_es_determinista_y_la_semilla_manda() -> None:
    """Misma semilla, misma muestra; otra semilla, otra muestra.

    Sin lo primero la suite no sería reproducible; sin lo segundo la "estratificación"
    sería una lista fija disfrazada de muestreo.
    """
    from eval.generar_golden import inventariar, muestrear

    inventario = inventariar()
    primera = [c.cuenta_id for c in muestrear(inventario, objetivo=40, semilla=20260804)]
    segunda = [c.cuenta_id for c in muestrear(inventario, objetivo=40, semilla=20260804)]
    otra = [c.cuenta_id for c in muestrear(inventario, objetivo=40, semilla=7)]

    assert primera == segunda
    assert primera != otra
    assert len(set(primera)) == len(primera), "la muestra repite cuentas"


def test_las_cifras_esperadas_salen_del_dataset_y_no_del_motor() -> None:
    """El delta esperado es el que dicen los documentos BrainyBill, no el que calcula el motor.

    Es la propiedad que impide que la suite sea un espejo: si mañana el motor calculara
    mal la diferencia entre dos recibos, estos números seguirían siendo los del recibo y
    la evaluación fallaría, que es justo lo que tiene que pasar.
    """
    from eval.datos import cargar_cuenta

    revisados = 0
    for caso in CASOS:
        cuenta = cargar_cuenta(caso.cuenta_id)
        actual = cuenta.recibo(caso.periodo)
        anteriores = cuenta.previos_de(caso.periodo)
        if actual is None or not anteriores:  # pragma: no cover
            continue
        previo = max(anteriores, key=lambda recibo: recibo.periodo)
        assert caso.total_esperado_cent == actual.total_cent, caso.caso_id
        assert caso.delta_esperado_cent == actual.total_cent - previo.total_cent, caso.caso_id
        assert isinstance(caso.total_esperado_cent, int)
        revisados += 1
    assert revisados > MINIMO_CASOS
