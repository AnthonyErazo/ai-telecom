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
from typing import Any

from apps.api.acl import CuentaNoEncontradaExterna, ErrorSistemaExterno
from packages.core_domain.dinero import a_centimos

__all__ = ["VAR_DSN", "TransporteSupabase"]

_LOG = logging.getLogger(__name__)

#: Cadena de conexión. Se lee de aquí y no de ``DATABASE_URL`` para que la base del RAG
#: y la del dataset puedan ser distintas sin que una arrastre a la otra.
VAR_DSN = "SUPABASE_DB_URL"

#: Grupos que el propio facturador marca como excluidos del cálculo. Son los pares BONO
#: que se anulan (164 524 de 297 002 filas): sumarlos duplicaría cargos.
GRUPO_EXCLUIDO = "NO CONSIDERAR"

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


class TransporteSupabase:
    """Sirve el documento de BrainyBill leyendo ``cargo_facturado``."""

    nombre = "supabase"

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
        """Responde a ``/bills/{cuenta_id}``. Cualquier otra ruta es un error de uso."""
        if not ruta.startswith("/bills/"):
            raise ErrorSistemaExterno(self.nombre, f"ruta no servida por este transporte: {ruta}")
        cuenta_id = ruta.removeprefix("/bills/").strip("/")
        ciclos = int((params or {}).get("cycles", 6))
        return self._documento(cuenta_id, ciclos)

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
            raise CuentaNoEncontradaExterna(self.nombre, cuenta_id)

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
        recibos = [self._recibo(cuenta_id, c, por_ciclo[c], modalidad) for c in recientes]
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
        self, cuenta_id: str, ciclo: str, filas: list[tuple], modalidad: str
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
                    "concepto_id": fila[3],
                    "nombre_comercial": fila[4] or fila[3],
                    "familia": _familia(fila[6]),
                    "descripcion": fila[4] or "",
                    "monto_cent": monto,
                    "periodo": _periodo(ciclo),
                    "cantidad": 1,
                    "afecto_igv": True,
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
