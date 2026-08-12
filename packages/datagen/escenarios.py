"""Los ocho escenarios sintéticos del generador (sección 8 de la especificación).

Cada escenario implementa la misma interfaz::

    escenario.aplicar(cliente, ciclo, rng) -> (lineas, movimientos, ground_truth)

y **escribe su ground truth en el mismo acto** de fabricar las líneas: la fila de
``gt_causa_delta`` sale de la misma aritmética que produjo el importe, nunca de
observar el recibo terminado. Si un escenario mueve un importe y no declara la fila
correspondiente, ``generar.py`` aborta la generación.

Los ocho escenarios funcionan en **ambas modalidades de renta**, con composiciones
distintas porque el documento es distinto:

* ``VENCIDA``  — el recibo del ciclo *k* cobra el ciclo *k* que ya cerró; los cambios
  de mitad de ciclo se ven como una renta partida en tramos.
* ``ADELANTADA`` — el recibo del ciclo *k* cobra la renta del ciclo *k+1* y **corrige**
  el ciclo *k*; por eso conviven dos rentas en el mismo documento y aparecen líneas de
  ajuste retroactivo que en renta vencida no existen.

Dos convenciones que valen para todo el módulo:

* Todo importe es ``int`` en céntimos. No hay ``float`` en ninguna parte.
* **Ningún nombre propio del dataset lleva un número suelto.** Los nombres viajan
  dentro del FactSet (etiquetas de tramo, plan vigente, nombre comercial de la línea)
  y un número suelto en ellos sería una cifra que el verificador numérico encontraría
  en el texto sin poder anclarla. Sí se admite el sufijo de capacidad pegado a su
  unidad —``10GB``, ``100Mb``— porque la expresión maestra del verificador exige que
  un entero **no** vaya seguido de un carácter de palabra: ``10GB`` no produce ninguna
  aserción. Lo que queda prohibido es el número separado por espacio (``Router WiFi
  6``), que sí se extraería. Los únicos números narrables del dataset son importes,
  días, fechas y números de cuota, que están anclados en el FactSet.

Procedencia del catálogo comercial
----------------------------------
Los nombres y los precios de los planes, paquetes y bonos **no son inventados**: son
los del catálogo oficial de ofertas que Movistar entregó para la Hackathon AI Telecom
2026, con los nombres comerciales y los importes medidos sobre el dataset del Desafío 1.
La propia ficha declara ese catálogo ficticio y creado para la hackaton, así que los
nombres comerciales y los precios pueden reproducirse; el fichero, no. Aquí solo vive
el **vocabulario**, transcrito a mano y con los importes convertidos a céntimos
enteros (``S/ 39.90 → 3990``). Adoptarlo hace que nuestros recibos sintéticos hablen
el mismo idioma que el resto de la hackaton y que un export real encaje sin traducir.
"""

from __future__ import annotations

import calendar
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from random import Random
from typing import Any, ClassVar, NamedTuple

from packages.core_domain.dinero import (
    Centimos,
    aplicar_porcentaje,
    prorratear,
    redondear_banca,
    repartir_mayor_resto,
)
from packages.core_domain.enums import EstadoServicio, ModalidadRenta, TipoMovimiento
from packages.core_domain.esquemas.evaluacion import GroundTruthCausaDelta
from packages.core_domain.esquemas.movimiento import (
    CuotaFinanciamiento,
    DetalleAltaEquipoFinanciado,
    DetalleAltaPaquete,
    DetalleCambioPlan,
    DetalleFinDescuento,
    DetalleNota,
    DetalleReconexion,
    DetalleSuspension,
    MovementEvent,
    PlanFinanciamiento,
)
from packages.core_domain.esquemas.recibo import (
    MESES_ES,
    LineaRecibo,
    Tramo,
    etiqueta_rango_fechas,
)
from packages.core_domain.reglas import ConfiguracionReglas, cargar_reglas

__all__ = [
    "CANALES_CLIENTE",
    "CANALES_ORDEN",
    "CONCEPTO_DEUDA_ANTERIOR",
    "DEPARTAMENTOS",
    "EDADES_RANGO",
    "EQUIPOS",
    "ESCENARIOS",
    "NOMBRES_ESCENARIOS",
    "ORDEN_CONCEPTOS",
    "PAQUETES_UNICOS",
    "PLANES_HOGAR",
    "PLANES_MOVIL",
    "PLANES_TOTAL",
    "PLANES_TV",
    "TIPOS_CLIENTE",
    "AltaPaquete",
    "CambioPlanMedioCiclo",
    "CicloFacturacion",
    "CorteReconexion",
    "CuotaEquipoFinanciado",
    "DescuentoBase",
    "DeudaAnterior",
    "Escenario",
    "Estable",
    "FinDescuento",
    "FinanciamientoBase",
    "NotaCredito",
    "PerfilCliente",
    "ResultadoEscenario",
    "ServicioBase",
    "combinar",
    "construir_ciclo",
    "construir_linea",
    "construir_plan_financiamiento",
    "desplazar_periodo",
    "escenarios_por_nombre",
    "inicio_de_ciclo",
    "lineas_base",
    "obtener_escenario",
    "orden_de_concepto",
    "pares_compatibles",
    "son_compatibles",
]

#: Concepto que se arrastra fuera del total del periodo (igual que en ``Recibo``).
CONCEPTO_DEUDA_ANTERIOR = "DEUDA_ANTERIOR"

#: Hora fija de los movimientos: el generador es determinista, no simula relojes.
HORA_MOVIMIENTO = time(10, 30)

#: Canales por los que entra una orden en Amdocs. Es el vocabulario del **sistema de
#: órdenes**, no el del perfil comercial del cliente: son dos cosas distintas y se
#: mantienen separadas (véase :data:`CANALES_CLIENTE`).
CANALES_ORDEN: tuple[str, ...] = ("APP", "WEB", "TIENDA", "CALL_CENTER", "WHATSAPP")

# --------------------------------------------------------------------------- #
# Perfil comercial del cliente — vocabulario del dataset real de Movistar
# --------------------------------------------------------------------------- #
# Los cuatro vocabularios que siguen son genéricos y públicos: la dicotomía
# postpago/prepago, los rangos de edad al uso y los veinticinco departamentos del Perú.
# No proceden de ningún fichero entregado por Movistar, y por eso pueden estar aquí: la
# cláusula 9 de las bases impone diez años de confidencialidad sobre los datos entregados,
# así que ningún CSV de Movistar entra en este repositorio.
#
# Por qué están aquí y no en `rules.yaml`: no son reglas de cálculo. Ninguno de estos
# campos toca un importe. Sirven para personalizar el trato —a un prepago no se le habla
# de "su recibo mensual", a quien resuelve todo por la app no se le ofrece ir a una
# tienda— y para que, el día que llegue un export real, el perfil encaje sin traducir.
#
# Cuidado al narrarlos: ``edad_rango`` es el único que lleva dígitos. "18-25" no produce
# ninguna aserción numérica (el guion bloquea la extracción), pero "65+" sí extraería un
# 65 suelto. Estos campos viajan en la metainformación del recibo, no en el texto.

#: ``tipo_cliente`` del dataset real.
TIPOS_CLIENTE: tuple[str, ...] = ("postpago", "prepago")

#: ``edad_rango`` del dataset real.
EDADES_RANGO: tuple[str, ...] = ("18-25", "26-35", "36-45", "46-55", "56-65", "65+")

#: ``ubicacion_departamento`` del dataset real (ocho departamentos más "Otro").
DEPARTAMENTOS: tuple[str, ...] = (
    "Lima",
    "Arequipa",
    "La Libertad",
    "Piura",
    "Lambayeque",
    "Cusco",
    "Junin",
    "Ica",
    "Otro",
)

#: ``canal_mas_usado`` del dataset real: por dónde prefiere resolver este cliente.
CANALES_CLIENTE: tuple[str, ...] = ("Digital", "Tienda", "Call In", "Call Out")

#: Planes móviles del **dataset del Desafío 1**, con el importe medio medido sobre
#: ``cargo_facturado`` (4 336 apariciones el más común). En orden ascendente de precio, que es
#: lo que hace realista un cambio de plan: se sube un escalón o se baja uno.
#:
#: Antes esto reproducía el catálogo ``OF001``–``OF022`` del **Desafío 2**, de cuando el
#: generador se construyó sin dataset propio. Ya no hace falta y además era una fuente de
#: datos que la declaración de procedencia no debía tener que justificar: los nombres reales
#: están en el dataset de este desafío, y son los que el cliente ve en su recibo.
PLANES_MOVIL: tuple[tuple[str, Centimos], ...] = (
    ("Plan Ahorro Mi Movistar S/20.9", 1980),
    ("Plan Ahorro Mi Movistar S/25.9", 2272),
    ("Plan Mi Movistar S/29.9", 2764),
    ("Plan Mi Movistar S/31.9", 2846),
)

#: Canales por los que entra una orden en Amdocs. Es el vocabulario del **sistema de
#: órdenes**, no el del perfil comercial del cliente: son dos cosas distintas y se
#: mantienen separadas (véase :data:`CANALES_CLIENTE`).
CANALES_ORDEN: tuple[str, ...] = ("APP", "WEB", "TIENDA", "CALL_CENTER", "WHATSAPP")

# --------------------------------------------------------------------------- #
# Perfil comercial del cliente — vocabulario del dataset real de Movistar
# --------------------------------------------------------------------------- #
# Los cuatro vocabularios que siguen son genéricos y públicos: la dicotomía
# postpago/prepago, los rangos de edad al uso y los veinticinco departamentos del Perú.
# No proceden de ningún fichero entregado por Movistar, y por eso pueden estar aquí: la
# cláusula 9 de las bases impone diez años de confidencialidad sobre los datos entregados,
# así que ningún CSV de Movistar entra en este repositorio.
#
# Por qué están aquí y no en `rules.yaml`: no son reglas de cálculo. Ninguno de estos
# campos toca un importe. Sirven para personalizar el trato —a un prepago no se le habla
# de "su recibo mensual", a quien resuelve todo por la app no se le ofrece ir a una
# tienda— y para que, el día que llegue un export real, el perfil encaje sin traducir.
#
# Cuidado al narrarlos: ``edad_rango`` es el único que lleva dígitos. "18-25" no produce
# ninguna aserción numérica (el guion bloquea la extracción), pero "65+" sí extraería un
# 65 suelto. Estos campos viajan en la metainformación del recibo, no en el texto.

#: ``tipo_cliente`` del dataset real.
TIPOS_CLIENTE: tuple[str, ...] = ("postpago", "prepago")

#: ``edad_rango`` del dataset real.
EDADES_RANGO: tuple[str, ...] = ("18-25", "26-35", "36-45", "46-55", "56-65", "65+")

