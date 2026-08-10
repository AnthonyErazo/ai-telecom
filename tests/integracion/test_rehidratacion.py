"""La explicación sobrevive al reinicio del proceso: prueba de ``rehidratacion.py``.

Qué agujero tapa este fichero
-----------------------------
``packages/orquestacion/rehidratacion.py`` es lo que hace que
``GET /v1/evidencia/{explicacion_id}`` siga respondiendo ``200`` después de que el
proceso muera y vuelva. Estaba verificado **a mano** —matando el proceso y volviendo a
preguntar— y sin una sola prueba automática. El riesgo no era que estuviera roto: era
que, el día en que alguien cambiase la forma del estado del *checkpoint*, la suite
siguiera verde y la persistencia dejara de funcionar en silencio hasta la demostración.

Cómo se prueba "matar el proceso" sin matar el proceso
------------------------------------------------------
No se simula nada del *checkpointer*: se escribe un turno completo con un ``SqliteSaver``
sobre un fichero de ``tmp_path``, se **cierra esa instancia entera** (conexión incluida)
y se abre una **instancia nueva** sobre el mismo fichero. Todo lo que cruce esa frontera
lo ha hecho pasando por el disco y por el serializador de dominio, que es exactamente el
camino que recorre un reinicio de verdad. La memoria viva con la que se consulta después
es una ``MemoriaConversaciones`` recién construida: vacía, como al arrancar.

Las cinco propiedades que se fijan aquí
---------------------------------------
1. **El caso central** — el turno vuelve entero: mismos 24 items de evidencia y mismo
   ``factset_sha256``, con los importes todavía en ``int``.
2. **El control de propiedad** — un token de otra cuenta recibe ``403`` sobre una
   explicación rehidratada. La rehidratación no es una puerta trasera al aislamiento.
3. **El límite declarado** — el barrido está acotado a :data:`LIMITE_BUSQUEDA`
   *checkpoints*; más allá se responde ``404``, y eso es **comportamiento esperado**.
4. **La degradación** — si el fichero no se puede abrir, se avisa, se sigue en memoria y
   la consulta responde ``404``. Nunca un ``500``.
5. **La no-regresión del respaldo** — con ``ORQUESTADOR=directo`` el ``404`` de evidencia
   **no importa LangGraph**. Se comprueba en un intérprete limpio, que es la única forma
   de comprobar algo sobre ``sys.modules``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.api.deps import (
    EstadoAdversario,
    MemoriaConversaciones,
    RegistroExplicacion,
)
from apps.api.errores import ErrorApi
from apps.api.routers.evidencia import evidencia_de_factset, obtener_evidencia
from apps.api.security import Identidad
from apps.api.settings import obtener_ajustes
from packages.core_domain.enums import Canal, NivelAseguramiento, Verbosidad
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import ItemEvidencia, RespuestaCanalAgnostica
from packages.governance.auditoria import RegistroAuditoria
from packages.governance.telemetria import RegistroTelemetria
from packages.orquestacion import (
    VALOR_EN_MEMORIA,
    Checkpointer,
    Servicios,
    abrir_checkpointer,
    compilar_grafo,
    ejecutar_turno,
    estado_inicial,
    rehidratacion,
)
from packages.orquestacion.rehidratacion import (
    LIMITE_BUSQUEDA,
    explicacion_persistida,
    rehidratar_explicacion,
)

pytestmark = pytest.mark.integracion

# --------------------------------------------------------------------------- #
# Guion del turno que se persiste
# --------------------------------------------------------------------------- #
#: Cliente de guion del dataset determinístico (``seed 20260804``).
CUENTA = "C-DEMO-01"

#: Otra cuenta del mismo dataset: la que **no** debe poder leer la evidencia de la
#: primera, ni viva ni rehidratada.
CUENTA_AJENA = "C-DEMO-02"

#: Periodo de los tres recibos de demostración.
PERIODO = "2026-07"

#: La pregunta que abre el camino feliz completo (``EXPLICAR_RECIBO``).
PREGUNTA = "por que subio mi recibo este mes"

#: Items de evidencia que devuelve ``GET /v1/evidencia`` para el turno de guion, en dos
#: mitades de naturaleza distinta:
#:
#: * los del **FactSet** (``evidencia_de_factset``: ``factset``, ``linea``, ``mov``,
#:   ``tramo`` y las fichas del catálogo de reglas) se **recalculan** en cada consulta a
#:   partir del ``FactSet``, así que sobreviven al reinicio si sobrevive el ``FactSet``;
#: * los del **contexto recuperado** (``cat``, ``faq``, ``casuistica`` del retriever) no
#:   se recalculan: estaban en RAM y son los que **tienen que haber viajado por el
#:   disco**. Son la mitad que de verdad prueba la rehidratación.
#:
#: El número es el del guion (``C-DEMO-01`` / ``2026-07`` / "por qué subió"). La
#: comprobación que manda no es esta constante sino la igualdad con el turno vivo, que
#: se calcula en cada ejecución; esta se conserva porque es la cifra que se enseña.
#:
#: Subió de 24 a 25 al corregir la atribución causal: el cambio de plan de C-DEMO-01
#: cancela la promoción atada al plan anterior y el generador emite ahora **dos**
#: órdenes (``CAMBIO_PLAN`` y ``FIN_DESCUENTO``) en vez de una, así que el FactSet cita
#: un ``mov:`` más.
ITEMS_DE_EVIDENCIA = 25


@dataclass(slots=True)
class TurnoPersistido:
    """Un turno ya escrito en disco y con su instancia de *checkpointer* cerrada.

    Lleva consigo lo que el turno produjo **en vivo**, antes de tocar el disco: es
    contra eso —y no contra números escritos a mano— contra lo que se compara lo que
    vuelve, de modo que la prueba solo se pone en rojo si la rehidratación cambia.
    """

    ruta: Path
    trace_id: str
    conversation_id: str
    cuenta: str
    factset: FactSet
    #: Items que aportó el contexto recuperado, tal cual estaban en memoria.
    evidencia_viva: list[ItemEvidencia]
    #: Items que ``evidencia_de_factset`` deriva del FactSet del turno.
    items_del_factset: int


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _identidad(cuenta: str) -> Identidad:
    """Identidad LOA2 (App Mi Movistar): el nivel que puede ver la evidencia."""
    return Identidad(
        sub=cuenta,
        acr=NivelAseguramiento.LOA2,
        amr=["pwd", "app"],
        exp=datetime.now(UTC) + timedelta(hours=1),
        canal=Canal.APP,
    )


def _servicios(directorio: Path, etiqueta: str) -> Servicios:
    """Dependencias vivas con bitácora, telemetría y memoria **propias**.

    Los singletons inmutables —reglas, recuperador, proveedor, repositorio— se comparten
    porque construirlos otra vez solo haría lenta la prueba. Lo que no se comparte es
    nada que acumule estado: si la memoria fuera la del proceso, el turno quedaría en RAM
    y la rehidratación no se estaría probando, se estaría esquivando.
    """
    from apps.api.deps import (
        obtener_proveedor_llm,
        obtener_recuperador,
        obtener_reglas,
        obtener_repositorio,
    )

    return Servicios(
        ajustes=obtener_ajustes().model_copy(update={"log_terminal": False}),
        repositorio=obtener_repositorio(),
        reglas=obtener_reglas(),
        recuperador=obtener_recuperador(),
        proveedor=obtener_proveedor_llm(),
        auditoria=RegistroAuditoria(directorio / f"auditoria-{etiqueta}.jsonl", actor="api"),
        memoria=MemoriaConversaciones(),
        telemetria=RegistroTelemetria(directorio / f"telemetria-{etiqueta}.jsonl"),
        adversario=EstadoAdversario(),
    )


def _escribir_turno(directorio: Path, etiqueta: str, *, cuenta: str = CUENTA) -> TurnoPersistido:
    """Ejecuta un turno completo contra un fichero SQLite y **cierra la instancia**.

    Cerrar es la mitad del experimento: es lo que convierte "el estado está en un objeto
    que tengo aquí" en "el estado está en el disco y este proceso ya no lo tiene".
    """
    ruta = directorio / f"turnos-{etiqueta}.sqlite"
    trace_id = f"tr-{uuid.uuid4().hex[:12]}"
    conversation_id = str(uuid.uuid4())
    identidad = _identidad(cuenta)
    servicios = _servicios(directorio, etiqueta)

    checkpointer = abrir_checkpointer(ruta)
    try:
        assert checkpointer.persistente is True, (
            f"el turno tiene que escribirse en disco de verdad, y se abrió {checkpointer.motivo}"
        )
        final = ejecutar_turno(
            estado_inicial(
                trace_id=trace_id,
                conversation_id=conversation_id,
                cuenta=cuenta,
                canal=Canal.APP,
                nivel=identidad.acr,
                contexto_auditoria=identidad.contexto_auditoria(),
                utterance=PREGUNTA,
                verbosidad=Verbosidad.CORTO,
                periodo=PERIODO,
            ),
            conversation_id,
            servicios,
            grafo=compilar_grafo(checkpointer.saver),
        )
    finally:
        # El equivalente en prueba del `taskkill /F`: se suelta la conexión entera.
        checkpointer.cerrar()

    factset = final["factset"]
    contexto = final["contexto_recuperado"]
    assert factset is not None and contexto is not None, (
        f"el turno de guion tiene que recorrer el camino feliz completo; recorrió {final['nodos']}"
    )
    return TurnoPersistido(
        ruta=ruta,
        trace_id=trace_id,
        conversation_id=conversation_id,
        cuenta=cuenta,
        factset=factset,
        evidencia_viva=list(contexto.items_evidencia()),
        items_del_factset=len(evidencia_de_factset(factset, servicios.reglas)),
    )


@contextmanager
def _proceso_nuevo(ruta: Path) -> Iterator[Checkpointer]:
    """Abre una instancia **nueva** de *checkpointer* sobre el mismo fichero.

    Conexión SQLite nueva, ``SqliteSaver`` nuevo, serializador nuevo: todo lo que se lea
    aquí ha cruzado el disco. Es lo que ve el proceso que arranca después del reinicio.
    """
    checkpointer = abrir_checkpointer(ruta)
    try:
        assert checkpointer.persistente is True, checkpointer.motivo
        yield checkpointer
    finally:
        checkpointer.cerrar()


def _ruido(checkpointer: Checkpointer, cuantos: int) -> None:
    """Escribe *checkpoints* posteriores para empujar al turno fuera del barrido."""
    grafo = compilar_grafo(checkpointer.saver)
    for indice in range(cuantos):
        grafo.update_state(
            {"configurable": {"thread_id": f"ruido-{indice}"}}, {"eventos": ["RUIDO"]}
        )


def _consultar_evidencia(
    explicacion_id: str,
    identidad: Identidad,
    memoria: MemoriaConversaciones,
):
    """Llama al endpoint real de evidencia con las dependencias que se le pasen."""
    from apps.api.deps import obtener_reglas

    return obtener_evidencia(explicacion_id, identidad, memoria, obtener_reglas())


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def turno_persistido(tmp_path_factory: pytest.TempPathFactory, exige_dataset: None):
    """Un turno del guion ya escrito en disco, con su *checkpointer* cerrado.

    De ámbito de módulo porque escribirlo cuesta una décima de segundo y **ninguna**
    prueba de este fichero lo modifica: todas abren instancias nuevas de solo lectura.
    """
    return _escribir_turno(tmp_path_factory.mktemp("rehidratacion"), "central")


# --------------------------------------------------------------------------- #
# 1. El caso central: el turno vuelve entero desde el disco
# --------------------------------------------------------------------------- #
def test_la_explicacion_sobrevive_al_cierre_del_checkpointer(
    turno_persistido: TurnoPersistido,
) -> None:
    """Instancia cerrada, instancia nueva, y el registro vuelve completo.

    Es la prueba de que ``explicacion_persistida`` reconstruye un
    ``RegistroExplicacion`` equivalente al que había en RAM antes del reinicio: mismo
    ``sha256`` del FactSet, mismos items de evidencia del contexto, misma cuenta y mismo
    hilo. Si alguien cambia la forma del estado del *checkpoint* —renombra ``factset``,
    deja de guardar ``contexto_recuperado``, mueve ``trace_id``—, esto se pone en rojo.
    """
    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        registro = explicacion_persistida(turno_persistido.trace_id, saver=checkpointer.saver)

    assert registro is not None, (
        "el turno estaba en disco y no se pudo rehidratar: GET /v1/evidencia respondería "
        "404 después de un reinicio, que es exactamente el fallo que este módulo tapa"
    )
    assert isinstance(registro, RegistroExplicacion)
    assert registro.explicacion_id == turno_persistido.trace_id
    assert registro.trace_id == turno_persistido.trace_id
    assert registro.conversation_id == turno_persistido.conversation_id
    assert registro.cuenta_ref == turno_persistido.cuenta
    assert registro.periodo == PERIODO
    assert registro.canal == str(Canal.APP)
    assert registro.utterance == PREGUNTA
    assert registro.derivada is False
    assert registro.score_incomprension is not None


def test_el_factset_rehidratado_conserva_su_sello_y_sus_enteros(
    turno_persistido: TurnoPersistido,
) -> None:
    """Vuelve como ``FactSet``, no como diccionario, y con el ``sha256`` intacto.

    Es la garantía que sostiene todo lo demás: si el FactSet volviera degradado a
    ``dict``, o si un importe volviera como ``float``, la evidencia que se entregara tras
    un reinicio ya no sería la misma que se entregó antes de él.
    """
    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        registro = explicacion_persistida(turno_persistido.trace_id, saver=checkpointer.saver)
    assert registro is not None

    assert isinstance(registro.factset, FactSet)
    assert registro.factset.sha256 == turno_persistido.factset.sha256
    assert registro.factset.verificar_sha256() is True
    assert isinstance(registro.respuesta, RespuestaCanalAgnostica)
    # Todo importe sigue siendo un entero en céntimos: el serializador de dominio no
    # convirtió nada a coma flotante al ir y volver del disco.
    assert isinstance(registro.factset.total_actual_cent, int)
    assert isinstance(registro.factset.total_previo_cent, int)
    assert isinstance(registro.factset.delta_total_cent, int)
    for linea in registro.factset.lineas:
        assert isinstance(linea.delta_cent, int), linea.concepto_id


def test_la_evidencia_del_contexto_vuelve_intacta_item_a_item(
    turno_persistido: TurnoPersistido,
) -> None:
    """Los items del retriever son los que **de verdad** viajan por el disco.

    Se comparan uno a uno contra los que el turno produjo en vivo, no contra un número
    escrito a mano: así la prueba se pone en rojo cuando cambia la rehidratación y no
    cuando cambia el corpus, que es la diferencia entre una prueba y una alarma.
    """
    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        registro = explicacion_persistida(turno_persistido.trace_id, saver=checkpointer.saver)
    assert registro is not None

    def _comparable(items: list[ItemEvidencia]) -> list[tuple[str, str, str]]:
        return [(item.tipo, item.ref_id, item.snippet) for item in items]

    assert _comparable(registro.evidencia) == _comparable(turno_persistido.evidencia_viva), (
        "la evidencia del contexto no volvió igual del disco: lo que se entregue tras un "
        "reinicio ya no sería lo mismo que se entregó antes de él"
    )
    assert registro.evidencia, "el turno de guion sí recupera contexto: una lista vacía es un fallo"
    assert registro.contexto_rag, "el resumen de auditoría del RAG también se rehidrata"


def test_el_endpoint_entrega_los_24_items_tras_el_reinicio(
    turno_persistido: TurnoPersistido, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /v1/evidencia`` con la memoria vacía responde 200 y los 24 items.

    La memoria viva es nueva —como al arrancar el proceso— y el *checkpointer* del
    proceso apunta a una instancia recién abierta sobre el fichero del turno. No se
    sustituye ningún ``saver`` por un doble: lo que se redirige es **de qué fichero**
    lee el proceso, que es justo lo que cambia al reiniciar con otro ``CHECKPOINT_PATH``.
    """
    memoria = MemoriaConversaciones()
    identidad = _identidad(turno_persistido.cuenta)

    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        monkeypatch.setattr(rehidratacion, "obtener_checkpointer", lambda: checkpointer)
        assert memoria.explicacion(turno_persistido.trace_id) is None
        respuesta = _consultar_evidencia(turno_persistido.trace_id, identidad, memoria)

    esperados = turno_persistido.items_del_factset + len(turno_persistido.evidencia_viva)
    assert respuesta.total == len(respuesta.items) == esperados, (
        "tras el reinicio se entregó una evidencia distinta de la del turno vivo: "
        f"{respuesta.total} items en vez de {esperados} "
        f"({Counter(i.tipo for i in respuesta.items)})"
    )
    assert respuesta.total == ITEMS_DE_EVIDENCIA, (
        f"el turno de guion entrega {ITEMS_DE_EVIDENCIA} items y ahora entrega "
        f"{respuesta.total} ({turno_persistido.items_del_factset} del FactSet + "
        f"{len(turno_persistido.evidencia_viva)} del contexto). Si la comparación con el "
        "turno vivo de la línea anterior pasó, esto NO es la rehidratación: cambió el "
        "FactSet del guion o el corpus del retriever, y lo que toca es actualizar "
        "ITEMS_DE_EVIDENCIA"
    )
    assert respuesta.factset_sha256 == turno_persistido.factset.sha256
    assert respuesta.explicacion_id == turno_persistido.trace_id
    assert respuesta.trace_id == turno_persistido.trace_id
    assert respuesta.periodo == PERIODO
    # Las dos familias están representadas: hechos del FactSet y corpus saneado.
    tipos = {item.tipo for item in respuesta.items}
    assert {"factset", "linea", "cat"} <= tipos


