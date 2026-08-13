"""Router de desarrollo. **Solo se monta si ``ENTORNO=dev``.**

Dos utilidades para la demo en vivo:

* ``POST /dev/token`` — emite tokens JWT de prueba en cualquiera de los cuatro niveles.
  En producción los emite el IdP de Movistar; aquí hace falta uno para poder enseñar la
  matriz de niveles sin montar un proveedor de identidad.
* ``POST /dev/alucinar`` — activa el **modo adversario**. Inyecta una cifra inventada en
  una explicación ya generada y enseña que el verificador la caza y que la respuesta se
  bloquea. Es la demostración de la métrica oficial *"cero invenciones financieras
  comprobables mediante logs de la terminal"*: sin un caso negativo, un ``PASS`` no
  prueba nada.

La guarda es doble: ``main.py`` no incluye este router fuera de ``dev`` y, además, cada
ruta comprueba el entorno. Un despliegue mal configurado no debe poder firmar tokens.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import (
    AdversarioDep,
    AjustesDep,
    ProveedorDep,
    ReglasDep,
    RepositorioDep,
)
from apps.api.errores import ErrorApi
from apps.api.routers.hechos import construir_hechos
from apps.api.security import Identidad, cuenta_autorizada, emitir_token, requiere_nivel
from apps.api.settings import Ajustes
from packages.core_domain.enums import Canal, NivelAseguramiento, Verbosidad
from packages.llm_layer.generador import generar_explicacion
from packages.llm_layer.verificador import construir_permitidos, inyectar_alucinacion, verificar

__all__ = ["router"]

_LOG = logging.getLogger(__name__)


def solo_desarrollo(ajustes: AjustesDep) -> Ajustes:
    """Corta la ruta si el servicio no está en modo desarrollo."""
    if not ajustes.es_desarrollo:
        raise ErrorApi(
            status.HTTP_404_NOT_FOUND,
            "FUNCION_NO_DISPONIBLE",
            "las utilidades de desarrollo solo existen con ENTORNO=dev",
        )
    return ajustes


router = APIRouter(prefix="/dev", tags=["desarrollo"], dependencies=[Depends(solo_desarrollo)])


# --------------------------------------------------------------------------- #
# Emisión de tokens
# --------------------------------------------------------------------------- #
class PeticionToken(BaseModel):
    """Cuerpo de ``POST /dev/token``."""

    model_config = ConfigDict(extra="forbid")

    cuenta_id: str = Field(default="C-DEMO-01", description="account_ref que irá en 'sub'")
    nivel: NivelAseguramiento = NivelAseguramiento.LOA2
    canal: Canal | None = None
    amr: list[str] | None = Field(default=None, description="Métodos de autenticación (claim amr)")
    acting_on_behalf_of: str | None = Field(
        default=None, description="Obligatorio si el nivel es LOA_ASESOR"
    )
    minutos: int | None = Field(default=None, ge=1, le=1440)


class RespuestaToken(BaseModel):
    """Token emitido y sus claims, para poder inspeccionarlos sin decodificar nada."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"
    expira_en: datetime
    expira_en_s: int
    claims: dict[str, Any]
    uso: str = Field(
        default="Authorization: Bearer <access_token>",
        description="Cómo se envía en el resto de llamadas",
    )


