"""Política de derivación: derivar es el ÚLTIMO recurso, y sigue siendo honesto.

La *Precisión del Hand-off* es una de las tres métricas oficiales del desafío, y una
métrica de hand-off solo significa algo si derivar es excepcional. Estas pruebas fijan la
distinción que la hace excepcional sin bajar un solo umbral:

* **(a) "no sé cuánto varió ni en qué línea"** → derivar. El recibo no se puede explicar.
  Su vía es la regla dura ``INVARIANTE_ROTO``, no el score.
* **(b) "sé exactamente cuánto y en qué líneas, pero no puedo CONFIRMAR la causa"** → NO
  derivar. Se explica lo que consta y no se inventa el motivo. Tampoco se ofrece un
  asesor: con datos reales la causa casi nunca consta, así que ofrecerlo aquí sería
  ofrecerlo siempre, y un último recurso que aparece en todas las respuestas es el
  primero.
* **(c) el cliente pide una persona** → derivar siempre, sin regatear.

Con el dataset del desafío, que no trae órdenes de CRM, (b) es el caso corriente: las
cuatro cuentas reales concilian al céntimo y ninguna tiene causa documentada. Cuando la
laguna causal entraba en ``s1``, las cuatro derivaban en el primer turno con U=0.675, y
el hand-off dejaba de ser el último recurso para ser el único camino.
"""

from __future__ import annotations

import copy

import pytest

from packages.core_domain.enums import (
    CausaOficial,
    FamiliaConcepto,
    ModalidadRenta,
    MotivoDerivacion,
    TipoMovimiento,
)
from packages.core_domain.esquemas.factset import FactSet, Invariante, LineaDelta
from packages.facts_engine.confianza import (
    Turno,
    evaluar_incomprension,
    pide_humano,
)

UUID_PRUEBA = "11111111-2222-3333-4444-555555555555"

#: Códigos reales del facturador que usan estas pruebas. No están en ``rules.yaml`` —el
#: catálogo del equipo tiene treinta y un conceptos modelados a mano y el dataset trae
#: setecientos treinta y dos códigos del operador— y por eso hay que declararlos, igual
#: que hace el corpus al arrancar la API.
CODIGOS_DEL_DATASET = frozenset({"FRTRPL_014", "FRIRPL_003", "FRTORX_002", "X"})


@pytest.fixture(scope="module")
def reglas():
    """Reglas con los códigos del dataset declarados, como en la API real.

    Sombrea a propósito la fixture ``reglas`` de ``conftest``. Sin esta declaración, la
    regla dura ``CONCEPTO_FUERA_CATALOGO`` derivaría cualquier cuenta real antes de
    llegar al score, y estas pruebas medirían un camino que la API no recorre: al
    arrancar, ``packages.retriever.corpus`` declara al motor los códigos del dataset
    precisamente para que eso no ocurra.

    Se trabaja sobre una copia profunda porque ``cargar_reglas`` devuelve un objeto
    cacheado y compartido con el resto de la sesión de pruebas, y mutarlo aquí
    contaminaría a los demás módulos.
    """
    from packages.core_domain.reglas import cargar_reglas

    configuracion = copy.deepcopy(cargar_reglas())
    configuracion.registrar_conceptos_del_dataset(set(CODIGOS_DEL_DATASET))
    return configuracion


# --------------------------------------------------------------------------- #
# Constructores: recibos que imitan el dataset real
# --------------------------------------------------------------------------- #
def linea(
    *,
    concepto_id: str = "FRTRPL_014",
    nombre: str = "Movistar TV Estándar",
    actual: int = 21_239,
    previo: int = 10_799,
    causa: TipoMovimiento | None = None,
    causa_oficial: CausaOficial | None = None,
    confianza: float = 0.30,
    evidencia: list[str] | None = None,
) -> LineaDelta:
    """Una línea del dataset real: nombre comercial y montos, sin orden del CRM.

    Los valores por defecto son los de la cuenta 732330542 en 2026-07 tal como los
    devuelve Supabase: confianza ``sin_candidato`` = 0.30 y ``causa = None``, porque el
    concepto ``FRTRPL_014`` no tiene fila en ``regla_concepto_causa``.
    """
    return LineaDelta(
        concepto_id=concepto_id,
        nombre_comercial=nombre,
        clase=LineaDelta.clasificar(actual, previo),
        monto_actual_cent=actual,
        monto_previo_cent=previo,
        delta_cent=actual - previo,
        causa=causa,
        causa_oficial=causa_oficial,
        confianza=confianza,
        familia=FamiliaConcepto.RECURRENTE,
        evidencia=evidencia if evidencia is not None else [f"cat:{concepto_id}", "linea:2"],
    )