def test_el_barrido_se_paga_una_sola_vez_por_explicacion(
    turno_persistido: TurnoPersistido, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rehidratar_explicacion`` repone el registro en la memoria viva.

    Sin esto, cada consulta posterior —y el ``POST /v1/derivacion`` del mismo turno—
    volvería a recorrer los *checkpoints*. Con esto, el barrido es un peaje de una vez.
    """
    memoria = MemoriaConversaciones()
    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        monkeypatch.setattr(rehidratacion, "obtener_checkpointer", lambda: checkpointer)
        devuelto = rehidratar_explicacion(memoria, turno_persistido.trace_id)

    assert devuelto is not None
    guardado = memoria.explicacion(turno_persistido.trace_id)
    assert guardado is devuelto, "el registro rehidratado tiene que quedar en la memoria viva"
    assert memoria.ultima_de_conversacion(turno_persistido.conversation_id) is devuelto


# --------------------------------------------------------------------------- #
# 2. El control de propiedad: la rehidratación no abre ninguna puerta trasera
# --------------------------------------------------------------------------- #
def test_un_token_de_otra_cuenta_recibe_403_sobre_una_explicacion_rehidratada(
    turno_persistido: TurnoPersistido, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El aislamiento por cuenta se aplica igual sobre un registro venido del disco.

    Es la prueba que impide que la rehidratación degenere en un agujero: el registro se
    encuentra —el ``404`` no es lo que protege aquí—, y aun así la respuesta es ``403``
    porque el ``cuenta_ref`` que se rehidrata es el que quedó grabado en el estado, no el
    del token que pregunta.
    """
    memoria = MemoriaConversaciones()
    intrusa = _identidad(CUENTA_AJENA)
    assert intrusa.cuenta_ref != turno_persistido.cuenta

    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        monkeypatch.setattr(rehidratacion, "obtener_checkpointer", lambda: checkpointer)
        with pytest.raises(ErrorApi) as capturado:
            _consultar_evidencia(turno_persistido.trace_id, intrusa, memoria)

        error = capturado.value
        assert error.status_code == 403, (
            "una explicación rehidratada de otra cuenta se entregó: la persistencia se "
            "convirtió en una vía para leer la facturación ajena"
        )
        assert error.cuerpo.codigo == "CUENTA_NO_AUTORIZADA"

        # Y el dueño legítimo sí la recibe, con el mismo fichero y el mismo estado: el
        # 403 es una decisión de autorización, no un fallo de la rehidratación.
        propia = _consultar_evidencia(
            turno_persistido.trace_id, _identidad(turno_persistido.cuenta), memoria
        )
    assert propia.total == turno_persistido.items_del_factset + len(turno_persistido.evidencia_viva)
    assert propia.factset_sha256 == turno_persistido.factset.sha256


# --------------------------------------------------------------------------- #
# 3. El límite declarado: el barrido está acotado, y eso es lo esperado
# --------------------------------------------------------------------------- #
def test_el_limite_de_busqueda_esta_declarado_y_es_el_documentado() -> None:
    """``LIMITE_BUSQUEDA`` es contrato: quien lo cambie tiene que venir a este fichero.

    No hay índice inverso ``explicacion_id → thread_id``, así que la búsqueda es un
    barrido de los *checkpoints* más recientes. El número es una decisión explícita —del
    orden de las últimas cuarenta explicaciones, a razón de unos seis *checkpoints* por
    turno— y no un accidente.
    """
    assert LIMITE_BUSQUEDA == 200
    assert isinstance(LIMITE_BUSQUEDA, int)


def test_mas_alla_del_limite_se_responde_404_y_eso_es_comportamiento_esperado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exige_dataset: None
) -> None:
    """Un turno lo bastante viejo cae fuera del barrido y vuelve al ``404`` de siempre.

    **Esto no es un fallo: es el límite declarado del módulo.** El barrido acotado se
    paga solo después de un reinicio, y acotarlo es lo que impide que una consulta
    recorra un fichero de *checkpoints* entero. La consecuencia honesta es que se
    recuperan los turnos **recientes**, no toda la historia; el ``404`` que se responde
    más allá del límite es el mismo de antes de que existiera la rehidratación, con el
    mismo mensaje accionable ("vuelva a pedir la explicación para regenerarla").

    Si algún día se añade el índice inverso que la documentación del módulo anota como
    trabajo pendiente, **este test es el que hay que cambiar**: dejará de ser cierto que
    más allá de doscientos *checkpoints* no se encuentra nada.
    """
    turno = _escribir_turno(tmp_path, "acotado")

    with _proceso_nuevo(turno.ruta) as checkpointer:
        # Dentro de la ventana: se encuentra.
        assert explicacion_persistida(turno.trace_id, saver=checkpointer.saver) is not None

        # Se empuja el turno fuera de la ventana con checkpoints posteriores.
        _ruido(checkpointer, LIMITE_BUSQUEDA + 5)

        assert explicacion_persistida(turno.trace_id, saver=checkpointer.saver) is None, (
            "el barrido encontró un turno que ya está fuera de sus 200 checkpoints; "
            "o el límite dejó de aplicarse o el orden de `list` dejó de ser de más "
            "nuevo a más viejo"
        )
        # Con una ventana mayor sigue estando en el fichero: lo que falla es el
        # acotamiento, no la persistencia.
        assert (
            explicacion_persistida(
                turno.trace_id, saver=checkpointer.saver, limite=LIMITE_BUSQUEDA * 10
            )
            is not None
        )

        # Y el endpoint responde el 404 de siempre, no un 500.
        monkeypatch.setattr(rehidratacion, "obtener_checkpointer", lambda: checkpointer)
        with pytest.raises(ErrorApi) as capturado:
            _consultar_evidencia(turno.trace_id, _identidad(turno.cuenta), MemoriaConversaciones())

    assert capturado.value.status_code == 404
    assert capturado.value.cuerpo.codigo == "EXPLICACION_NO_ENCONTRADA"
    assert "vuelva a pedir la explicación" in capturado.value.cuerpo.detalle