#: ``ubicacion_departamento`` del dataset real (ocho departamentos más "Otro").
DEPARTAMENTOS: tuple[str, ...] = (
    "Lima",
    "Arequipa",
    "La Libertad",
    "Piura",
    "Lambayeque",
    "Cusco",
    "Junin",
    "Ica",
    "Otro",
)

#: Internet fijo. Dos escalones, para que el cambio de velocidad sea un escenario posible.
#: Los paquetes con televisión y telefonía quedan fuera a propósito: aquí se facturan en su
#: propia línea.
PLANES_HOGAR: tuple[tuple[str, Centimos], ...] = (
    ("Internet Hogar 100Mb", 8990),
    ("Internet Hogar 200Mb", 10990),
)

#: Televisión suelta, una sola oferta.
PLANES_TV: tuple[tuple[str, Centimos], ...] = (("TV Hogar Sola", 6990),)

#: Paquetes convergentes Movistar Total.
PLANES_TOTAL: tuple[tuple[str, Centimos], ...] = (
    ("Movistar Total Basico", 14990),
    ("Movistar Total Plus", 18990),
    ("Movistar Total Max", 22990),
)

#: Equipos financiables. Nombres sin dígitos, a propósito.
EQUIPOS: tuple[tuple[str, Centimos], ...] = (
    ("Samsung Galaxy serie A", 79900),
    ("Samsung Galaxy serie S", 249900),
    ("Xiaomi Redmi Note", 69900),
    ("Motorola Moto G", 59900),
    ("Honor serie X", 64900),
    ("iPhone estándar", 329900),
)

#: Paquetes de compra puntual: (concepto_id, nombre, importe). El roaming es la oferta
#: Bolsas de datos. No tienen equivalente en el catálogo del propio dataset, así que el
#: catálogo —solo cubre altas comerciales, no recargas—, así que conservan un nombre
#: genérico y sin números; no son un plan, son una compra suelta dentro del ciclo.
PAQUETES_UNICOS: tuple[tuple[str, str, Centimos], ...] = (
    ("PAQUETE_DATOS_ADICIONAL", "Paquete de datos Ligero", 990),
    ("PAQUETE_DATOS_ADICIONAL", "Paquete de datos Plus", 1490),
    ("PAQUETE_DATOS_ADICIONAL", "Paquete de datos Full", 1990),
    ("PAQUETE_ROAMING", "Paquete Roaming Internacional", 2990),
)

#: Paquetes recurrentes que se dan de alta a mitad de ciclo.
PAQUETES_RECURRENTES: tuple[tuple[str, str, Centimos], ...] = (
    ("PAQUETE_TV_PREMIUM", "Paquete Streaming Video", 1990),
    ("SERVICIO_ADICIONAL_SEGURO", "Paquete Seguridad Digital", 1290),
)

#: Conceptos de consumo fuera de plan: (concepto_id, mínimo, máximo, paso).
CONSUMOS_POSIBLES: tuple[tuple[str, int, int, int], ...] = (
    ("LLAMADAS_FUERA_DE_PLAN", 350, 1500, 10),
    ("CONSUMO_DATOS_ADICIONAL", 500, 2000, 10),
    ("SMS_PREMIUM", 200, 600, 10),
    ("LARGA_DISTANCIA", 400, 1800, 10),
)

#: Promociones de descuento con nombre sin dígitos.
PROMOCIONES: tuple[tuple[str, str], ...] = (
    ("PROMO_BIENVENIDA", "Descuento de bienvenida"),
    ("PROMO_FIDELIDAD", "Descuento por permanencia"),
    ("PROMO_PORTABILIDAD", "Descuento por portabilidad"),
    ("PROMO_VERANO", "Descuento de campaña"),
)

#: Motivos habituales de una nota de crédito o de débito.
MOTIVOS_NOTA: tuple[tuple[str, str], ...] = (
    ("NOTA_CREDITO", "Devolución por cobro duplicado"),
    ("NOTA_CREDITO", "Compensación por avería reportada"),
    ("NOTA_CREDITO", "Ajuste de facturación reclamado"),
    ("NOTA_DEBITO", "Regularización de un cargo no facturado"),
    ("NOTA_DEBITO", "Cobro pendiente de un periodo anterior"),
)

#: Orden canónico de las líneas dentro del recibo (así se numeran los ``linea_id``).
ORDEN_CONCEPTOS: tuple[str, ...] = (
    "RENTA_MOVISTAR_TOTAL",
    "RENTA_PLAN_MOVIL",
    "RENTA_HOGAR_INTERNET",
    "RENTA_TV",
    "RENTA_LINEA_FIJA",
    "PAQUETE_TV_PREMIUM",
    "SERVICIO_ADICIONAL_SEGURO",
    "ALQUILER_EQUIPO_HOGAR",
    "PRORRATEO_PLAN",
    "AJUSTE_RETROACTIVO_RENTA",
    "AJUSTE_DIAS_SUSPENSION",
    "CARGO_RECONEXION",
    "INSTALACION_HOGAR",
    "CARGO_TRASLADO",
    "PAQUETE_DATOS_ADICIONAL",
    "PAQUETE_ROAMING",
    "LLAMADAS_FUERA_DE_PLAN",
    "CONSUMO_DATOS_ADICIONAL",
    "SMS_PREMIUM",
    "LARGA_DISTANCIA",
    "CUOTA_EQUIPO_FINANCIADO",
    "INTERES_FINANCIAMIENTO",
    "DESCUENTO_PROMOCIONAL",
    "DESCUENTO_MOVISTAR_TOTAL",
    "DESCUENTO_EQUIPO",
    "NOTA_DEBITO",
    "NOTA_CREDITO",
    "INTERES_MORATORIO",
    "IGV",
    "REDONDEO",
)


def orden_de_concepto(concepto_id: str) -> tuple[int, str]:
    """Clave de ordenación de una línea dentro del recibo.

    Los conceptos conocidos siguen ``ORDEN_CONCEPTOS``; los desconocidos van al final
    en orden alfabético, de modo que el orden es total y determinista.
    """
    try:
        return (ORDEN_CONCEPTOS.index(concepto_id), concepto_id)
    except ValueError:
        return (len(ORDEN_CONCEPTOS), concepto_id)


# --------------------------------------------------------------------------- #
# Calendario de ciclos
# --------------------------------------------------------------------------- #
def desplazar_periodo(periodo: str, meses: int) -> str:
    """Desplaza un periodo ``YYYY-MM`` un número (con signo) de meses."""
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    total = anio * 12 + (mes - 1) + meses
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def inicio_de_ciclo(periodo: str, dia_ciclo: int) -> date:
    """Primer día del ciclo de facturación de un periodo.

    ``dia_ciclo`` se limita al último día real del mes para que sea válido en febrero.
    """
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    return date(anio, mes, min(dia_ciclo, calendar.monthrange(anio, mes)[1]))


# --------------------------------------------------------------------------- #
# Perfil del cliente y ciclo de facturación
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ServicioBase:
    """Servicio contratado que genera una renta recurrente estable."""

    concepto_id: str
    nombre_comercial: str
    plan: str
    tarifa_cent: Centimos
    servicio_id: str


@dataclass(slots=True)
class DescuentoBase:
    """Descuento promocional vigente en el recibo base (importe positivo)."""

    promocion_id: str
    nombre: str
    monto_cent: Centimos
    concepto_id: str = "DESCUENTO_PROMOCIONAL"
    meses_vigencia: int | None = None


@dataclass(slots=True)
class FinanciamientoBase:
    """Equipo financiado que ya venía pagándose antes del primer recibo del historial.

    Su cuota es **constante** en los seis periodos (tasa cero y capital múltiplo del
    número de cuotas), así que su delta es siempre 0: es el *distractor* de la
    atribución ingenua, que tendería a culpar a la línea más grande del recibo.
    """

    equipo: str
    cuota_cent: Centimos
    cuotas_totales: int
    cuota_en_actual: int
    movimiento_id: int
    fecha_alta: date

    @property
    def principal_cent(self) -> Centimos:
        """Capital financiado (cuota constante por número de cuotas)."""
        return self.cuota_cent * self.cuotas_totales


@dataclass(slots=True)
class PerfilCliente:
    """Perfil estable de un cliente sintético.

    Todo lo que hay aquí se repite idéntico en los seis periodos del historial: el
    recibo base **no tiene ruido**. Así, cualquier variación entre el recibo actual y
    el previo procede de un escenario inyectado y tiene ground truth conocido.

    Los cuatro campos de perfil comercial (``tipo_cliente``, ``edad_rango``,
    ``ubicacion_departamento``, ``canal_mas_usado``) son los del dataset real de
    Movistar y **no intervienen en ningún cálculo**: no mueven un céntimo del recibo.
    Están para personalizar el trato y para que un export real encaje sin traducir.
    """

    cuenta_id: str
    seed: int
    segmento: str
    modalidad_renta: ModalidadRenta
    dia_ciclo: int
    servicios: list[ServicioBase]
    consumos: dict[str, Centimos] = field(default_factory=dict)
    descuento: DescuentoBase | None = None
    financiamiento: FinanciamientoBase | None = None
    beneficios: list[str] = field(default_factory=list)
    tipo_cliente: str = "postpago"
    edad_rango: str = "26-35"
    ubicacion_departamento: str = "Lima"
    canal_mas_usado: str = "Digital"
    opciones: dict[str, dict[str, Any]] = field(default_factory=dict)
    base_movimiento_id: int = 10_000_000
    contador_movimiento: int = 0

    @property
    def servicio_principal(self) -> ServicioBase:
        """Servicio sobre el que actúan los escenarios de renta."""
        return self.servicios[0]

    def perfil_comercial(self) -> dict[str, str]:
        """Perfil comercial del cliente con los nombres de campo del dataset real.

        Se publica tal cual —mismas claves, mismos valores— para que el consumidor no
        tenga que traducir nada cuando la fuente deje de ser sintética.
        """
        return {
            "tipo_cliente": self.tipo_cliente,
            "edad_rango": self.edad_rango,
            "ubicacion_departamento": self.ubicacion_departamento,
            "canal_mas_usado": self.canal_mas_usado,
        }

    def siguiente_movimiento_id(self) -> int:
        """Identificador de orden único y determinista dentro del cliente."""
        self.contador_movimiento += 1
        return self.base_movimiento_id + self.contador_movimiento

    def opcion(self, escenario: str) -> dict[str, Any]:
        """Diccionario de opciones que ``preparar()`` dejó para ``aplicar()``."""
        return self.opciones.setdefault(escenario, {})