@router.post("/token", summary="Emite un JWT de prueba (solo ENTORNO=dev)")
def token(peticion: PeticionToken, ajustes: AjustesDep) -> RespuestaToken:
    """Firma un token con los claims ``sub``, ``acr``, ``amr`` y ``exp``.

    Recordatorio de la matriz: ``LOA0`` solo abre ``/v1/catalogo``; ``LOA1`` explica sin
    importes; ``LOA2`` lo abre todo; ``LOA_ASESOR`` es ``LOA2`` con
    ``acting_on_behalf_of`` obligatorio, que además queda registrado en cada evento de
    auditoría del turno.
    """
    valor, expira = emitir_token(
        peticion.cuenta_id,
        peticion.nivel,
        amr=peticion.amr,
        acting_on_behalf_of=peticion.acting_on_behalf_of,
        canal=peticion.canal,
        minutos=peticion.minutos,
        ajustes=ajustes,
    )
    claims: dict[str, Any] = {
        "sub": peticion.cuenta_id,
        "acr": str(peticion.nivel),
        "amr": peticion.amr,
        "iss": ajustes.jwt_emisor,
        "aud": ajustes.jwt_audiencia,
    }
    if peticion.acting_on_behalf_of:
        claims["act"] = peticion.acting_on_behalf_of
    if peticion.canal:
        claims["canal"] = str(peticion.canal)
    return RespuestaToken(
        access_token=valor,
        expira_en=expira,
        expira_en_s=(peticion.minutos or ajustes.jwt_ttl_min) * 60,
        claims=claims,
    )


# --------------------------------------------------------------------------- #
# Modo adversario
# --------------------------------------------------------------------------- #
class PeticionAlucinar(BaseModel):
    """Cuerpo de ``POST /dev/alucinar``."""

    model_config = ConfigDict(extra="forbid")

    activar: bool = Field(default=True, description="Activa o desactiva el modo adversario")
    delta_cent: int = Field(default=731, description="Céntimos que se inventarán en el texto")
    turnos: int = Field(default=1, ge=1, le=10, description="Turnos de /v1/explicar afectados")
    cuenta_id: str | None = Field(
        default=None, description="Si se indica, ejecuta la demo adversaria en el acto"
    )
    periodo: str | None = None


class RespuestaAlucinar(BaseModel):
    """Estado del modo adversario y, si se pidió, el resultado de la demo inmediata."""

    model_config = ConfigDict(extra="forbid")

    activo: bool
    delta_cent: int
    turnos_restantes: int
    aviso: str
    demo: dict[str, Any] | None = None


AVISO_ADVERSARIO = (
    "Modo adversario activo: el próximo POST /v1/explicar recibirá una cifra inventada "
    "en el texto ya generado. El verificador debe cazarla, la respuesta debe bloquearse "
    "y el turno debe terminar en derivación con NO ANCLADAS > 0 en el log."
)


@router.post("/alucinar", summary="Activa el modo adversario de la demo (solo ENTORNO=dev)")
def alucinar(
    peticion: PeticionAlucinar,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA2))],
    adversario: AdversarioDep,
    repositorio: RepositorioDep,
    reglas: ReglasDep,
    proveedor: ProveedorDep,
    ajustes: AjustesDep,
) -> RespuestaAlucinar:
    """Activa el modo adversario y, opcionalmente, lo demuestra en el acto.

    Con ``cuenta_id`` se ejecuta el ciclo completo sin tocar el estado de la conversación:
    se construye el FactSet, se genera la explicación real, se **envenena** el texto con
    una cifra que no existe en el FactSet y se vuelve a verificar. Lo que se devuelve es
    la comparación: el mismo texto verifica ``PASS`` limpio y ``FAIL`` envenenado, con la
    lista de infractores y las líneas de terminal.
    """
    adversario.activo = peticion.activar
    adversario.delta_cent = peticion.delta_cent
    adversario.turnos_restantes = peticion.turnos if peticion.activar else 0

    demo: dict[str, Any] | None = None
    if peticion.cuenta_id:
        cuenta = cuenta_autorizada(identidad, peticion.cuenta_id)
        factset, _ = construir_hechos(repositorio, reglas, cuenta, peticion.periodo)
        permitidos = construir_permitidos(factset)
        resultado = generar_explicacion(
            factset,
            utterance="¿por qué me vino más caro este mes?",
            verbosidad=Verbosidad.CORTO,
            proveedor=proveedor,
            estricto=ajustes.verificador_estricto,
            permitidos=permitidos,
        )
        envenenado = inyectar_alucinacion(resultado.texto, factset, delta_cent=peticion.delta_cent)
        veredicto = verificar(
            envenenado, factset, permitidos=permitidos, estricto=ajustes.verificador_estricto
        )
        _LOG.error("DEMO ADVERSARIA sobre %s: infractores %s", cuenta, veredicto.infractores)
        demo = {
            "cuenta_id": cuenta,
            "periodo": factset.periodo_actual,
            "factset_sha256": factset.sha256,
            "veredicto_limpio": str(resultado.verificacion.veredicto),
            "no_ancladas_limpio": resultado.verificacion.no_ancladas,
            "veredicto_envenenado": str(veredicto.veredicto),
            "no_ancladas_envenenado": veredicto.no_ancladas,
            "infractores": list(veredicto.infractores),
            "tokens_infractores": list(veredicto.tokens_infractores),
            "texto_envenenado": envenenado,
            "terminal": veredicto.lineas_terminal(),
            "conclusion": (
                "la cifra inventada no está en el FactSet, el verificador la marca como "
                "NO_ANCLADA y la respuesta no llega al cliente"
            ),
        }

    return RespuestaAlucinar(
        activo=adversario.activo,
        delta_cent=adversario.delta_cent,
        turnos_restantes=max(adversario.turnos_restantes, 0),
        aviso=AVISO_ADVERSARIO if adversario.activo else "modo adversario desactivado",
        demo=demo,
    )