# --------------------------------------------------------------------------- #
# 4. La degradación: sin fichero se avisa, se sigue, y nunca se revienta
# --------------------------------------------------------------------------- #
def test_si_el_fichero_no_se_puede_abrir_se_degrada_a_memoria_con_aviso(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Un destino imposible degrada a memoria, lo dice en el log y no lanza.

    Perder la persistencia degrada la experiencia —la conversación no sobrevive al
    reinicio— pero no la corrección: las cifras salen del FactSet y el verificador sigue
    anclándolas igual. Lo que no puede pasar es que un disco lleno tumbe la explicación
    de un recibo.
    """
    ocupado = tmp_path / "ocupado"
    ocupado.write_text("no soy un directorio", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="packages.orquestacion.checkpointer"):
        # `ocupado/turnos.sqlite` es imposible: el padre es un fichero, no un directorio.
        degradado = abrir_checkpointer(ocupado / "turnos.sqlite")
    try:
        assert degradado.persistente is False
        assert degradado.ruta == VALOR_EN_MEMORIA
        assert "no se pudo abrir" in degradado.motivo
        assert any(
            "no se pudo abrir el fichero de checkpoints" in registro.getMessage()
            for registro in caplog.records
        ), "la degradación tiene que quedar dicha en el log, no pasar en silencio"

        # Y buscar en el almacén degradado devuelve `None`, no una excepción.
        assert explicacion_persistida("tr-000000000001", saver=degradado.saver) is None
    finally:
        degradado.cerrar()


def test_un_fichero_de_checkpoints_corrupto_no_impide_responder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un SQLite ilegible degrada a memoria y la consulta responde ``404``, no ``500``.

    ``explicacion_persistida`` solo puede convertir un ``404`` en un ``200``, jamás al
    revés: es la garantía que declara el módulo y la que se comprueba aquí con un
    fichero corrupto de verdad, no con un doble que finge estarlo.
    """
    corrupto = tmp_path / "corrupto.sqlite"
    corrupto.write_bytes(b"esto no es una base de datos sqlite" * 32)

    checkpointer = abrir_checkpointer(corrupto)
    try:
        assert checkpointer.persistente is False
        assert "no se pudo abrir" in checkpointer.motivo

        monkeypatch.setattr(rehidratacion, "obtener_checkpointer", lambda: checkpointer)
        memoria = MemoriaConversaciones()
        assert rehidratar_explicacion(memoria, "tr-000000000001") is None
        with pytest.raises(ErrorApi) as capturado:
            _consultar_evidencia("tr-000000000001", _identidad(CUENTA), memoria)
    finally:
        checkpointer.cerrar()

    assert capturado.value.status_code == 404
    assert capturado.value.cuerpo.codigo == "EXPLICACION_NO_ENCONTRADA"


def test_una_conexion_ya_cerrada_devuelve_none_en_vez_de_lanzar(
    turno_persistido: TurnoPersistido, caplog: pytest.LogCaptureFixture
) -> None:
    """Preguntar por el disco después del apagado ordenado no puede producir un ``500``.

    Es el fallo real que cubre el ``except Exception`` de ``explicacion_persistida``, y
    se provoca sin ningún doble: se abre el *checkpointer* de verdad sobre el fichero de
    verdad, se cierra su conexión —lo mismo que hace ``cerrar_checkpointer()`` en el
    apagado— y se pregunta después. SQLite lanza ``Cannot operate on a closed database``;
    el módulo tiene que tragárselo, avisar en el log y devolver ``None``, porque su
    contrato es que **solo puede convertir un 404 en un 200, jamás al revés**.
    """
    checkpointer = abrir_checkpointer(turno_persistido.ruta)
    saver = checkpointer.saver
    checkpointer.cerrar()

    with caplog.at_level(logging.WARNING, logger="packages.orquestacion.rehidratacion"):
        assert explicacion_persistida(turno_persistido.trace_id, saver=saver) is None
    assert any(
        "no se pudo rehidratar la explicación" in registro.getMessage()
        for registro in caplog.records
    ), "el fallo del camino de recuperación tiene que quedar en el log, no en silencio"


def test_un_explicacion_id_vacio_no_recorre_ningun_checkpoint(
    turno_persistido: TurnoPersistido,
) -> None:
    """Sin identificador no hay nada que buscar: se corta antes de tocar el disco."""
    with _proceso_nuevo(turno_persistido.ruta) as checkpointer:
        assert explicacion_persistida("", saver=checkpointer.saver) is None
        assert explicacion_persistida("tr-no-existe", saver=checkpointer.saver) is None


# --------------------------------------------------------------------------- #
# 5. No-regresión del respaldo: ORQUESTADOR=directo no importa LangGraph
# --------------------------------------------------------------------------- #
#: Guion que se ejecuta en un intérprete **limpio**. Tiene que ser un proceso aparte:
#: en la suite, LangGraph ya está importado por las pruebas del grafo, así que mirar
#: ``sys.modules`` desde aquí no probaría nada.
GUION_ORQUESTADOR_DIRECTO = """
import json, os, sys

os.environ["ORQUESTADOR"] = "directo"
os.environ["ENTORNO"] = "dev"
os.environ["LLM_MODE"] = "mock"
os.environ["CHECKPOINT_PATH"] = ":memory:"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, sys.argv[1])

from datetime import UTC, datetime, timedelta

from apps.api.deps import MemoriaConversaciones, obtener_reglas
from apps.api.errores import ErrorApi
from apps.api.routers.evidencia import obtener_evidencia
from apps.api.security import Identidad
from apps.api.settings import obtener_ajustes
from packages.core_domain.enums import Canal, NivelAseguramiento

identidad = Identidad(
    sub="C-DEMO-01",
    acr=NivelAseguramiento.LOA2,
    amr=["pwd", "app"],
    exp=datetime.now(UTC) + timedelta(hours=1),
    canal=Canal.APP,
)
estado, codigo = 0, ""
try:
    obtener_evidencia("tr-inexistente", identidad, MemoriaConversaciones(), obtener_reglas())
except ErrorApi as error:
    estado, codigo = error.status_code, error.cuerpo.codigo

prohibidos = {"langgraph", "langchain_core", "langchain", "langsmith"}
print(json.dumps({
    "orquestador": obtener_ajustes().orquestador,
    "estado": estado,
    "codigo": codigo,
    "importados": sorted(m for m in sys.modules if m.split(".")[0] in prohibidos),
}))
"""


def test_con_orquestador_directo_el_404_de_evidencia_no_importa_langgraph(
    tmp_path: Path, raiz_proyecto: Path
) -> None:
    """La vía de respaldo tiene que seguir funcionando **sin** la capa de orquestación.

    ``ORQUESTADOR=directo`` existe para que la demostración siga en pie si LangGraph da
    problemas. La rehidratación se cuelga del ``404`` de evidencia, así que es
    exactamente el sitio donde es fácil colar un import por la puerta de atrás y romper
    esa propiedad sin que nadie se entere: la suite seguiría verde porque en ella
    LangGraph siempre está cargado.

    Por eso se comprueba en un intérprete limpio: se pide la evidencia de una
    explicación que no existe y se exige que ``sys.modules`` no contenga ni ``langgraph``
    ni ``langchain_core`` al terminar.
    """
    guion = tmp_path / "sin_langgraph.py"
    guion.write_text(GUION_ORQUESTADOR_DIRECTO, encoding="utf-8")

    salida = subprocess.run(
        [sys.executable, str(guion), str(raiz_proyecto)],
        capture_output=True,
        text=True,
        cwd=str(raiz_proyecto),
        timeout=180,
    )
    assert salida.returncode == 0, (
        f"el intérprete limpio falló:\n{salida.stdout[-2000:]}\n{salida.stderr[-2000:]}"
    )
    informe = json.loads(salida.stdout.strip().splitlines()[-1])

    assert informe["orquestador"] == "directo"
    assert informe["estado"] == 404
    assert informe["codigo"] == "EXPLICACION_NO_ENCONTRADA"
    assert informe["importados"] == [], (
        "el 404 de evidencia importó la capa de orquestación con ORQUESTADOR=directo: "
        f"{informe['importados']}. Ese modo existe para funcionar sin LangGraph, y la "
        "guarda sobre sys.modules de `_rehidratar` es lo que lo garantiza"
    )