@dataclass(slots=True)
class CicloFacturacion:
    """Ciclo de facturación de un periodo, con el estado mutable del recibo en curso.

    Los rangos son ``[inicio, fin)`` con ``fin`` exclusivo, como en ``Tramo``. Un
    escenario puede modificar el estado del recibo (deuda arrastrada, estado del
    servicio, plan vigente) y retirar conceptos de la base a través de este objeto:
    la tupla que devuelve ``aplicar`` solo contiene líneas **nuevas o sustitutas**.
    """

    periodo: str
    inicio: date
    fin: date
    dias: int
    dias_efectivos: int
    indice: int
    modalidad_renta: ModalidadRenta
    periodo_siguiente: str
    inicio_siguiente: date
    fin_siguiente: date
    dias_siguiente: int
    dias_siguiente_efectivos: int
    total_previo_cent: Centimos = 0
    deuda_previa_cent: Centimos = 0
    deuda_anterior_cent: Centimos = 0
    estado_servicio: EstadoServicio = EstadoServicio.ACTIVO
    plan_vigente: str | None = None
    conceptos_retirados: set[str] = field(default_factory=set)
    notas: dict[str, Any] = field(default_factory=dict)

    @property
    def es_actual(self) -> bool:
        """Verdadero solo en M0, el único periodo en el que se inyectan escenarios."""
        return self.indice == 0

    @property
    def fecha_emision(self) -> date:
        """El recibo se emite el día en que cierra el ciclo."""
        return self.fin

    @property
    def fecha_vencimiento(self) -> date:
        """Vencimiento del recibo: doce días después de la emisión. **[SUPUESTO]**"""
        return self.fin + timedelta(days=12)

    def retirar_base(self, concepto_id: str) -> None:
        """Quita una línea del recibo base (p. ej. un descuento que dejó de aplicarse)."""
        self.conceptos_retirados.add(concepto_id)

    def dia(self, offset: int) -> date:
        """Fecha a ``offset`` días del inicio del ciclo, acotada dentro del ciclo."""
        return self.inicio + timedelta(days=max(0, min(offset, self.dias)))


def construir_ciclo(
    periodo: str,
    indice: int,
    perfil: PerfilCliente,
    reglas: ConfiguracionReglas,
) -> CicloFacturacion:
    """Construye el ciclo de un periodo a partir del día de facturación del cliente."""
    inicio = inicio_de_ciclo(periodo, perfil.dia_ciclo)
    periodo_siguiente = desplazar_periodo(periodo, 1)
    fin = inicio_de_ciclo(periodo_siguiente, perfil.dia_ciclo)
    fin_siguiente = inicio_de_ciclo(desplazar_periodo(periodo, 2), perfil.dia_ciclo)
    dias = (fin - inicio).days
    dias_siguiente = (fin_siguiente - fin).days
    return CicloFacturacion(
        periodo=periodo,
        inicio=inicio,
        fin=fin,
        dias=dias,
        dias_efectivos=reglas.dias_ciclo_efectivos(dias),
        indice=indice,
        modalidad_renta=perfil.modalidad_renta,
        periodo_siguiente=periodo_siguiente,
        inicio_siguiente=fin,
        fin_siguiente=fin_siguiente,
        dias_siguiente=dias_siguiente,
        dias_siguiente_efectivos=reglas.dias_ciclo_efectivos(dias_siguiente),
        plan_vigente=perfil.servicio_principal.plan,
    )


# --------------------------------------------------------------------------- #
# Construcción de líneas
# --------------------------------------------------------------------------- #
def construir_linea(
    *,
    concepto_id: str,
    monto_cent: Centimos,
    periodo_imputado: str,
    reglas: ConfiguracionReglas,
    nombre_comercial: str | None = None,
    descripcion: str | None = None,
    servicio_id: str | None = None,
    cantidad: int = 1,
    dias_prorrateo: int | None = None,
    fecha_inicio: date | None = None,
    fecha_fin: date | None = None,
    cuota_numero: int | None = None,
    cuotas_totales: int | None = None,
    movimiento_id: int | None = None,
    tramos: list[Tramo] | None = None,
    meta: dict[str, Any] | None = None,
) -> LineaRecibo:
    """Crea una ``LineaRecibo`` tomando familia, nombre y afectación de IGV del catálogo.

    El ``linea_id`` sale en 0: lo renumera el ensamblador del recibo una vez conocido
    el orden definitivo de las líneas.

    Raises:
        KeyError: si el concepto no está en el catálogo de ``rules.yaml``. Un concepto
            fuera de catálogo es una regla dura de derivación: el generador no puede
            fabricarlo por su cuenta.
    """
    concepto = reglas.concepto(concepto_id)
    if concepto is None:
        raise KeyError(f"concepto fuera del catálogo de rules.yaml: {concepto_id}")
    return LineaRecibo(
        linea_id=0,
        concepto_id=concepto_id,
        nombre_comercial=nombre_comercial or concepto.nombre_comercial,
        familia=concepto.familia,
        monto_cent=int(monto_cent),
        periodo=periodo_imputado,
        servicio_id=servicio_id,
        descripcion=descripcion,
        cantidad=cantidad,
        afecto_igv=concepto.afecto_igv,
        dias_prorrateo=dias_prorrateo,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        cuota_numero=cuota_numero,
        cuotas_totales=cuotas_totales,
        movimiento_id=movimiento_id,
        tramos=tramos or [],
        meta=meta or {},
    )


def _tramo_completo(
    inicio: date,
    fin: date,
    tarifa_mensual_cent: Centimos,
    concepto_id: str,
    plan: str | None = None,
) -> Tramo:
    """Tramo que cubre un ciclo entero: el importe es la tarifa, sin dividir.

    No se usa ``Tramo.crear`` a propósito. Un mes completo se cobra completo en
    cualquier convención de días; hacerlo pasar por el prorrateo introduciría un
    artefacto cuando la convención configurada es ``30_360`` y el mes real tiene
    treinta y un días. La estabilidad del recibo base depende de esto.
    """
    return Tramo(
        inicio=inicio,
        fin=fin,
        dias=(fin - inicio).days,
        tarifa_mensual_cent=tarifa_mensual_cent,
        estado=EstadoServicio.ACTIVO,
        facturable=True,
        monto_prorrateado_cent=tarifa_mensual_cent,
        etiqueta=etiqueta_rango_fechas(inicio, fin),
        concepto_id=concepto_id,
        plan=plan,
    )


def _ventana_renta(ciclo: CicloFacturacion) -> tuple[str, date, date, int, int]:
    """Ventana que factura la renta según la modalidad.

    En renta vencida es el propio ciclo; en renta adelantada es el ciclo siguiente,
    porque el recibo cobra por anticipado. Devuelve ``(periodo_imputado, inicio, fin,
    dias, dias_efectivos)``.
    """
    if ciclo.modalidad_renta is ModalidadRenta.ADELANTADA:
        return (
            ciclo.periodo_siguiente,
            ciclo.inicio_siguiente,
            ciclo.fin_siguiente,
            ciclo.dias_siguiente,
            ciclo.dias_siguiente_efectivos,
        )
    return (ciclo.periodo, ciclo.inicio, ciclo.fin, ciclo.dias, ciclo.dias_efectivos)


def lineas_base(
    perfil: PerfilCliente,
    ciclo: CicloFacturacion,
    reglas: ConfiguracionReglas,
) -> list[LineaRecibo]:
    """Recibo base del cliente: idéntico en importe en los seis periodos.

    Es la línea de flotación del dataset. Como no varía, cualquier delta entre el
    recibo actual y el previo procede exclusivamente de un escenario inyectado, y el
    ground truth es exacto por construcción (no aproximado).
    """
    periodo_renta, inicio_renta, fin_renta, dias_renta, _dias_efectivos = _ventana_renta(ciclo)
    lineas: list[LineaRecibo] = []

    for servicio in perfil.servicios:
        tramo = _tramo_completo(
            inicio_renta,
            fin_renta,
            servicio.tarifa_cent,
            servicio.concepto_id,
            servicio.plan,
        )
        lineas.append(
            construir_linea(
                concepto_id=servicio.concepto_id,
                monto_cent=tramo.monto_prorrateado_cent,
                periodo_imputado=periodo_renta,
                reglas=reglas,
                nombre_comercial=servicio.nombre_comercial,
                descripcion=f"{servicio.plan}, {etiqueta_rango_fechas(inicio_renta, fin_renta)}",
                servicio_id=servicio.servicio_id,
                dias_prorrateo=dias_renta,
                fecha_inicio=inicio_renta,
                fecha_fin=fin_renta,
                tramos=[tramo],
            )
        )

    if perfil.descuento is not None:
        descuento = perfil.descuento
        tramo = _tramo_completo(
            inicio_renta,
            fin_renta,
            -descuento.monto_cent,
            descuento.concepto_id,
            perfil.servicio_principal.plan,
        )
        lineas.append(
            construir_linea(
                concepto_id=descuento.concepto_id,
                monto_cent=tramo.monto_prorrateado_cent,
                periodo_imputado=periodo_renta,
                reglas=reglas,
                nombre_comercial=descuento.nombre,
                descripcion=f"{descuento.nombre} vigente",
                servicio_id=perfil.servicio_principal.servicio_id,
                dias_prorrateo=dias_renta,
                fecha_inicio=inicio_renta,
                fecha_fin=fin_renta,
                tramos=[tramo],
                meta={"promocion_id": descuento.promocion_id},
            )
        )

    if perfil.financiamiento is not None:
        fin_eq = perfil.financiamiento
        numero = fin_eq.cuota_en_actual + ciclo.indice
        lineas.append(
            construir_linea(
                concepto_id="CUOTA_EQUIPO_FINANCIADO",
                monto_cent=fin_eq.cuota_cent,
                periodo_imputado=ciclo.periodo,
                reglas=reglas,
                descripcion=f"{fin_eq.equipo}, cuota {numero} de {fin_eq.cuotas_totales}",
                servicio_id=perfil.servicio_principal.servicio_id,
                cuota_numero=numero,
                cuotas_totales=fin_eq.cuotas_totales,
                movimiento_id=fin_eq.movimiento_id,
                meta={
                    "equipo": fin_eq.equipo,
                    "principal_cent": fin_eq.principal_cent,
                    "cuotas_totales": fin_eq.cuotas_totales,
                    "tasa_mensual_bp": 0,
                    "distractor": True,
                },
            )
        )

    for concepto_id, monto in perfil.consumos.items():
        lineas.append(
            construir_linea(
                concepto_id=concepto_id,
                monto_cent=monto,
                periodo_imputado=ciclo.periodo,
                reglas=reglas,
                servicio_id=perfil.servicio_principal.servicio_id,
                fecha_inicio=ciclo.inicio,
                fecha_fin=ciclo.fin,
            )
        )

    return [linea for linea in lineas if linea.concepto_id not in ciclo.conceptos_retirados]