@router.get("/cuentas", summary="Cuentas disponibles en el origen que sirve los recibos")
def cuentas(ajustes: AjustesDep, repositorio: RepositorioDep) -> dict[str, Any]:
    """Lista cuentas que se pueden explicar **en el origen que está sirviendo ahora**.

    La pregunta que responde no es «qué hay en disco» sino «qué puedo escribir en la
    pantalla de entrada para que funcione». Son la misma cosa solo cuando los recibos
    salen del disco; con ``ORIGEN_RECIBOS=supabase`` dejan de serlo, y la diferencia se
    pagaba entera en la cara del usuario: la interfaz ofrecía ``C-DEMO-01``, el ACL la
    buscaba en Supabase, saltaba «la cuenta no existe» y el login moría. El desplegable
    prometía cuentas que el motor no podía servir.

    Por eso se le pregunta al **transporte que de verdad atiende los recibos**, en lugar
    de mirar un directorio. Si ese transporte sabe enumerar sus cuentas se le pide la
    lista; si no —el de fichero no la necesita, los ficheros ya están ahí— se recorre el
    disco como siempre.
    """
    transporte = getattr(repositorio.brainybill, "transporte", None)
    enumerar = getattr(transporte, "cuentas", None)
    if callable(enumerar):
        reales = list(enumerar(10))
        return {
            "raiz": f"{getattr(transporte, 'nombre', 'externo')}:cargo_facturado",
            "total": len(reales),
            # Van en ``demo`` porque es la clave que lee la interfaz para poblar el
            # desplegable. No son cuentas de guion —el dataset real no trae guion— pero
            # sí son las únicas que la pantalla puede ofrecer sin mentir.
            "demo": reales,
            # Sin guion: describir el caso de una cuenta real exigiría abrir su recibo, y
            # este endpoint no explica nada, solo dice a quién se puede preguntar.
            "guion": {},
            "muestra": reales,
        }

    directorio = ajustes.ruta_datos / "bills"
    disponibles = (
        sorted(ruta.stem for ruta in directorio.glob("*.json")) if directorio.is_dir() else []
    )
    return {
        "raiz": str(ajustes.ruta_datos),
        "total": len(disponibles),
        "demo": [cuenta for cuenta in disponibles if cuenta.startswith("C-DEMO")],
        "guion": {
            "C-DEMO-01": "cambio de plan a mitad de ciclo · renta ADELANTADA · "
            "cuota de equipo financiado como distractor",
            "C-DEMO-02": "corte y reconexión por morosidad · renta VENCIDA",
            "C-DEMO-03": "fin de descuento prorrateado + deuda anterior arrastrada",
        },
        "muestra": disponibles[:10],
    }