def recibo(*lineas: LineaDelta, invariante_ok: bool = True) -> FactSet:
    """FactSet conciliado a partir de las líneas dadas.

    Con ``invariante_ok=False`` se fabrica el caso (a): quedan céntimos que ninguna línea
    explica, así que no se sabe *cuánto* varió cada cosa y no hay explicación posible.
    """
    deltas = [item.delta_cent for item in lineas]
    delta_total = sum(deltas)
    residual = 0 if invariante_ok else 500
    return FactSet(
        factset_id=UUID_PRUEBA,
        cuenta_id="C-PRUEBA",
        modalidad_renta=ModalidadRenta.VENCIDA,
        periodo_actual="2026-07",
        periodo_previo="2026-06",
        dias_ciclo=30,
        total_actual_cent=50_000 + delta_total + residual,
        total_previo_cent=50_000,
        delta_total_cent=delta_total + residual,
        lineas=list(lineas),
        invariante=Invariante(
            ok=invariante_ok,
            residual_cent=residual,
            suma_deltas_cent=delta_total,
            delta_total_cent=delta_total + residual,
        ),
        confianza_global=0.30,
        rules_version="prueba",
    )


# --------------------------------------------------------------------------- #
# (b) El caso corriente del dataset real: se explica, no se deriva
# --------------------------------------------------------------------------- #
class TestDesglosarNoEsConfirmar:
    """``s1`` mide el desglose; la laguna causal se mide aparte y NO deriva."""

    def test_recibo_sin_ordenes_de_crm_no_deriva(self, reglas) -> None:
        """El caso (b), que es el de las cuatro cuentas reales del desafío.

        El recibo concilia al céntimo, cada línea tiene su nombre comercial y sus dos
        importes, y ninguna tiene orden del CRM detrás. Eso es un recibo perfectamente
        explicable al que solo le falta el porqué: derivarlo sería renunciar a explicar
        lo que sí se sabe.
        """
        resultado = evaluar_incomprension(
            recibo(linea()), [], "¿por qué me vino más caro este mes?", reglas=reglas
        )
        assert resultado.s1_cobertura == 1.0, "el delta está íntegramente desglosado"
        assert resultado.cobertura_causal == 0.0, "y ninguna causa está confirmada"
        assert resultado.derivar is False
        assert resultado.tau_alto >= resultado.U, "no basta con no derivar: U no roza el umbral"

    def test_la_laguna_causal_no_aporta_nada_al_score(self, reglas) -> None:
        """El mismo recibo con y sin causa confirmada da EL MISMO ``U``.

        Es la prueba de que la laguna del CRM salió del score. Antes la diferencia era
        exactamente ``w1 = 0.40``, un suelo permanente que ninguna conversación sana
        podía compensar.
        """
        sin_causa = evaluar_incomprension(recibo(linea()), [], "no me cuadra", reglas=reglas)
        con_causa = evaluar_incomprension(
            recibo(
                linea(
                    causa=TipoMovimiento.CAMBIO_PLAN,
                    causa_oficial=CausaOficial.CAMBIO_DE_PLAN,
                    confianza=0.98,
                )
            ),
            [],
            "no me cuadra",
            reglas=reglas,
        )
        assert sin_causa.U == con_causa.U
        assert sin_causa.cobertura_causal == 0.0
        assert con_causa.cobertura_causal == 1.0

    def test_no_se_ofrece_asesor_por_falta_de_causa(self, reglas) -> None:
        """Que no conste la causa NO es motivo para ponerle un asesor delante.

        Antes sí lo era, y ese era el problema. Con datos reales la causa no consta en la
        mayoría de los recibos —el CRM no registra una orden por cada línea que se
        mueve—, así que la puerta al asesor se abría casi siempre. Una puerta que se abre
        en todas las respuestas no es el último recurso: es el primero, y le dice al
        cliente que la explicación que acaba de leer no bastaba.

        Lo que sí se conserva es el dato honesto: ``causa_confirmada`` sigue siendo
        ``False`` y sigue viajando al expediente. Lo que se calla es el ofrecimiento.
        """
        sin_causa = evaluar_incomprension(recibo(linea()), [], "no me cuadra", reglas=reglas)
        assert sin_causa.causa_confirmada is False
        assert sin_causa.ofrecer_asesor is False
        assert sin_causa.derivar is False
        assert sin_causa.asesor_a_la_vista is False

    def test_el_asesor_se_ve_solo_si_lo_pide_el_cliente(self, reglas) -> None:
        """El botón lo abre el cliente, no el diagnóstico interno.

        Un concepto fuera de catálogo deriva —regla dura— pero es un diagnóstico nuestro:
        al cliente «FRTOCH_003 no está en el catálogo» solo le comunica que el sistema no
        funciona. El caso llega igual a la cola del asesor, en silencio. En cambio, si la
        persona la pide él, el botón aparece.
        """
        pedido = evaluar_incomprension(
            recibo(linea()), [], "quiero hablar con un asesor", reglas=reglas
        )
        assert pedido.motivo is MotivoDerivacion.PETICION_HUMANO
        assert pedido.asesor_a_la_vista is True

        interno = evaluar_incomprension(
            recibo(linea(concepto_id="CONCEPTO_QUE_NO_EXISTE")), [], "no me cuadra", reglas=reglas
        )
        assert interno.derivar is True, "la derivación interna se mantiene"
        assert interno.asesor_a_la_vista is False, "pero al cliente no se le anuncia"

    def test_una_linea_sin_nombre_si_baja_el_desglose(self, reglas) -> None:
        """Lo que ``s1`` sí penaliza: no poder nombrar ni citar la línea.

        Una línea sin nombre comercial y sin evidencia es dinero que se movió y que el
        asistente no sabe presentar. Eso sí es no poder explicar, y sí tiene que pesar.
        """
        resultado = evaluar_incomprension(
            recibo(
                linea(actual=11_000, previo=10_000),
                linea(concepto_id="X", nombre="", actual=11_000, previo=10_000, evidencia=[]),
            ),
            [],
            "no me cuadra",
            reglas=reglas,
        )
        assert resultado.s1_cobertura == 0.5