# --------------------------------------------------------------------------- #
# Financiamiento de equipos (sistema francés, aritmética entera)
# --------------------------------------------------------------------------- #
def construir_plan_financiamiento(
    equipo: str,
    principal_cent: Centimos,
    cuotas_totales: int,
    tasa_mensual_bp: int = 0,
    movimiento_id: int | None = None,
) -> PlanFinanciamiento:
    """Cronograma francés completo en céntimos enteros.

    ``A = K·i / (1 − (1+i)^(−n))``; con ``i == 0`` el capital se reparte por mayor
    resto, de modo que la suma de cuotas es exactamente ``K``. La última cuota absorbe
    el residuo (``A_n = B_{n−1}·(1+i)``), así que el saldo final cierra en cero y la
    suma de amortizaciones es idéntica al principal.

    La aritmética intermedia usa ``Fraction`` (exacta) y sale siempre con ``int``.

    Raises:
        ValueError: si la cuota resultante no amortiza capital (tasa demasiado alta
            para el plazo pedido), lo que produciría un cronograma que nunca cierra.
    """
    if cuotas_totales <= 0:
        raise ValueError(f"cuotas_totales debe ser positivo: {cuotas_totales}")
    if principal_cent <= 0:
        raise ValueError(f"principal_cent debe ser positivo: {principal_cent}")

    cronograma: list[CuotaFinanciamiento] = []

    if tasa_mensual_bp == 0:
        partes = repartir_mayor_resto(principal_cent, [1] * cuotas_totales)
        saldo = principal_cent
        for numero, parte in enumerate(partes, start=1):
            saldo_inicial = saldo
            saldo -= parte
            cronograma.append(
                CuotaFinanciamiento(
                    numero=numero,
                    de_total=cuotas_totales,
                    monto_cent=parte,
                    interes_cent=0,
                    amortizacion_cent=parte,
                    saldo_inicial_cent=saldo_inicial,
                    saldo_final_cent=saldo,
                )
            )
        return PlanFinanciamiento(
            equipo=equipo,
            principal_cent=principal_cent,
            cuotas_totales=cuotas_totales,
            tasa_mensual_bp=0,
            cronograma=cronograma,
            movimiento_id=movimiento_id,
        )

    tasa = Fraction(tasa_mensual_bp, 10_000)
    factor = (1 + tasa) ** cuotas_totales
    cuota_exacta = Fraction(principal_cent) * tasa * factor / (factor - 1)
    cuota = redondear_banca(cuota_exacta.numerator, cuota_exacta.denominator)

    saldo = principal_cent
    for numero in range(1, cuotas_totales):
        interes = aplicar_porcentaje(saldo, tasa_mensual_bp)
        amortizacion = cuota - interes
        if amortizacion <= 0:
            raise ValueError(
                f"la cuota ({cuota}) no amortiza capital con tasa {tasa_mensual_bp} bp "
                f"y {cuotas_totales} cuotas"
            )
        saldo_inicial = saldo
        saldo -= amortizacion
        cronograma.append(
            CuotaFinanciamiento(
                numero=numero,
                de_total=cuotas_totales,
                monto_cent=cuota,
                interes_cent=interes,
                amortizacion_cent=amortizacion,
                saldo_inicial_cent=saldo_inicial,
                saldo_final_cent=saldo,
            )
        )

    interes_final = aplicar_porcentaje(saldo, tasa_mensual_bp)
    cronograma.append(
        CuotaFinanciamiento(
            numero=cuotas_totales,
            de_total=cuotas_totales,
            monto_cent=saldo + interes_final,
            interes_cent=interes_final,
            amortizacion_cent=saldo,
            saldo_inicial_cent=saldo,
            saldo_final_cent=0,
        )
    )
    return PlanFinanciamiento(
        equipo=equipo,
        principal_cent=principal_cent,
        cuotas_totales=cuotas_totales,
        tasa_mensual_bp=tasa_mensual_bp,
        cronograma=cronograma,
        movimiento_id=movimiento_id,
    )


# --------------------------------------------------------------------------- #
# Interfaz común de los escenarios
# --------------------------------------------------------------------------- #
class ResultadoEscenario(NamedTuple):
    """Lo que devuelve ``Escenario.aplicar``: una tupla de tres listas.

    ``lineas`` son líneas **nuevas o sustitutas**: si el ``concepto_id`` ya existe en
    el recibo base, lo reemplaza; si no, se añade. Las retiradas se declaran con
    ``ciclo.retirar_base()``.
    """

    lineas: list[LineaRecibo]
    movimientos: list[MovementEvent]
    ground_truth: list[GroundTruthCausaDelta]


def combinar(*partes: ResultadoEscenario) -> ResultadoEscenario:
    """Une los resultados de varios escenarios aplicados al mismo ciclo."""
    lineas: list[LineaRecibo] = []
    movimientos: list[MovementEvent] = []
    ground_truth: list[GroundTruthCausaDelta] = []
    for parte in partes:
        lineas.extend(parte.lineas)
        movimientos.extend(parte.movimientos)
        ground_truth.extend(parte.ground_truth)
    return ResultadoEscenario(lineas, movimientos, ground_truth)


class Escenario(ABC):
    """Escenario inyectable en el periodo actual (M0).

    Contrato:

    1. ``preparar`` ajusta el perfil **antes** de generar el historial, para garantizar
       las precondiciones del escenario (por ejemplo, que exista un descuento vigente
       si el escenario consiste en que ese descuento termine).
    2. ``aplicar`` fabrica las líneas, los movimientos de Amdocs y el ground truth en
       la misma pasada, con la misma aritmética.
    3. ``conceptos_que_toca`` declara qué conceptos puede modificar. Dos escenarios
       solo se combinan si sus conjuntos son disjuntos: así nunca se pisan una línea y
       el ground truth compuesto sigue siendo exacto.
    """

    nombre: ClassVar[str]
    descripcion: ClassVar[str] = ""
    conceptos_que_toca: ClassVar[frozenset[str]] = frozenset()
    causas_esperadas: ClassVar[tuple[TipoMovimiento, ...]] = ()
    combinable: ClassVar[bool] = True

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        """Garantiza las precondiciones del escenario sobre el perfil base."""
        return None

    @abstractmethod
    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        """Inyecta el escenario en el ciclo y devuelve líneas, movimientos y ground truth."""

    # ------------------------------------------------------------------ #
    # Utilidades compartidas
    # ------------------------------------------------------------------ #
    @staticmethod
    def _reglas() -> ConfiguracionReglas:
        """Reglas de negocio vigentes (objeto cacheado y compartido: no mutar)."""
        return cargar_reglas()

    def _movimiento(
        self,
        cliente: PerfilCliente,
        tipo: TipoMovimiento,
        fecha: date,
        detalle: dict[str, Any],
        rng: Random,
        servicio_id: str | None = None,
    ) -> MovementEvent:
        """Crea el evento de Amdocs que justifica la variación."""
        return MovementEvent(
            movimiento_id=cliente.siguiente_movimiento_id(),
            cuenta_id=cliente.cuenta_id,
            tipo=tipo,
            ocurrido_en=datetime.combine(fecha, HORA_MOVIMIENTO),
            detalle=detalle,
            canal=rng.choice(CANALES_ORDEN),
            servicio_id=servicio_id,
        )

    def _gt(
        self,
        cliente: PerfilCliente,
        ciclo: CicloFacturacion,
        concepto_id: str,
        causa: TipoMovimiento | None,
        delta_cent: Centimos,
        movimiento_id: int | None = None,
    ) -> GroundTruthCausaDelta:
        """Fila de ``gt_causa_delta`` etiquetada con este escenario."""
        return GroundTruthCausaDelta(
            cuenta_id=cliente.cuenta_id,
            periodo=ciclo.periodo,
            concepto_id=concepto_id,
            causa=causa,
            delta_cent=int(delta_cent),
            movimiento_id=movimiento_id,
            escenario=self.nombre,
        )


def _sin_ceros(filas: list[GroundTruthCausaDelta]) -> list[GroundTruthCausaDelta]:
    """Descarta las filas de delta nulo: un concepto IGUAL no se explica."""
    return [fila for fila in filas if fila.delta_cent != 0]


