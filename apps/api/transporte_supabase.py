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
            self._conexion = psycopg.connect(cadena, connect_timeout=20, autocommit=True)
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