# --------------------------------------------------------------------------- #
# (a) Lo que sí debe derivar
# --------------------------------------------------------------------------- #
class TestLoQueSiDeriva:
    """Las tres vías que siguen derivando, y que ninguna de las mejoras debilita."""

    def test_invariante_roto_deriva(self, reglas) -> None:
        """El caso (a): el recibo no cuadra, así que no se sabe cuánto varió cada cosa."""
        resultado = evaluar_incomprension(
            recibo(linea(), invariante_ok=False), [], "no me cuadra", reglas=reglas
        )
        assert resultado.derivar is True
        assert resultado.motivo is MotivoDerivacion.INVARIANTE_ROTO

    @pytest.mark.parametrize(
        "frase",
        [
            "quiero hablar con una persona",
            "pásame con un asesor",
            "necesito hablar con un humano",
            "me puede atender un agente por favor",
            "quiero que me atienda una persona real",
            "comunicarme con un representante",
            "no entendí nada, ¿me pasa con un asesor?",
            "no quiero un bot, quiero una persona real",
            "necesito que un ejecutivo revise mi recibo",
            "quiero hablar con alguien que me explique el monto",
            "esto no me sirve, quiero atencion al cliente de verdad",
            "no quiero pagar de más, quiero hablar con un asesor",
        ],
    )
    def test_pedir_una_persona_siempre_deriva(self, reglas, frase: str) -> None:
        """El punto (c): si el cliente pide una persona, se deriva sin regatear.

        Incluye frases que empiezan con una negación dirigida a otra cosa
        («no entendí nada, ¿me pasa con un asesor?»): la negación no puede servir de
        excusa para no derivar.
        """
        resultado = evaluar_incomprension(recibo(linea()), [], frase, reglas=reglas)
        assert resultado.derivar is True
        assert resultado.motivo is MotivoDerivacion.PETICION_HUMANO

    @pytest.mark.parametrize(
        "frase",
        [
            "quiero dar de baja el servicio",
            "voy a presentar un reclamo formal por este monto",
            "quiero el libro de reclamaciones",
            "me están cobrando de más, voy a ir a Osiptel",
            "esto es abusivo, lo llevo a Indecopi",
        ],
    )
    def test_la_intencion_regulatoria_sigue_derivando(self, reglas, frase: str) -> None:
        """Baja, reclamo formal y organismos: trámite regulado, va a una persona."""
        resultado = evaluar_incomprension(recibo(linea()), [], frase, reglas=reglas)
        assert resultado.derivar is True
        assert MotivoDerivacion.INTENCION_REGULATORIA.value in resultado.reglas_disparadas