# --------------------------------------------------------------------------- #
# 1 · CAMBIO_PLAN_MEDIO_CICLO
# --------------------------------------------------------------------------- #
class CambioPlanMedioCiclo(Escenario):
    """Cambio de plan a mitad de ciclo. El escenario central del desafío.

    * **VENCIDA**: la renta del ciclo se parte en dos tramos y se cobra proporcional a
      los días de cada plan. La tabla de tramos *es* la explicación.
    * **ADELANTADA**: el recibo cobra el mes completo del plan nuevo (ciclo siguiente)
      y añade un ajuste retroactivo ``(P_nuevo − P_anterior)·d_nuevo/D`` por los días
      del ciclo que ya se habían cobrado con el plan anterior. Conviven dos rentas en
      el mismo documento.

    Dos variantes, ambas realistas y ambas dolorosas para el cliente:

    * ``SUBIDA`` — mejora de plan: el recibo sube **más** que la diferencia de tarifa,
      porque además del mes nuevo se cobra el ajuste del mes en curso.
    * ``BAJADA_PIERDE_DESCUENTO`` — el cliente se pasa a un plan de menor precio de
      lista pero el descuento promocional estaba atado al plan anterior y se pierde.
      **El recibo sube aunque el plan nuevo sea más barato**, que es exactamente el
      caso que la especificación exige demostrar.

      Esa variante emite **dos** movimientos, no uno: el ``CAMBIO_PLAN`` y el
      ``FIN_DESCUENTO`` que el cambio dispara. Cada delta lleva entonces su causa real
      —la renta y el ajuste retroactivo son ``CAMBIO_PLAN``, la pérdida del descuento es
      ``FIN_DESCUENTO``— y el ground truth deja de decir que el recibo subió *por el
      cambio de plan*, cuando el cambio de plan por sí solo lo bajó. Etiquetar todos los
      deltas de un escenario con su causa principal era el defecto que hacía que
      ``precision_causa_raiz`` diera 100 % sobre una verdad equivocada.
    """

    nombre = "CAMBIO_PLAN_MEDIO_CICLO"
    descripcion = "Cambio de plan a mitad de ciclo, con prorrateo o ajuste retroactivo"
    conceptos_que_toca = frozenset(
        {
            "RENTA_PLAN_MOVIL",
            "RENTA_HOGAR_INTERNET",
            "RENTA_TV",
            "RENTA_LINEA_FIJA",
            "RENTA_MOVISTAR_TOTAL",
            "AJUSTE_RETROACTIVO_RENTA",
            "PRORRATEO_PLAN",
            "DESCUENTO_PROMOCIONAL",
        }
    )
    # Dos causas, no una: la variante BAJADA_PIERDE_DESCUENTO añade el FIN_DESCUENTO que
    # el propio cambio de plan dispara sobre la promoción atada al plan anterior.
    causas_esperadas = (TipoMovimiento.CAMBIO_PLAN, TipoMovimiento.FIN_DESCUENTO)

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        servicio = cliente.servicio_principal
        catalogo = _catalogo_de_planes(servicio.concepto_id)
        opciones = cliente.opcion(self.nombre)

        pierde_descuento = rng.random() < 0.40
        candidatos_caros = [par for par in catalogo if par[1] > servicio.tarifa_cent]
        candidatos_baratos = [par for par in catalogo if par[1] < servicio.tarifa_cent]

        if pierde_descuento and candidatos_baratos:
            plan_nuevo, tarifa_nueva = rng.choice(candidatos_baratos)
            variante = "BAJADA_PIERDE_DESCUENTO"
        elif candidatos_caros:
            plan_nuevo, tarifa_nueva = rng.choice(candidatos_caros)
            variante = "SUBIDA"
        else:
            plan_nuevo, tarifa_nueva = rng.choice(candidatos_baratos or catalogo)
            variante = "BAJADA_PIERDE_DESCUENTO"

        opciones["variante"] = variante
        opciones["plan_nuevo"] = plan_nuevo
        opciones["tarifa_nueva_cent"] = tarifa_nueva
        opciones["dia_cambio"] = rng.randint(6, 22)

        if variante == "BAJADA_PIERDE_DESCUENTO":
            # El descuento perdido debe superar el ahorro del plan para que el recibo
            # suba: el ajuste retroactivo nunca duplica la diferencia de tarifa.
            ahorro = servicio.tarifa_cent - tarifa_nueva
            minimo = ahorro * 2 + rng.choice((500, 700, 1000))
            promocion_id, nombre = rng.choice(PROMOCIONES)
            if cliente.descuento is None or cliente.descuento.monto_cent < minimo:
                cliente.descuento = DescuentoBase(
                    promocion_id=promocion_id,
                    nombre=nombre,
                    monto_cent=minimo,
                )

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        servicio = cliente.servicio_principal
        opciones = cliente.opcion(self.nombre)
        plan_nuevo: str = opciones["plan_nuevo"]
        tarifa_nueva: int = opciones["tarifa_nueva_cent"]
        tarifa_anterior = servicio.tarifa_cent
        dia_cambio = max(3, min(int(opciones["dia_cambio"]), ciclo.dias - 3))
        fecha_cambio = ciclo.inicio + timedelta(days=dia_cambio)

        movimiento = self._movimiento(
            cliente,
            TipoMovimiento.CAMBIO_PLAN,
            fecha_cambio,
            DetalleCambioPlan(
                plan_anterior=servicio.plan,
                plan_nuevo=plan_nuevo,
                tarifa_anterior_cent=tarifa_anterior,
                tarifa_nueva_cent=tarifa_nueva,
                servicio_id=servicio.servicio_id,
            ).model_dump(mode="json"),
            rng,
            servicio_id=servicio.servicio_id,
        )
        ciclo.plan_vigente = plan_nuevo
        ciclo.notas["cambio_plan"] = {
            "plan_anterior": servicio.plan,
            "plan_nuevo": plan_nuevo,
            "fecha": fecha_cambio.isoformat(),
        }

        lineas: list[LineaRecibo] = []
        filas: list[GroundTruthCausaDelta] = []

        if ciclo.modalidad_renta is ModalidadRenta.VENCIDA:
            tramo_anterior = Tramo.crear(
                inicio=ciclo.inicio,
                fin=fecha_cambio,
                tarifa_mensual_cent=tarifa_anterior,
                dias_ciclo=ciclo.dias_efectivos,
                concepto_id=servicio.concepto_id,
                plan=servicio.plan,
            )
            tramo_nuevo = Tramo.crear(
                inicio=fecha_cambio,
                fin=ciclo.fin,
                tarifa_mensual_cent=tarifa_nueva,
                dias_ciclo=ciclo.dias_efectivos,
                concepto_id=servicio.concepto_id,
                plan=plan_nuevo,
            )
            monto = tramo_anterior.monto_prorrateado_cent + tramo_nuevo.monto_prorrateado_cent
            lineas.append(
                construir_linea(
                    concepto_id=servicio.concepto_id,
                    monto_cent=monto,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    nombre_comercial=servicio.nombre_comercial,
                    descripcion=(
                        f"{servicio.plan} {tramo_anterior.etiqueta} y "
                        f"{plan_nuevo} {tramo_nuevo.etiqueta}"
                    ),
                    servicio_id=servicio.servicio_id,
                    dias_prorrateo=ciclo.dias,
                    fecha_inicio=ciclo.inicio,
                    fecha_fin=ciclo.fin,
                    movimiento_id=movimiento.movimiento_id,
                    tramos=[tramo_anterior, tramo_nuevo],
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    servicio.concepto_id,
                    TipoMovimiento.CAMBIO_PLAN,
                    monto - tarifa_anterior,
                    movimiento.movimiento_id,
                )
            )
        else:
            dias_con_plan_nuevo = ciclo.dias - dia_cambio
            tramo_siguiente = _tramo_completo(
                ciclo.inicio_siguiente,
                ciclo.fin_siguiente,
                tarifa_nueva,
                servicio.concepto_id,
                plan_nuevo,
            )
            lineas.append(
                construir_linea(
                    concepto_id=servicio.concepto_id,
                    monto_cent=tramo_siguiente.monto_prorrateado_cent,
                    periodo_imputado=ciclo.periodo_siguiente,
                    reglas=reglas,
                    nombre_comercial=servicio.nombre_comercial,
                    descripcion=f"{plan_nuevo}, {tramo_siguiente.etiqueta}",
                    servicio_id=servicio.servicio_id,
                    dias_prorrateo=ciclo.dias_siguiente,
                    fecha_inicio=ciclo.inicio_siguiente,
                    fecha_fin=ciclo.fin_siguiente,
                    movimiento_id=movimiento.movimiento_id,
                    tramos=[tramo_siguiente],
                )
            )
            ajuste = prorratear(
                tarifa_nueva - tarifa_anterior, dias_con_plan_nuevo, ciclo.dias_efectivos
            )
            tramo_ajuste = Tramo(
                inicio=fecha_cambio,
                fin=ciclo.fin,
                dias=dias_con_plan_nuevo,
                tarifa_mensual_cent=tarifa_nueva - tarifa_anterior,
                estado=EstadoServicio.ACTIVO,
                facturable=True,
                monto_prorrateado_cent=ajuste,
                etiqueta=etiqueta_rango_fechas(fecha_cambio, ciclo.fin),
                concepto_id="AJUSTE_RETROACTIVO_RENTA",
                plan=plan_nuevo,
            )
            lineas.append(
                construir_linea(
                    concepto_id="AJUSTE_RETROACTIVO_RENTA",
                    monto_cent=ajuste,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    descripcion=(
                        f"Diferencia entre {servicio.plan} y {plan_nuevo} {tramo_ajuste.etiqueta}"
                    ),
                    servicio_id=servicio.servicio_id,
                    dias_prorrateo=dias_con_plan_nuevo,
                    fecha_inicio=fecha_cambio,
                    fecha_fin=ciclo.fin,
                    movimiento_id=movimiento.movimiento_id,
                    tramos=[tramo_ajuste],
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    servicio.concepto_id,
                    TipoMovimiento.CAMBIO_PLAN,
                    tarifa_nueva - tarifa_anterior,
                    movimiento.movimiento_id,
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    "AJUSTE_RETROACTIVO_RENTA",
                    TipoMovimiento.CAMBIO_PLAN,
                    ajuste,
                    movimiento.movimiento_id,
                )
            )

        movimientos = [movimiento]

        if opciones.get("variante") == "BAJADA_PIERDE_DESCUENTO" and cliente.descuento:
            descuento = cliente.descuento
            ciclo.retirar_base(descuento.concepto_id)
            # El cambio de plan CANCELA la promoción: son dos hechos distintos y el
            # historial de Amdocs los registra por separado. El evento se construye a
            # mano (y no con `self._movimiento`) para reutilizar el canal del cambio de
            # plan: es la misma gestión, y así el generador no consume una tirada extra
            # del generador aleatorio, que desplazaría el dataset entero.
            fin_descuento = MovementEvent(
                movimiento_id=cliente.siguiente_movimiento_id(),
                cuenta_id=cliente.cuenta_id,
                tipo=TipoMovimiento.FIN_DESCUENTO,
                ocurrido_en=datetime.combine(fecha_cambio, HORA_MOVIMIENTO),
                detalle=DetalleFinDescuento(
                    promocion_id=descuento.promocion_id,
                    nombre=descuento.nombre,
                    descuento_cent=descuento.monto_cent,
                    meses_vigencia=descuento.meses_vigencia,
                    motivo="CAMBIO_PLAN",
                    plan_asociado=servicio.plan,
                    movimiento_origen=movimiento.movimiento_id,
                ).model_dump(mode="json"),
                canal=movimiento.canal,
                servicio_id=servicio.servicio_id,
            )
            movimientos.append(fin_descuento)
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    descuento.concepto_id,
                    TipoMovimiento.FIN_DESCUENTO,
                    descuento.monto_cent,
                    fin_descuento.movimiento_id,
                )
            )
            ciclo.notas["descuento_perdido"] = descuento.nombre
            ciclo.notas["fin_descuento"] = {
                "promocion_id": descuento.promocion_id,
                "nombre": descuento.nombre,
                "fecha": fecha_cambio.isoformat(),
                "motivo": "CAMBIO_PLAN",
            }

        return ResultadoEscenario(lineas, movimientos, _sin_ceros(filas))


def _catalogo_de_planes(concepto_id: str) -> tuple[tuple[str, Centimos], ...]:
    """Catálogo de planes alternativos válido para un concepto de renta."""
    return {
        "RENTA_PLAN_MOVIL": PLANES_MOVIL,
        "RENTA_HOGAR_INTERNET": PLANES_HOGAR,
        "RENTA_TV": PLANES_TV,
        "RENTA_MOVISTAR_TOTAL": PLANES_TOTAL,
    }.get(concepto_id, PLANES_MOVIL)


