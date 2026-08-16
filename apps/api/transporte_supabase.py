"""Transporte que sirve ``/bills/{cuenta}`` desde Supabase en vez de desde disco.

Por qué un transporte y no un adaptador nuevo
---------------------------------------------
``AdaptadorBrainyBill`` no habla con ficheros: habla con un :class:`Transporte`
(``apps/api/acl.py``). Cambiar de origen es implementar ese protocolo de dos métodos, no
reescribir el adaptador ni tocar el motor, el verificador o los routers. Es exactamente
para lo que se puso esa indirección, y hoy se cobra el interés.

Qué traduce
-----------
El dataset del desafío es **plano**: una fila por línea de cargo, con el recibo repetido
en cada una. El motor espera el documento de BrainyBill::

    {"cuenta_id": ..., "modalidad_renta": ..., "recibos": [{"header": ..., "lines": [...]}]}

Aquí se agrupa por recibo y se construye esa forma. Todo lo que no está en el dataset se
declara como ausente en vez de rellenarse con un valor plausible.

Lo que el dataset NO trae, y cómo se trata
-------------------------------------------
* ``PERIOD_START_DATE`` / ``PERIOD_END_DATE`` vienen **vacías en las 297 002 filas**. El
  ciclo se reconstruye desde ``ciclo`` (YYYYMMDD, que es el día de cierre) y la tabla
  ciclo→vencimiento de ``rules.yaml``, que se extrajo del propio dataset y coincide con
  el vídeo oficial. Sin eso no habría tramos que calcular.
* La **modalidad de renta** no es un campo: se deduce del ``GRUPO``. «CARGO FIJO VENCIDO»
  ⇒ vencida. La marca «RV » de la descripción corrobora pero no decide, porque su
  ausencia no implica adelantada (12 321 cargos sin marca son igualmente vencidos).
* No hay **órdenes de CRM**. El adaptador de Amdocs sigue leyendo lo que ya leía; sin
  movimientos, la atribución causal se apoya en las reglas de concepto y lo declara con
  menos confianza, que es lo correcto: inventar una orden sería peor.

Importes
--------
El dataset trae soles decimales; el modelo canónico exige **céntimos enteros**. La
conversión pasa por :func:`packages.core_domain.dinero.a_centimos`, la misma que usa el
resto del proyecto, para que no haya dos redondeos distintos conviviendo.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from apps.api.acl import CuentaNoEncontradaExterna, ErrorSistemaExterno
from packages.core_domain.dinero import a_centimos
from packages.datagen.mapping.movistar_map import concepto_desde_grupo, tipo_desde_crm

__all__ = ["VAR_DSN", "TransporteSupabase"]

_LOG = logging.getLogger(__name__)

#: Cadena de conexión. Se lee de aquí y no de ``DATABASE_URL`` para que la base del RAG
#: y la del dataset puedan ser distintas sin que una arrastre a la otra.
VAR_DSN = "SUPABASE_DB_URL"

#: Grupos que el propio facturador marca como excluidos del cálculo. Son los pares BONO
#: que se anulan (164 524 de 297 002 filas): sumarlos duplicaría cargos.
GRUPO_EXCLUIDO = "NO CONSIDERAR"

#: Los escenarios que pide el reto, cada uno con el grupo de cargo que lo delata en el
#: dataset y cómo se le nombra al que elige la cuenta.
#:
#: El nombre del grupo hace la mitad del trabajo: el facturador ya separa el prorrateo de
#: renta adelantada del de renta vencida con el sufijo «VENCIDO», así que las dos
#: modalidades que exige el reto salen del propio dato y no de una suposición nuestra.
#:
#: «Cambio de plan» no está, y es a propósito. En el CRM hay 11 648 órdenes «Pedido de
#: Cliente | Cambiar» que no dicen QUÉ se cambió; ofrecerlas como cambios de plan sería
#: prometer un escenario que después la explicación no puede sostener. Cuando el negocio
#: confirme qué significa ese código, esto es una línea más.
ESCENARIOS: tuple[tuple[str, str], ...] = (
    ("CARGO FIJO PROPORCIONAL VENCIDO", "Prorrateo · renta vencida"),
    ("CARGO FIJO PROPORCIONAL", "Prorrateo · renta adelantada"),
    ("DESCUENTO CARGO RECURRENTE", "Fin de descuento"),
    ("CARGO POR RECONEXION", "Reconexión tras suspensión"),
    ("PAQUETES", "Compra de paquetes"),
)

#: El cambio de plan no se delata por un grupo de cargo —no existe uno— sino por la orden
#: del CRM. Va aparte porque su consulta es otra: se buscan cuentas con una orden
#: «Cambiar» a petición del cliente y con movimiento en la renta del último ciclo, que es
#: donde un cambio de plan tiene que verse.
ESCENARIO_CAMBIO_PLAN = "Cambio de plan"


#: Familia canónica según el GRUPO del facturador. El orden de comprobación importa:
#: primero lo que el dato afirma, nunca el prefijo del código (una heurística sobre el
#: prefijo «FR» resultó ser falsa en los 221 códigos que empiezan así).
_FAMILIAS: tuple[tuple[str, str], ...] = (
    ("DESCUENTO", "AJUSTE"),
    ("RECONEXION", "UNICO"),
    ("TRAFICO", "UNICO"),
    ("ROAMING", "UNICO"),
    ("CARGA EXTERNA", "UNICO"),
)


def _familia(grupo: str) -> str:
    """Familia contable a partir del grupo del facturador."""
    g = (grupo or "").upper()
    for marca, familia in _FAMILIAS:
        if marca in g:
            return familia
    return "RECURRENTE"


def _periodo(ciclo: str) -> str:
    """``20260705`` → ``2026-07``. El ciclo nombra el día de CIERRE."""
    return f"{ciclo[:4]}-{ciclo[4:6]}" if len(ciclo) >= 6 else ciclo


def _fecha(valor: str | None) -> str | None:
    """``20260721`` → ``2026-07-21``; ``None`` si no hay dato."""
    if not valor or len(valor) != 8 or not valor.isdigit():
        return None
    return f"{valor[:4]}-{valor[4:6]}-{valor[6:]}"


def _apoyo_de_promocion(promocion: dict[str, Any] | None, grupo: str | None) -> dict[str, Any]:
    """Campos de promoción/cuota que le corresponden a ESTA línea, según su grupo.

    Se reparte por grupo y no se cuelga de todas las líneas porque cada dato explica una
    cosa distinta: la cuota del equipo solo significa algo en la línea del equipo, y el
    descuento en la del cargo fijo del plan. Si se colgara de cualquiera, la plantilla
    acabaría escribiendo «cuota 2 de 6» debajo de un paquete de datos, que es una cifra
    correcta puesta en el sitio equivocado —y eso el verificador numérico no lo caza,
    porque la cifra existe—.

    La regla del vídeo que decide el reparto: el descuento de alta aplica **solo al cargo
    fijo del plan**, nunca a servicios adicionales, paquetes ni financiamiento de equipos.
    """
    if not promocion:
        return {}
    g = (grupo or "").upper()
    apoyo: dict[str, Any] = {}
    # El cargo fijo del plan Y su línea de descuento. La promoción se aplica sobre el
    # cargo fijo, pero en muchos recibos lo que se mueve —y por tanto lo que hay que
    # explicar— es la línea de descuento, que es donde el cliente ve el cambio. Dejar
    # fuera esa línea significaba que las cuentas en promoción, justo las que llevan
    # cuota, no recibían el dato.
    if ("CARGO FIJO" in g or "DESCUENTO CARGO RECURRENTE" in g) and promocion.get(
        "cuota_numero"
    ) is not None:
        apoyo["cuota_numero"] = promocion["cuota_numero"]
        if promocion.get("meses_promocion") is not None:
            apoyo["cuotas_totales"] = promocion["meses_promocion"]
    return apoyo


class TransporteSupabase:
    """Sirve el documento de BrainyBill leyendo ``cargo_facturado``."""

    nombre = "supabase"

    #: Este transporte sirve además `/orders`. Lo consulta `crear_repositorio` para
    #: decidir si Amdocs puede apoyarse en él en vez de volver al disco.
    sirve_ordenes = True

    def __init__(self, dsn: str | None = None) -> None:
        """Abre la conexión.

        Raises:
            ErrorSistemaExterno: si falta el DSN o psycopg. Se falla al construir y no en
                la primera petición: un origen mal configurado debe notarse al arrancar,
                no a mitad de una conversación con un cliente.
        """
        cadena = (dsn or os.getenv(VAR_DSN) or "").strip()
        if not cadena:
            raise ErrorSistemaExterno(self.nombre, f"falta {VAR_DSN}")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - psycopg es dependencia declarada
            raise ErrorSistemaExterno(self.nombre, "falta psycopg") from exc
        try:
            self._conexion = psycopg.connect(
                cadena,
                connect_timeout=20,
                autocommit=True,
                # Sin sentencias preparadas. El DSN de Supabase apunta al **pooler de
                # transacciones** (puerto 6543, pgbouncer), que multiplexa cada consulta
                # sobre conexiones de servidor distintas y no admite `PREPARE`: psycopg3
                # prepara sola a partir de la quinta ejecución de la misma consulta, y la
                # siguiente caía en otra conexión con
                # `DuplicatePreparedStatement: prepared statement "_pg3_0" already exists`.
                #
                # El síntoma era desconcertante y peligrosísimo para una demostración: las
                # cinco primeras cuentas se explicaban y **de la sexta en adelante todo el
                # mundo recibía un 500**, sin que nada cambiara en los datos ni en el
                # código. Se desactiva aquí y no se sube el umbral porque el pooler no las
                # admite nunca, no es cuestión de cuántas.
                prepare_threshold=None,
            )
        except Exception as exc:
            raise ErrorSistemaExterno(self.nombre, f"no se pudo conectar: {exc}") from exc

    # -- protocolo Transporte ------------------------------------------------ #
    def obtener(self, ruta: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """Responde a ``/bills/{cuenta_id}`` y a ``/orders/{cuenta_id}``."""
        if ruta.startswith("/bills/"):
            cuenta_id = ruta.removeprefix("/bills/").strip("/")
            ciclos = int((params or {}).get("cycles", 6))
            return self._documento(cuenta_id, ciclos)
        if ruta.startswith("/orders/"):
            return self._ordenes(ruta.removeprefix("/orders/").strip("/"))
        raise ErrorSistemaExterno(self.nombre, f"ruta no servida por este transporte: {ruta}")

    def _ordenes(self, cuenta_id: str) -> dict[str, Any]:
        """Órdenes del CRM de la cuenta, en el formato que espera el adaptador de Amdocs.

        Es la pieza que faltaba. Sin órdenes, la atribución causal solo podía apoyarse en
        reglas de concepto y toda explicación tenía que decir que el motivo no constaba;
        con ellas, un «Cargo por Reconexión» deja de ser una hipótesis del motor y pasa a
        tener detrás una orden de reconexión con su fecha.

        Se devuelve el formato ``amdocs`` —el mismo que sirve el mock— para no abrir un
        segundo camino de conversión: la traducción a movimientos canónicos sigue estando
        donde estaba, en :class:`AdaptadorAmdocs` y en ``movistar_map``.

        Las órdenes cuyo par (razón, tipo de ítem) no es concluyente se entregan igual,
        con ``ORDER_TYPE`` vacío: descartarlas aquí escondería en el transporte una
        decisión que el ACL ya toma —y registra— con su aviso.
        """
        filas = self._conexion.execute(
            """
            SELECT id, subscriber_key, razon_desc, tipo_item, completado, inicio, estado
            FROM orden_servicio
            WHERE financial_account = %s
            ORDER BY completado NULLS LAST, id
            """,
            (cuenta_id,),
        ).fetchall()

        ordenes: list[dict[str, Any]] = []
        for identificador, suscripcion, razon, item, completado, inicio, estado in filas:
            tipo = tipo_desde_crm(razon, item)
            momento = completado or inicio
            ordenes.append(
                {
                    "ORDER_ID": str(identificador),
                    "ACCOUNT_ID": cuenta_id,
                    "ORDER_TYPE": str(tipo) if tipo else "",
                    "ORDER_DATE": momento.isoformat() if momento else "",
                    "SERVICE_ID": str(suscripcion or ""),
                    "CHANNEL": "CRM",
                    # El detalle conserva el vocabulario ORIGINAL del CRM. Es lo que
                    # permite que un asesor lea «Cobranza Manual con Cargo» y lo reconozca
                    # en su propio sistema, en vez de un tipo canónico que allí no existe.
                    "DETAIL_JSON": {
                        "razon": razon or "",
                        "tipo_item": item or "",
                        "estado": estado or "",
                    },
                }
            )
        return {
            "cuenta_id": cuenta_id,
            "formato": "amdocs",
            "total": len(ordenes),
            "orders": ordenes,
        }

    def cerrar(self) -> None:
        """Cierra la conexión. Idempotente y silenciosa: cerrar no debe propagar."""
        with contextlib.suppress(Exception):
            self._conexion.close()

    # -- listado para la demo ------------------------------------------------ #
    def cuentas(self, limite: int = 10) -> list[str]:
        """Cuentas reales que **sirven para demostrar**: las de mayor variación real.

        Existe porque ``GET /dev/cuentas`` listaba siempre los ficheros del disco, sin
        mirar de dónde salen de verdad los recibos. Con Supabase sirviendo el dataset, la
        interfaz ofrecía las cuentas sintéticas ``C-DEMO-*``, el cliente elegía una, el
        ACL no la encontraba y el login moría con «la cuenta no existe». El desplegable
        contaba una cosa y el motor otra.

        El criterio no es «que exista» sino **que tenga algo que explicar**. Este producto
        responde *por qué varió su recibo*: una cuenta con dos ciclos idénticos abre una
        pantalla que dice que no pasó nada, y quien la elija concluirá, con razón, que el
        asistente no funciona. Se piden por tanto los dos ciclos más recientes de cada
        cuenta, se comparan sus totales y se ofrecen las de mayor diferencia primero, de
        modo que la que la interfaz precarga sea la más demostrativa que hay.

        La comparación es una suma en SQL, no el ``FactSet``: aquí solo hace falta ordenar
        candidatas. El importe que se le enseña al cliente lo sigue calculando el motor,
        con sus reglas y su invariante, como todo lo demás.

        Returns:
            Los identificadores, de mayor a menor variación. Lista vacía si la consulta
            falla: un desplegable incompleto es un incordio, pero tumbar ``/dev/cuentas``
            dejaría la pantalla de entrada en blanco.
        """
        try:
            filas = self._conexion.execute(
                """
                WITH por_ciclo AS (
                    SELECT financial_account_key AS cuenta,
                           ciclo,
                           SUM(charge_total_amount) AS total,
                           ROW_NUMBER() OVER (
                               PARTITION BY financial_account_key ORDER BY ciclo DESC
                           ) AS puesto
                    FROM cargo_facturado
                    WHERE grupo <> %s
                    GROUP BY financial_account_key, ciclo
                )
                SELECT actual.cuenta
                FROM por_ciclo AS actual
                JOIN por_ciclo AS previo
                  ON previo.cuenta = actual.cuenta AND previo.puesto = 2
                WHERE actual.puesto = 1
                  AND ABS(actual.total - previo.total) >= 1
                ORDER BY ABS(actual.total - previo.total) DESC
                LIMIT %s
                """,
                (GRUPO_EXCLUIDO, max(1, limite)),
            ).fetchall()
        except Exception as error:
            _LOG.warning("no se pudieron listar cuentas de Supabase: %s", error)
            return []
        return [str(fila[0]) for fila in filas]

    def cuentas_por_escenario(self, por_escenario: int = 2) -> dict[str, str]:
        """Cuentas elegidas porque **ejemplifican** un caso del reto, no por su tamaño.

        El criterio anterior —mayor variación entre los dos últimos ciclos— parecía el
        más demostrativo y resultó ser el peor. Las variaciones más grandes del dataset
        son bajas de servicios caros, y una baja no trae orden en el CRM: las diez cuentas
        que salían tenían 21 de 27 líneas sin causa, ningún prorrateo, ningún fin de
        descuento. El motor sabía explicar cinco escenarios y la demo solo podía enseñar
        uno, no porque faltara el dato sino porque el selector miraba lo que no era.

        Ahora se pregunta por el caso: para cada escenario se buscan cuentas cuyo **último
        ciclo** contenga ese grupo de cargo, que es la garantía de que el concepto está en
        el recibo que se va a explicar y no en uno viejo. Se sigue exigiendo variación
        entre ciclos —sin ella la pantalla dice que no pasó nada— y se ordena por tamaño
        dentro de cada escenario.

        Args:
            por_escenario: cuántas cuentas se ofrecen de cada caso. Dos bastan para poder
                enseñar otra si la primera resulta poco clara.

        Returns:
            ``{cuenta: descripción del escenario}``, en el orden de ``ESCENARIOS``. Una
            cuenta puede ejemplificar dos casos; se queda con el primero, porque lo que
            se está eligiendo es qué mirar primero, no clasificando la cuenta.
        """
        elegidas: dict[str, str] = {}
        for grupo, etiqueta in ESCENARIOS:
            try:
                filas = self._conexion.execute(
                    """
                    WITH por_ciclo AS (
                        SELECT financial_account_key AS cuenta,
                               ciclo,
                               SUM(charge_total_amount) AS total,
                               ROW_NUMBER() OVER (
                                   PARTITION BY financial_account_key ORDER BY ciclo DESC
                               ) AS puesto
                        FROM cargo_facturado
                        WHERE grupo <> %s
                        GROUP BY financial_account_key, ciclo
                    ),
                    con_caso AS (
                        SELECT DISTINCT financial_account_key AS cuenta, ciclo
                        FROM cargo_facturado
                        WHERE grupo = %s
                    )
                    SELECT actual.cuenta
                    FROM por_ciclo AS actual
                    JOIN por_ciclo AS previo
                      ON previo.cuenta = actual.cuenta AND previo.puesto = 2
                    JOIN con_caso
                      ON con_caso.cuenta = actual.cuenta AND con_caso.ciclo = actual.ciclo
                    WHERE actual.puesto = 1
                      AND ABS(actual.total - previo.total) >= 1
                    ORDER BY ABS(actual.total - previo.total) DESC
                    LIMIT %s
                    """,
                    (GRUPO_EXCLUIDO, grupo, max(1, por_escenario)),
                ).fetchall()
            except Exception as error:
                # Un escenario que falla no puede dejar la pantalla de entrada vacía: se
                # pierde ese caso del recorrido y los demás siguen ofreciéndose.
                _LOG.warning("no se pudieron listar cuentas de «%s»: %s", etiqueta, error)
                continue
            for fila in filas:
                elegidas.setdefault(str(fila[0]), etiqueta)

        try:
            filas = self._conexion.execute(
                """
                WITH por_ciclo AS (
                    SELECT financial_account_key AS cuenta,
                           ciclo,
                           SUM(charge_total_amount) AS total,
                           ROW_NUMBER() OVER (
                               PARTITION BY financial_account_key ORDER BY ciclo DESC
                           ) AS puesto
                    FROM cargo_facturado
                    WHERE grupo <> %s
                    GROUP BY financial_account_key, ciclo
                ),
                con_renta AS (
                    SELECT DISTINCT financial_account_key AS cuenta, ciclo
                    FROM cargo_facturado
                    WHERE grupo IN ('CARGO FIJO', 'CARGO FIJO VENCIDO')
                ),
                con_orden AS (
                    SELECT DISTINCT financial_account AS cuenta
                    FROM orden_servicio
                    WHERE tipo_item ILIKE 'Cambiar%%' AND razon_desc ILIKE '%%liente%%'
                )
                SELECT actual.cuenta
                FROM por_ciclo AS actual
                JOIN por_ciclo AS previo
                  ON previo.cuenta = actual.cuenta AND previo.puesto = 2
                JOIN con_renta
                  ON con_renta.cuenta = actual.cuenta AND con_renta.ciclo = actual.ciclo
                JOIN con_orden ON con_orden.cuenta = actual.cuenta
                WHERE actual.puesto = 1
                  AND ABS(actual.total - previo.total) >= 1
                ORDER BY ABS(actual.total - previo.total) DESC
                LIMIT %s
                """,
                (GRUPO_EXCLUIDO, max(1, por_escenario)),
            ).fetchall()
        except Exception as error:
            _LOG.warning("no se pudieron listar cuentas de cambio de plan: %s", error)
            filas = []
        for fila in filas:
            elegidas.setdefault(str(fila[0]), ESCENARIO_CAMBIO_PLAN)
        return elegidas

    def promocion(self, cuenta_id: str) -> dict[str, Any]:
        """Promoción y cuota vigentes de la cuenta, según ``v_descuento_cuota``.

        Es el dato que faltaba para que las fórmulas dejaran de trabajar con parámetros
        por defecto. La transcripción del vídeo enuncia «50 % durante 3 meses, unos 90
        días»; esta vista lo trae medido **por cliente**: su porcentaje, su duración, los
        días que lleva consumidos y en qué cuota va.

        Dos detalles que no son míos, son del dato, y que confirman la fórmula:

        * ``tipo_renta`` viene dicho (``RA``/``RV``) en vez de deducirse del sufijo del
          grupo de cargo. Es la misma distinción del vídeo, escrita en origen.
        * Los días se reparten según la modalidad: las filas ``RV`` traen
          ``diasvencidos`` y las ``RA`` ``diasadelantados``. Por eso aquí se toma el que
          corresponde y se expone como un único ``dias_consumidos``: al motor le da igual
          de qué columna salió, lo que necesita es cuánta bolsa se gastó.

        Returns:
            El registro más reciente de la cuenta, o ``{}`` si no tiene promoción. Un
            diccionario vacío es una respuesta legítima —la mayoría de recibos no están
            en promoción— y quien llama no debe distinguirla de un fallo, porque el
            comportamiento es el mismo: se explica sin ese dato.
        """
        try:
            fila = self._conexion.execute(
                """
                SELECT tipo_renta, promotionduration, porcentajepromo,
                       diasvencidos, diasadelantados, cuotaactual,
                       monto_descuento, descripcion, tipo_descuento, fechainicio, fechafin
                FROM v_descuento_cuota
                WHERE cuentafinanciera = %s
                ORDER BY fechainicio DESC NULLS LAST
                LIMIT 1
                """,
                (cuenta_id,),
            ).fetchone()
        except Exception as error:
            # Sin promoción se explica igual, con los valores por defecto de la fórmula.
            # Tumbar la explicación por no poder leer un dato de apoyo sería cambiar una
            # respuesta buena por ninguna.
            _LOG.warning("no se pudo leer la promoción de %s: %s", cuenta_id, error)
            return {}
        if not fila:
            return {}

        vencida = (fila[0] or "").upper() == "RV"
        dias = fila[3] if vencida else fila[4]
        return {
            "modalidad_renta": "VENCIDA" if vencida else "ADELANTADA",
            "meses_promocion": int(fila[1]) if fila[1] is not None else None,
            "porcentaje_promocion": float(fila[2]) if fila[2] is not None else None,
            "dias_consumidos": int(dias) if dias is not None else None,
            "cuota_numero": int(fila[5]) if fila[5] is not None else None,
            "monto_descuento_cent": (
                round(Decimal(fila[6]) * 100) if fila[6] is not None else None
            ),
            "descripcion": fila[7] or "",
            "tipo_descuento": fila[8] or "",
            "vigencia": (fila[9], fila[10]),
        }

    # -- construcción del documento ------------------------------------------ #
    def _documento(self, cuenta_id: str, ciclos: int) -> dict[str, Any]:
        filas = self._conexion.execute(
            """
            SELECT ciclo, legal_invoice_number, billing_cycle_key, charge_code_id,
                   charge_code_desc, charge_total_amount, grupo, sub_grupo,
                   fecha_vencimiento, deuda
            FROM cargo_facturado
            WHERE financial_account_key = %s AND grupo <> %s
            ORDER BY ciclo DESC, legal_invoice_number, charge_code_id
            """,
            (cuenta_id, GRUPO_EXCLUIDO),
        ).fetchall()
        if not filas:
            raise CuentaNoEncontradaExterna(self.nombre, cuenta_id, "supabase:cargo_facturado")

        por_ciclo: dict[str, list[tuple]] = defaultdict(list)
        for fila in filas:
            por_ciclo[fila[0]].append(fila)

        # La modalidad se decide una vez para toda la cuenta y se propaga a cada
        # cabecera: el modelo canónico la exige por recibo, y deducirla por separado en
        # cada ciclo daría una cuenta que cambia de modalidad de un mes a otro, que es
        # imposible en facturación real.
        vencida = any("VENCIDO" in (f[6] or "").upper() for f in filas)
        modalidad = "VENCIDA" if vencida else "ADELANTADA"

        # Los `ciclos` más recientes, el más nuevo primero: es el contrato de BrainyBill.
        recientes = sorted(por_ciclo, reverse=True)[:ciclos]
        # Una sola consulta de promoción para todo el documento: es la misma para toda
        # la cuenta, y pedirla dentro del bucle multiplicaría por ciclo un viaje que ya
        # está indexado pero que no es gratis.
        promocion = self.promocion(cuenta_id)
        recibos = [
            self._recibo(cuenta_id, c, por_ciclo[c], modalidad, promocion) for c in recientes
        ]
        planta = self._planta(cuenta_id)

        return {
            "cuenta_id": cuenta_id,
            "modalidad_renta": modalidad,
            "segmento": planta.get("segmento", "MASIVO"),
            "dia_ciclo": planta.get("dia_ciclo") or filas[0][2] or 1,
            "moneda": "PEN",
            "origen": "supabase/cargo_facturado",
            "beneficios_vigentes": [],
            "recibos": recibos,
            **({"alta_cuenta": planta["alta_cuenta"]} if planta.get("alta_cuenta") else {}),
        }

    def _planta(self, cuenta_id: str) -> dict[str, Any]:
        """Datos de la cuenta en la planta comercial: negocio, ciclo y fecha de alta.

        Por qué importa la fecha de alta
        --------------------------------
        Es lo que separa *«su recibo subió»* de *«este es su primer recibo completo»*. Una
        cuenta dada de alta a mitad del ciclo anterior facturó días sueltos y este mes
        factura el mes entero: la diferencia no es un cobro nuevo, es que el primer recibo
        era parcial. Sin este dato la explicación atribuye la subida a otra causa, que es
        una alucinación con la aritmética correcta. Además ``rules.yaml`` fija una ventana
        de 90 días para el descuento de alta, y sin fecha no hay forma de aplicarla.

        Devuelve ``{}`` si la cuenta no está en la planta: 1 552 de las 20 000 no cruzan
        con la facturación, y el documento debe salir igual con los valores por defecto.
        """
        try:
            fila = self._conexion.execute(
                """
                SELECT negocio, lob_type, ciclo, fecha_activacion_original
                FROM cliente_planta WHERE financial_account = %s LIMIT 1
                """,
                (cuenta_id,),
            ).fetchone()
        except Exception:  # pragma: no cover - la planta es opcional, nunca bloquea
            return {}
        if not fila:
            return {}
        negocio, lob, ciclo_dia, alta = fila
        datos: dict[str, Any] = {}
        # «MT/CONVERGENTE» es un cliente con paquete de varios servicios; el resto, masivo
        # móvil. El segmento cambia el tono, no las cifras.
        if negocio:
            datos["segmento"] = "CONVERGENTE" if "CONVERGENTE" in negocio.upper() else "MASIVO"
        if lob:
            datos["linea_negocio"] = lob
        if ciclo_dia:
            datos["dia_ciclo"] = int(ciclo_dia)
        if alta:
            # La planta escribe «14/12/2017 00:00»; el resto del sistema usa ISO.
            dia = str(alta).split(" ")[0].split("/")
            if len(dia) == 3:
                datos["alta_cuenta"] = f"{dia[2]}-{int(dia[1]):02d}-{int(dia[0]):02d}"
        return datos

    def _recibo(
        self,
        cuenta_id: str,
        ciclo: str,
        filas: list[tuple],
        modalidad: str,
        promocion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Un recibo con su cabecera y sus líneas, en la forma que espera el adaptador."""
        cierre = date(int(ciclo[:4]), int(ciclo[4:6]), int(ciclo[6:]))
        # El ciclo nombra el día de cierre y la facturación reanuda al siguiente, así que
        # el periodo abierto empieza el día después del cierre anterior.
        inicio = cierre - timedelta(days=30)
        lineas: list[dict[str, Any]] = []
        total = 0
        for indice, fila in enumerate(filas, start=1):
            monto = a_centimos(fila[5] or 0)
            total += monto
            lineas.append(
                {
                    "linea_id": indice,
                    # El código canónico si el grupo lo identifica; si no, el código
                    # crudo del facturador. Sin esta traducción, `regla_concepto_causa`
                    # no encontraba entrada para ningún cargo real y la atribución se
                    # quedaba sin candidatos aunque hubiera una orden del CRM delante.
                    "concepto_id": concepto_desde_grupo(fila[6], fila[3]) or fila[3],
                    # El código original se conserva como nombre técnico: el asesor tiene
                    # que poder buscarlo en el facturador, donde el canónico no existe.
                    "nombre_comercial": fila[4] or fila[3],
                    "codigo_origen": fila[3],
                    "familia": _familia(fila[6]),
                    "descripcion": fila[4] or "",
                    "monto_cent": monto,
                    "periodo": _periodo(ciclo),
                    "cantidad": 1,
                    "afecto_igv": True,
                    **_apoyo_de_promocion(promocion, fila[6]),
                }
            )
        return {
            "header": {
                "recibo_id": filas[0][1],
                "cuenta_id": cuenta_id,
                "periodo": _periodo(ciclo),
                "modalidad_renta": modalidad,
                "emision": cierre.isoformat(),
                "vencimiento": _fecha(filas[0][8]) or cierre.isoformat(),
                "ciclo_inicio": inicio.isoformat(),
                "ciclo_fin": cierre.isoformat(),
                "dias_ciclo": (cierre - inicio).days,
                "moneda": "PEN",
                "total_cent": total,
                "deuda_anterior_cent": 0,
            },
            "lines": lineas,
        }