class TestMencionarNoEsPedir:
    """Nombrar a una persona no es pedirla, y rechazarla mucho menos."""

    @pytest.mark.parametrize(
        "frase",
        [
            "el asesor de la tienda me dijo que mi plan costaba 55 soles",
            "me atendió un agente y no me explicó nada",
            "llamé al call center y no me resolvieron, por eso escribo aquí",
            "me cambiaron el plan en atención al cliente el mes pasado",
            "esto lo revisa una persona o es un robot",
            "no hace falta que me pases con una persona, solo dime el monto",
            "¿por qué me vino más caro este mes?",
        ],
    )
    def test_mencionar_o_rechazar_no_deriva(self, reglas, frase: str) -> None:
        """Cuatro cuentan un pasado, una pregunta y otra RECHAZA el traspaso.

        Con la regla de subcadenas, seis de estas siete derivaban: bastaba con que la
        palabra «asesor» o «agente» apareciera en cualquier sitio. Derivar aquí no
        protege a nadie —le quita al cliente la respuesta que venía a buscar— y hunde la
        precisión del hand-off con falsos positivos.
        """
        assert pide_humano(frase) is None
        resultado = evaluar_incomprension(recibo(linea()), [], frase, reglas=reglas)
        assert MotivoDerivacion.PETICION_HUMANO.value not in resultado.reglas_disparadas

    def test_otro_operador_es_la_competencia_no_una_persona(self, reglas) -> None:
        """«otro operador» es portabilidad, y deriva por su motivo real, no por (c).

        En Perú «operador» nombra también a la propia telco. La frase debe derivar
        —es un trámite regulado— pero con ``INTENCION_REGULATORIA``, porque un motivo
        equivocado manda al asesor un expediente que dice lo que no es.
        """
        resultado = evaluar_incomprension(
            recibo(linea()), [], "quiero mi portabilidad a otro operador", reglas=reglas
        )
        assert resultado.derivar is True
        assert resultado.motivo is MotivoDerivacion.INTENCION_REGULATORIA
        assert MotivoDerivacion.PETICION_HUMANO.value not in resultado.reglas_disparadas