# --------------------------------------------------------------------------- #
# 2 · CUOTA_EQUIPO_FINANCIADO
# --------------------------------------------------------------------------- #
class CuotaEquipoFinanciado(Escenario):
    """Alta de un equipo en cuotas dentro del ciclo: aparece la primera cuota.

    La cuota **nunca se prorratea**: se cobra completa desde el primer recibo, aunque
    el equipo se haya comprado el último día del ciclo. Es una de las variaciones que
    más consultas genera, porque el cliente no relaciona la compra del equipo con el
    recibo del servicio.

    Con tasa cero se emite una sola línea con la cuota. Con tasa positiva se emiten dos
    (amortización e interés), que es como lo desglosa el facturador; ambas comparten el
    par "cuota N de M" y suman exactamente la cuota francesa.
    """

    nombre = "CUOTA_EQUIPO_FINANCIADO"
    descripcion = "Alta de equipo financiado: aparece la primera cuota, sin prorrateo"
    conceptos_que_toca = frozenset({"CUOTA_EQUIPO_FINANCIADO", "INTERES_FINANCIAMIENTO"})
    causas_esperadas = (TipoMovimiento.ALTA_EQUIPO_FINANCIADO,)

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        # El equipo nuevo ocupa el concepto CUOTA_EQUIPO_FINANCIADO: si el perfil traía
        # un financiamiento previo se retira, para que no haya dos cuotas en la misma
        # línea del recibo.
        cliente.financiamiento = None
        equipo, principal = rng.choice(EQUIPOS)
        opciones = cliente.opcion(self.nombre)
        opciones["equipo"] = equipo
        opciones["principal_cent"] = principal
        opciones["cuotas_totales"] = rng.choice((6, 12, 18, 24))
        opciones["tasa_mensual_bp"] = rng.choice((0, 0, 0, 120, 150))
        opciones["dia_alta"] = rng.randint(2, 24)

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        opciones = cliente.opcion(self.nombre)
        equipo: str = opciones["equipo"]
        principal: int = opciones["principal_cent"]
        cuotas_totales: int = opciones["cuotas_totales"]
        tasa_bp: int = opciones["tasa_mensual_bp"]
        dia_alta = max(1, min(int(opciones["dia_alta"]), ciclo.dias - 1))
        fecha_alta = ciclo.inicio + timedelta(days=dia_alta)

        plan = construir_plan_financiamiento(equipo, principal, cuotas_totales, tasa_bp)
        primera = plan.cronograma[0]

        movimiento = self._movimiento(
            cliente,
            TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
            fecha_alta,
            DetalleAltaEquipoFinanciado(
                equipo=equipo,
                principal_cent=principal,
                cuotas_totales=cuotas_totales,
                tasa_mensual_bp=tasa_bp,
                cuota_cent=primera.monto_cent,
            ).model_dump(mode="json"),
            rng,
            servicio_id=cliente.servicio_principal.servicio_id,
        )

        meta = {
            "equipo": equipo,
            "principal_cent": principal,
            "cuotas_totales": cuotas_totales,
            "tasa_mensual_bp": tasa_bp,
            "saldo_final_cent": primera.saldo_final_cent,
            "fecha_alta": fecha_alta.isoformat(),
        }
        lineas: list[LineaRecibo] = []
        filas: list[GroundTruthCausaDelta] = []

        if tasa_bp == 0:
            lineas.append(
                construir_linea(
                    concepto_id="CUOTA_EQUIPO_FINANCIADO",
                    monto_cent=primera.monto_cent,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    descripcion=f"{equipo}, {primera.etiqueta}",
                    servicio_id=cliente.servicio_principal.servicio_id,
                    cuota_numero=primera.numero,
                    cuotas_totales=cuotas_totales,
                    movimiento_id=movimiento.movimiento_id,
                    meta=meta,
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    "CUOTA_EQUIPO_FINANCIADO",
                    TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
                    primera.monto_cent,
                    movimiento.movimiento_id,
                )
            )
        else:
            lineas.append(
                construir_linea(
                    concepto_id="CUOTA_EQUIPO_FINANCIADO",
                    monto_cent=primera.amortizacion_cent,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    descripcion=f"{equipo}, {primera.etiqueta}",
                    servicio_id=cliente.servicio_principal.servicio_id,
                    cuota_numero=primera.numero,
                    cuotas_totales=cuotas_totales,
                    movimiento_id=movimiento.movimiento_id,
                    meta=meta,
                )
            )
            lineas.append(
                construir_linea(
                    concepto_id="INTERES_FINANCIAMIENTO",
                    monto_cent=primera.interes_cent,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    descripcion=f"Intereses de {equipo}, {primera.etiqueta}",
                    servicio_id=cliente.servicio_principal.servicio_id,
                    cuota_numero=primera.numero,
                    cuotas_totales=cuotas_totales,
                    movimiento_id=movimiento.movimiento_id,
                    meta=meta,
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    "CUOTA_EQUIPO_FINANCIADO",
                    TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
                    primera.amortizacion_cent,
                    movimiento.movimiento_id,
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    "INTERES_FINANCIAMIENTO",
                    TipoMovimiento.ALTA_EQUIPO_FINANCIADO,
                    primera.interes_cent,
                    movimiento.movimiento_id,
                )
            )

        ciclo.notas["financiamiento"] = meta
        return ResultadoEscenario(lineas, [movimiento], _sin_ceros(filas))


# --------------------------------------------------------------------------- #
# 3 · CORTE_RECONEXION
# --------------------------------------------------------------------------- #
class CorteReconexion(Escenario):
    """Suspensión por deuda y posterior reconexión dentro del mismo ciclo.

    * **VENCIDA**: la renta del ciclo se parte en tres tramos (activo, suspendido, de
      nuevo activo) y los días suspendidos no se cobran, así que la renta baja; encima
      se suma el cargo fijo de reconexión, que se cobra una sola vez.
    * **ADELANTADA**: la renta del ciclo ya se había cobrado por adelantado en el
      recibo anterior, así que la devolución de los días sin servicio aparece como una
      línea de ajuste negativa en este recibo, junto al cargo de reconexión.

    El efecto neto es contraintuitivo y hay que explicarlo: al cliente le devuelven los
    días sin servicio **y** le cobran la reconexión, y normalmente el recibo sube.
    """

    nombre = "CORTE_RECONEXION"
    descripcion = "Suspensión por morosidad y reconexión: ajuste de días y cargo fijo"
    conceptos_que_toca = frozenset(
        {
            "RENTA_PLAN_MOVIL",
            "RENTA_HOGAR_INTERNET",
            "RENTA_TV",
            "RENTA_LINEA_FIJA",
            "RENTA_MOVISTAR_TOTAL",
            "AJUSTE_DIAS_SUSPENSION",
            "CARGO_RECONEXION",
        }
    )
    causas_esperadas = (TipoMovimiento.SUSPENSION, TipoMovimiento.RECONEXION)

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        opciones = cliente.opcion(self.nombre)
        opciones["dia_suspension"] = rng.randint(4, 16)
        opciones["dias_suspendido"] = rng.randint(3, 11)

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        servicio = cliente.servicio_principal
        opciones = cliente.opcion(self.nombre)

        dia_suspension = max(2, min(int(opciones["dia_suspension"]), ciclo.dias - 4))
        dias_suspendido = max(
            1, min(int(opciones["dias_suspendido"]), ciclo.dias - dia_suspension - 1)
        )
        fecha_suspension = ciclo.inicio + timedelta(days=dia_suspension)
        fecha_reconexion = fecha_suspension + timedelta(days=dias_suspendido)

        movimiento_suspension = self._movimiento(
            cliente,
            TipoMovimiento.SUSPENSION,
            fecha_suspension,
            DetalleSuspension(
                motivo="MOROSIDAD",
                estado=EstadoServicio.SUSPENDIDO,
                fecha_fin_prevista=fecha_reconexion,
                servicio_id=servicio.servicio_id,
            ).model_dump(mode="json"),
            rng,
            servicio_id=servicio.servicio_id,
        )
        cargo_reconexion = reglas.politica.cargo_reconexion_cent
        movimiento_reconexion = self._movimiento(
            cliente,
            TipoMovimiento.RECONEXION,
            fecha_reconexion,
            DetalleReconexion(
                cargo_cent=cargo_reconexion,
                dias_suspendido=dias_suspendido,
                servicio_id=servicio.servicio_id,
            ).model_dump(mode="json"),
            rng,
            servicio_id=servicio.servicio_id,
        )

        se_cobra_suspendido = reglas.tramo_es_facturable(True)
        lineas: list[LineaRecibo] = []
        filas: list[GroundTruthCausaDelta] = []

        lineas.append(
            construir_linea(
                concepto_id="CARGO_RECONEXION",
                monto_cent=cargo_reconexion,
                periodo_imputado=ciclo.periodo,
                reglas=reglas,
                descripcion=(
                    "Reactivación del servicio el "
                    f"{fecha_reconexion.day} de {_mes(fecha_reconexion)}"
                ),
                servicio_id=servicio.servicio_id,
                fecha_inicio=fecha_reconexion,
                fecha_fin=fecha_reconexion + timedelta(days=1),
                movimiento_id=movimiento_reconexion.movimiento_id,
            )
        )
        filas.append(
            self._gt(
                cliente,
                ciclo,
                "CARGO_RECONEXION",
                TipoMovimiento.RECONEXION,
                cargo_reconexion,
                movimiento_reconexion.movimiento_id,
            )
        )

        if ciclo.modalidad_renta is ModalidadRenta.VENCIDA:
            tramos = [
                Tramo.crear(
                    inicio=ciclo.inicio,
                    fin=fecha_suspension,
                    tarifa_mensual_cent=servicio.tarifa_cent,
                    dias_ciclo=ciclo.dias_efectivos,
                    concepto_id=servicio.concepto_id,
                    plan=servicio.plan,
                ),
                Tramo.crear(
                    inicio=fecha_suspension,
                    fin=fecha_reconexion,
                    tarifa_mensual_cent=servicio.tarifa_cent,
                    dias_ciclo=ciclo.dias_efectivos,
                    estado=EstadoServicio.SUSPENDIDO,
                    facturable=se_cobra_suspendido,
                    concepto_id=servicio.concepto_id,
                    plan=servicio.plan,
                ),
                Tramo.crear(
                    inicio=fecha_reconexion,
                    fin=ciclo.fin,
                    tarifa_mensual_cent=servicio.tarifa_cent,
                    dias_ciclo=ciclo.dias_efectivos,
                    concepto_id=servicio.concepto_id,
                    plan=servicio.plan,
                ),
            ]
            monto = sum(tramo.monto_prorrateado_cent for tramo in tramos)
            lineas.append(
                construir_linea(
                    concepto_id=servicio.concepto_id,
                    monto_cent=monto,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    nombre_comercial=servicio.nombre_comercial,
                    descripcion=(f"{servicio.plan}, sin cobro {tramos[1].etiqueta} por suspensión"),
                    servicio_id=servicio.servicio_id,
                    dias_prorrateo=ciclo.dias - (0 if se_cobra_suspendido else dias_suspendido),
                    fecha_inicio=ciclo.inicio,
                    fecha_fin=ciclo.fin,
                    movimiento_id=movimiento_suspension.movimiento_id,
                    tramos=tramos,
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    servicio.concepto_id,
                    TipoMovimiento.SUSPENSION,
                    monto - servicio.tarifa_cent,
                    movimiento_suspension.movimiento_id,
                )
            )
        else:
            ajuste = (
                0
                if se_cobra_suspendido
                else -prorratear(servicio.tarifa_cent, dias_suspendido, ciclo.dias_efectivos)
            )
            tramo_suspendido = Tramo(
                inicio=fecha_suspension,
                fin=fecha_reconexion,
                dias=dias_suspendido,
                tarifa_mensual_cent=servicio.tarifa_cent,
                estado=EstadoServicio.SUSPENDIDO,
                facturable=False,
                monto_prorrateado_cent=0,
                etiqueta=etiqueta_rango_fechas(fecha_suspension, fecha_reconexion),
                concepto_id="AJUSTE_DIAS_SUSPENSION",
                plan=servicio.plan,
            )
            if ajuste != 0:
                lineas.append(
                    construir_linea(
                        concepto_id="AJUSTE_DIAS_SUSPENSION",
                        monto_cent=ajuste,
                        periodo_imputado=ciclo.periodo,
                        reglas=reglas,
                        descripcion=f"Devolución por los días sin servicio, {tramo_suspendido.etiqueta}",
                        servicio_id=servicio.servicio_id,
                        dias_prorrateo=dias_suspendido,
                        fecha_inicio=fecha_suspension,
                        fecha_fin=fecha_reconexion,
                        movimiento_id=movimiento_suspension.movimiento_id,
                        tramos=[tramo_suspendido],
                    )
                )
                filas.append(
                    self._gt(
                        cliente,
                        ciclo,
                        "AJUSTE_DIAS_SUSPENSION",
                        TipoMovimiento.SUSPENSION,
                        ajuste,
                        movimiento_suspension.movimiento_id,
                    )
                )

        ciclo.estado_servicio = EstadoServicio.ACTIVO
        ciclo.notas["suspension"] = {
            "inicio": fecha_suspension.isoformat(),
            "fin": fecha_reconexion.isoformat(),
            "dias": dias_suspendido,
        }
        return ResultadoEscenario(
            lineas, [movimiento_suspension, movimiento_reconexion], _sin_ceros(filas)
        )


def _mes(fecha: date) -> str:
    """Nombre del mes en español de Perú."""
    return MESES_ES[fecha.month]


# --------------------------------------------------------------------------- #
# 4 · FIN_DESCUENTO
# --------------------------------------------------------------------------- #
class FinDescuento(Escenario):
    """Una promoción con fecha de fin se acaba y el recibo vuelve al precio de lista.

    * **VENCIDA**: la promoción vence a mitad del ciclo, así que el descuento se aplica
      solo por los días en que estuvo vigente. El cliente ve el mismo descuento con un
      importe menor, lo que suele leerse como un error de facturación.
    * **ADELANTADA**: la promoción vence al cierre del ciclo anterior, de modo que la
      renta anticipada del ciclo siguiente ya no la lleva y la línea desaparece.

    Es la causa oficial *promociones vencidas* y la que más sensación de cobro indebido
    genera, porque nada nuevo aparece en el recibo: algo dejó de estar.
    """

    nombre = "FIN_DESCUENTO"
    descripcion = "Promoción vencida: el descuento se prorratea o desaparece"
    conceptos_que_toca = frozenset({"DESCUENTO_PROMOCIONAL"})
    causas_esperadas = (TipoMovimiento.FIN_DESCUENTO,)

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        if cliente.descuento is None:
            promocion_id, nombre = rng.choice(PROMOCIONES)
            tarifa = cliente.servicio_principal.tarifa_cent
            monto = redondear_banca(tarifa * rng.choice((2000, 2500, 3000, 4000)), 10_000)
            cliente.descuento = DescuentoBase(
                promocion_id=promocion_id,
                nombre=nombre,
                monto_cent=max(500, monto),
                meses_vigencia=rng.choice((6, 12)),
            )
        cliente.opcion(self.nombre)["dia_fin"] = rng.randint(6, 20)

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        descuento = cliente.descuento
        if descuento is None:  # pragma: no cover - preparar() lo garantiza
            raise RuntimeError("FIN_DESCUENTO requiere un descuento vigente en el perfil")

        lineas: list[LineaRecibo] = []
        filas: list[GroundTruthCausaDelta] = []

        if ciclo.modalidad_renta is ModalidadRenta.VENCIDA:
            dia_fin = max(2, min(int(cliente.opcion(self.nombre)["dia_fin"]), ciclo.dias - 2))
            fecha_fin = ciclo.inicio + timedelta(days=dia_fin)
        else:
            dia_fin = 0
            fecha_fin = ciclo.inicio

        movimiento = self._movimiento(
            cliente,
            TipoMovimiento.FIN_DESCUENTO,
            fecha_fin,
            DetalleFinDescuento(
                promocion_id=descuento.promocion_id,
                nombre=descuento.nombre,
                descuento_cent=descuento.monto_cent,
                meses_vigencia=descuento.meses_vigencia,
            ).model_dump(mode="json"),
            rng,
            servicio_id=cliente.servicio_principal.servicio_id,
        )

        if ciclo.modalidad_renta is ModalidadRenta.VENCIDA:
            tramo = Tramo.crear(
                inicio=ciclo.inicio,
                fin=fecha_fin,
                tarifa_mensual_cent=-descuento.monto_cent,
                dias_ciclo=ciclo.dias_efectivos,
                concepto_id=descuento.concepto_id,
                plan=cliente.servicio_principal.plan,
            )
            monto = tramo.monto_prorrateado_cent
            lineas.append(
                construir_linea(
                    concepto_id=descuento.concepto_id,
                    monto_cent=monto,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    nombre_comercial=descuento.nombre,
                    descripcion=f"{descuento.nombre}, vigente {tramo.etiqueta}",
                    servicio_id=cliente.servicio_principal.servicio_id,
                    dias_prorrateo=dia_fin,
                    fecha_inicio=ciclo.inicio,
                    fecha_fin=fecha_fin,
                    movimiento_id=movimiento.movimiento_id,
                    tramos=[tramo],
                    meta={"promocion_id": descuento.promocion_id, "vencida": True},
                )
            )
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    descuento.concepto_id,
                    TipoMovimiento.FIN_DESCUENTO,
                    monto - (-descuento.monto_cent),
                    movimiento.movimiento_id,
                )
            )
        else:
            ciclo.retirar_base(descuento.concepto_id)
            filas.append(
                self._gt(
                    cliente,
                    ciclo,
                    descuento.concepto_id,
                    TipoMovimiento.FIN_DESCUENTO,
                    descuento.monto_cent,
                    movimiento.movimiento_id,
                )
            )

        ciclo.notas["fin_descuento"] = {
            "promocion_id": descuento.promocion_id,
            "nombre": descuento.nombre,
            "fecha": fecha_fin.isoformat(),
        }
        return ResultadoEscenario(lineas, [movimiento], _sin_ceros(filas))


# --------------------------------------------------------------------------- #
# 5 · ALTA_PAQUETE
# --------------------------------------------------------------------------- #
class AltaPaquete(Escenario):
    """Compra de un paquete durante el ciclo (datos, roaming o canales).

    Los paquetes de compra puntual se cobran completos en el ciclo de la compra y no se
    prorratean. Los paquetes recurrentes sí: en renta vencida se cobran por los días
    desde el alta, y en renta adelantada se cobra el primer mes completo del ciclo
    siguiente, que es cuando empiezan a estar disponibles en el recibo.
    """

    nombre = "ALTA_PAQUETE"
    descripcion = "Compra de paquete de datos, roaming o canales durante el ciclo"
    conceptos_que_toca = frozenset(
        {
            "PAQUETE_DATOS_ADICIONAL",
            "PAQUETE_ROAMING",
            "PAQUETE_TV_PREMIUM",
            "SERVICIO_ADICIONAL_SEGURO",
        }
    )
    causas_esperadas = (TipoMovimiento.ALTA_PAQUETE,)

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        opciones = cliente.opcion(self.nombre)
        opciones["recurrente"] = rng.random() < 0.35
        if opciones["recurrente"]:
            concepto_id, nombre, monto = rng.choice(PAQUETES_RECURRENTES)
            opciones["cantidad"] = 1
        else:
            concepto_id, nombre, monto = rng.choice(PAQUETES_UNICOS)
            opciones["cantidad"] = rng.choice((1, 1, 1, 2))
        opciones["concepto_id"] = concepto_id
        opciones["nombre"] = nombre
        opciones["monto_cent"] = monto
        opciones["dia_alta"] = rng.randint(3, 24)

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        opciones = cliente.opcion(self.nombre)
        concepto_id: str = opciones["concepto_id"]
        nombre: str = opciones["nombre"]
        precio: int = opciones["monto_cent"]
        cantidad: int = opciones["cantidad"]
        recurrente: bool = opciones["recurrente"]
        dia_alta = max(1, min(int(opciones["dia_alta"]), ciclo.dias - 1))
        fecha_alta = ciclo.inicio + timedelta(days=dia_alta)

        movimiento = self._movimiento(
            cliente,
            TipoMovimiento.ALTA_PAQUETE,
            fecha_alta,
            DetalleAltaPaquete(
                paquete_id=concepto_id,
                nombre=nombre,
                monto_cent=precio,
                recurrente=recurrente,
            ).model_dump(mode="json"),
            rng,
            servicio_id=cliente.servicio_principal.servicio_id,
        )

        if not recurrente:
            monto = precio * cantidad
            linea = construir_linea(
                concepto_id=concepto_id,
                monto_cent=monto,
                periodo_imputado=ciclo.periodo,
                reglas=reglas,
                nombre_comercial=nombre,
                descripcion=f"{nombre} activado el {fecha_alta.day} de {_mes(fecha_alta)}",
                servicio_id=cliente.servicio_principal.servicio_id,
                cantidad=cantidad,
                fecha_inicio=fecha_alta,
                fecha_fin=ciclo.fin,
                movimiento_id=movimiento.movimiento_id,
            )
        elif ciclo.modalidad_renta is ModalidadRenta.VENCIDA:
            tramo = Tramo.crear(
                inicio=fecha_alta,
                fin=ciclo.fin,
                tarifa_mensual_cent=precio,
                dias_ciclo=ciclo.dias_efectivos,
                concepto_id=concepto_id,
                plan=nombre,
            )
            monto = tramo.monto_prorrateado_cent
            linea = construir_linea(
                concepto_id=concepto_id,
                monto_cent=monto,
                periodo_imputado=ciclo.periodo,
                reglas=reglas,
                nombre_comercial=nombre,
                descripcion=f"{nombre}, {tramo.etiqueta}",
                servicio_id=cliente.servicio_principal.servicio_id,
                dias_prorrateo=ciclo.dias - dia_alta,
                fecha_inicio=fecha_alta,
                fecha_fin=ciclo.fin,
                movimiento_id=movimiento.movimiento_id,
                tramos=[tramo],
            )
        else:
            tramo = _tramo_completo(
                ciclo.inicio_siguiente,
                ciclo.fin_siguiente,
                precio,
                concepto_id,
                nombre,
            )
            monto = tramo.monto_prorrateado_cent
            linea = construir_linea(
                concepto_id=concepto_id,
                monto_cent=monto,
                periodo_imputado=ciclo.periodo_siguiente,
                reglas=reglas,
                nombre_comercial=nombre,
                descripcion=f"{nombre}, {tramo.etiqueta}",
                servicio_id=cliente.servicio_principal.servicio_id,
                dias_prorrateo=ciclo.dias_siguiente,
                fecha_inicio=ciclo.inicio_siguiente,
                fecha_fin=ciclo.fin_siguiente,
                movimiento_id=movimiento.movimiento_id,
                tramos=[tramo],
            )

        fila = self._gt(
            cliente,
            ciclo,
            concepto_id,
            TipoMovimiento.ALTA_PAQUETE,
            monto,
            movimiento.movimiento_id,
        )
        ciclo.notas["alta_paquete"] = {"nombre": nombre, "fecha": fecha_alta.isoformat()}
        return ResultadoEscenario([linea], [movimiento], _sin_ceros([fila]))