# --------------------------------------------------------------------------- #
# Contabilidad del historial: s3 y s6
# --------------------------------------------------------------------------- #
class TestContabilidadDelHistorial:
    """``s3`` y ``s6`` miden la conversación, no el hecho de que exista."""

    def test_la_primera_pregunta_no_es_una_repregunta(self, reglas) -> None:
        """Un cliente nuevo no puede estar repreguntando: no ha preguntado antes."""
        resultado = evaluar_incomprension(
            recibo(linea()), [], "¿por qué me vino más caro este mes?", reglas=reglas
        )
        assert resultado.s3_repregunta == 0.0

    def test_repetir_la_misma_pregunta_si_cuenta(self, reglas) -> None:
        """Y cuando de verdad repregunta, ``s3`` lo detecta: la señal sigue viva."""
        frase = "¿por qué me vino más caro este mes?"
        resultado = evaluar_incomprension(
            recibo(linea()),
            [Turno(utterance=frase, rol="cliente")],
            frase,
            reglas=reglas,
        )
        assert resultado.s3_repregunta == 1.0

    def test_el_progreso_del_asistente_reinicia_el_contador(self, reglas) -> None:
        """``s6`` lee el ``progreso`` que escribe el asistente, que es quien lo sabe.

        El bucle descartaba los turnos que no eran del cliente *antes* de mirar la
        bandera, y los turnos de cliente nacen con ``progreso=False``: la bandera no se
        leía jamás y ``s6`` era un contador de mensajes disfrazado de señal de calidad.
        """
        historial = [
            Turno(utterance="¿por qué subió?", rol="cliente"),
            Turno(utterance="subió por esto", rol="asistente", progreso=True),
            Turno(utterance="ah, y ¿el mes pasado?", rol="cliente"),
            Turno(utterance="el mes pasado fue así", rol="asistente", progreso=True),
        ]
        resultado = evaluar_incomprension(
            recibo(linea()), historial, "gracias, otra duda", reglas=reglas
        )
        assert resultado.turnos_sin_progreso == 0
        assert resultado.s6_sin_progreso == 0.0

    def test_sin_progreso_el_contador_sube(self, reglas) -> None:
        """Si el asistente no resuelve nada, ``s6`` sube: la señal no se ha anulado."""
        historial = [
            Turno(utterance="¿por qué subió?", rol="cliente"),
            Turno(utterance="no le he entendido", rol="asistente", progreso=False),
            Turno(utterance="que por qué subió", rol="cliente"),
            Turno(utterance="no le he entendido", rol="asistente", progreso=False),
        ]
        resultado = evaluar_incomprension(recibo(linea()), historial, "otra vez", reglas=reglas)
        assert resultado.turnos_sin_progreso == 2
        assert resultado.s6_sin_progreso == 1.0


class TestHisteresis:
    """La histéresis protege una conversación humana, no un número que subió una vez."""

    def test_sin_asesor_en_sala_no_se_fija_la_derivacion(self, reglas) -> None:
        """Un pico transitorio no puede condenar el resto del diálogo.

        Mientras nadie recoja el expediente no hay conversación humana que proteger. Si
        la causa de la derivación sigue ahí, la regla dura o el score volverán a
        dispararla solos; si no sigue, no había nada que fijar.
        """
        resultado = evaluar_incomprension(
            recibo(linea()),
            [],
            "¿y el mes que viene cuánto pago?",
            reglas=reglas,
            derivado_previamente=True,
            asesor_en_sala=False,
        )
        assert resultado.derivar is False
        assert resultado.histeresis_aplicada is False

    def test_con_asesor_en_sala_la_derivacion_se_mantiene(self, reglas) -> None:
        """Con una persona atendiendo, la máquina no le quita la conversación."""
        resultado = evaluar_incomprension(
            recibo(linea()),
            [],
            "¿y el mes que viene cuánto pago?",
            reglas=reglas,
            derivado_previamente=True,
            asesor_en_sala=True,
        )
        assert resultado.derivar is True
        assert resultado.histeresis_aplicada is True


class TestConversacionCompletaDelDatasetReal:
    """Tres turnos seguidos sobre un recibo del dataset real no derivan nunca."""

    def test_tres_turnos_sin_derivar(self, reglas) -> None:
        """Reproduce el perfil exacto de las cuatro cuentas reales, turno a turno.

        Antes: turno 1 con U=0.675 (deriva), turno 2 con U=0.75 y la histéresis fijando
        la derivación para siempre. El cliente no había hecho nada raro: solo preguntar.
        """
        factset = recibo(linea())
        historial: list[Turno] = []
        for frase in (
            "¿por qué me vino más caro este mes?",
            "y esa subida, ¿de qué línea es?",
            "vale, ¿y cuánto me toca pagar entonces?",
        ):
            resultado = evaluar_incomprension(factset, historial, frase, reglas=reglas)
            assert resultado.derivar is False, f"derivó en «{frase}» con U={resultado.U}"
            historial.append(Turno(utterance=frase, rol="cliente"))
            historial.append(
                Turno(utterance="explicación", rol="asistente", progreso=not resultado.derivar)
            )