# --------------------------------------------------------------------------- #
# 6 · NOTA_CREDITO
# --------------------------------------------------------------------------- #
class NotaCredito(Escenario):
    """Nota de crédito (resta) o de débito (suma) aplicada en el recibo.

    Son documentos tributarios: existen por exigencia fiscal, no por decisión
    comercial, y por eso su redacción en el recibo es opaca para el cliente. La nota de
    crédito baja el recibo (y suele generar la consulta "¿por qué me vino más barato?"),
    la de débito lo sube por algo que se dejó de facturar antes.
    """

    nombre = "NOTA_CREDITO"
    descripcion = "Nota de crédito o de débito que corrige una facturación anterior"
    conceptos_que_toca = frozenset({"NOTA_CREDITO", "NOTA_DEBITO"})
    causas_esperadas = (TipoMovimiento.NOTA_CREDITO, TipoMovimiento.NOTA_DEBITO)

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        opciones = cliente.opcion(self.nombre)
        concepto_id, motivo = rng.choice(MOTIVOS_NOTA)
        opciones["concepto_id"] = concepto_id
        opciones["motivo"] = motivo
        opciones["monto_cent"] = rng.choice((990, 1490, 1990, 2490, 2990, 3990, 4990))
        opciones["correlativo"] = rng.randint(100_000, 999_999)
        opciones["dia"] = rng.randint(2, 25)

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        opciones = cliente.opcion(self.nombre)
        concepto_id: str = opciones["concepto_id"]
        es_credito = concepto_id == "NOTA_CREDITO"
        importe: int = opciones["monto_cent"]
        monto = -importe if es_credito else importe
        prefijo = "NC" if es_credito else "ND"
        documento = f"{prefijo}-{opciones['correlativo']}"
        fecha = ciclo.inicio + timedelta(days=max(1, min(int(opciones["dia"]), ciclo.dias - 1)))
        tipo = TipoMovimiento.NOTA_CREDITO if es_credito else TipoMovimiento.NOTA_DEBITO

        movimiento = self._movimiento(
            cliente,
            tipo,
            fecha,
            DetalleNota(
                documento=documento,
                monto_cent=monto,
                motivo=opciones["motivo"],
            ).model_dump(mode="json"),
            rng,
            servicio_id=cliente.servicio_principal.servicio_id,
        )
        linea = construir_linea(
            concepto_id=concepto_id,
            monto_cent=monto,
            periodo_imputado=ciclo.periodo,
            reglas=reglas,
            descripcion=opciones["motivo"],
            servicio_id=cliente.servicio_principal.servicio_id,
            fecha_inicio=fecha,
            fecha_fin=fecha + timedelta(days=1),
            movimiento_id=movimiento.movimiento_id,
            meta={"documento": documento, "motivo": opciones["motivo"]},
        )
        fila = self._gt(cliente, ciclo, concepto_id, tipo, monto, movimiento.movimiento_id)
        ciclo.notas["nota"] = {"documento": documento, "motivo": opciones["motivo"]}
        return ResultadoEscenario([linea], [movimiento], _sin_ceros([fila]))


# --------------------------------------------------------------------------- #
# 7 · DEUDA_ANTERIOR
# --------------------------------------------------------------------------- #
class DeudaAnterior(Escenario):
    """El recibo anterior quedó impago: se arrastra el saldo y se cobra mora.

    Este escenario es el que rompe la intuición del cliente: **el recibo del mes no
    subió**, lo que subió es el total a pagar, porque arrastra lo que quedó pendiente.
    La deuda anterior no forma parte del total del periodo (igual que en ``Recibo``), y
    por eso su fila de ground truth queda fuera de la conciliación del delta.

    Además no hay ningún movimiento de Amdocs que lo explique: el interés moratorio no
    tiene causas permitidas en ``rules.yaml``. Es, a propósito, el caso de *cero
    candidatos* de la atribución, que debe resolverse con confianza baja y sin inventar
    una causa.
    """

    nombre = "DEUDA_ANTERIOR"
    descripcion = "Recibo anterior impago: saldo arrastrado más interés moratorio"
    conceptos_que_toca = frozenset({"DEUDA_ANTERIOR", "INTERES_MORATORIO"})
    causas_esperadas = ()

    def preparar(self, cliente: PerfilCliente, rng: Random) -> None:
        opciones = cliente.opcion(self.nombre)
        opciones["porcentaje_bp"] = rng.choice((10_000, 10_000, 5_000))
        opciones["tasa_mora_bp"] = rng.choice((100, 150, 200))

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        reglas = self._reglas()
        opciones = cliente.opcion(self.nombre)
        deuda = redondear_banca(ciclo.total_previo_cent * int(opciones["porcentaje_bp"]), 10_000)
        ciclo.deuda_anterior_cent = deuda

        interes = aplicar_porcentaje(deuda, int(opciones["tasa_mora_bp"]))
        lineas: list[LineaRecibo] = []
        filas = [
            self._gt(
                cliente,
                ciclo,
                CONCEPTO_DEUDA_ANTERIOR,
                None,
                deuda - ciclo.deuda_previa_cent,
                None,
            )
        ]
        if interes != 0:
            lineas.append(
                construir_linea(
                    concepto_id="INTERES_MORATORIO",
                    monto_cent=interes,
                    periodo_imputado=ciclo.periodo,
                    reglas=reglas,
                    descripcion="Interés por el recibo anterior pagado fuera de fecha",
                    servicio_id=cliente.servicio_principal.servicio_id,
                    fecha_inicio=ciclo.inicio,
                    fecha_fin=ciclo.fin,
                    meta={"tasa_mensual_bp": int(opciones["tasa_mora_bp"])},
                )
            )
            filas.append(self._gt(cliente, ciclo, "INTERES_MORATORIO", None, interes, None))

        ciclo.notas["deuda_anterior"] = {
            "monto_cent": deuda,
            "vence": ciclo.fecha_vencimiento.isoformat(),
        }
        return ResultadoEscenario(lineas, [], _sin_ceros(filas))


# --------------------------------------------------------------------------- #
# 8 · ESTABLE (control)
# --------------------------------------------------------------------------- #
class Estable(Escenario):
    """Control: nada cambia. Delta exactamente cero.

    Existe para comprobar lo contrario de todo lo demás: que el motor sabe decir "su
    recibo no varió" sin fabricar una explicación. Un sistema que siempre encuentra una
    causa está alucinando; este escenario lo detecta.
    """

    nombre = "ESTABLE"
    descripcion = "Control sin variación: el recibo no cambió respecto del mes anterior"
    conceptos_que_toca = frozenset()
    causas_esperadas = ()
    combinable = False

    def aplicar(
        self, cliente: PerfilCliente, ciclo: CicloFacturacion, rng: Random
    ) -> ResultadoEscenario:
        ciclo.notas["estable"] = True
        return ResultadoEscenario([], [], [])


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #
ESCENARIOS: dict[str, Escenario] = {
    escenario.nombre: escenario
    for escenario in (
        CambioPlanMedioCiclo(),
        CuotaEquipoFinanciado(),
        CorteReconexion(),
        FinDescuento(),
        AltaPaquete(),
        NotaCredito(),
        DeudaAnterior(),
        Estable(),
    )
}

#: Nombres en orden estable: el reparto round-robin del generador depende de este orden.
NOMBRES_ESCENARIOS: tuple[str, ...] = tuple(ESCENARIOS)


def obtener_escenario(nombre: str) -> Escenario:
    """Devuelve el escenario por nombre.

    Raises:
        KeyError: si el nombre no existe, con la lista de los válidos.
    """
    try:
        return ESCENARIOS[nombre]
    except KeyError as exc:
        raise KeyError(
            f"escenario desconocido: {nombre!r}. Válidos: {', '.join(NOMBRES_ESCENARIOS)}"
        ) from exc


def escenarios_por_nombre(nombres: list[str] | tuple[str, ...]) -> list[Escenario]:
    """Resuelve una lista de nombres de escenario."""
    return [obtener_escenario(nombre) for nombre in nombres]


def son_compatibles(uno: str, otro: str) -> bool:
    """Dos escenarios se combinan solo si no tocan ningún concepto en común.

    Es la condición que mantiene exacto el ground truth compuesto: si dos escenarios
    escribieran la misma línea, el segundo pisaría al primero y su fila de ``gt``
    quedaría mintiendo.
    """
    if uno == otro:
        return False
    a, b = obtener_escenario(uno), obtener_escenario(otro)
    if not (a.combinable and b.combinable):
        return False
    return not (a.conceptos_que_toca & b.conceptos_que_toca)


def pares_compatibles() -> list[tuple[str, str]]:
    """Todos los pares de escenarios combinables, en orden determinista."""
    nombres = sorted(NOMBRES_ESCENARIOS)
    return [
        (uno, otro)
        for indice, uno in enumerate(nombres)
        for otro in nombres[indice + 1 :]
        if son_compatibles(uno, otro)
    ]
