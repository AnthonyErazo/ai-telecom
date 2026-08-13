"""Autenticación JWT y matriz de niveles de aseguramiento (sección 9).

La ficha exige *"no mostrar información sensible sin autenticación"* y *"autenticación
para el acceso a información sensible"*. Aquí está esa puerta.

Token
-----
JWT **HS256** firmado localmente. Claims:

======  =======================================================================
Claim   Contenido
======  =======================================================================
``sub`` ``account_ref``: la cuenta del titular. **Es la única fuente de cuenta.**
``acr`` nivel de aseguramiento (``LOA0``…``LOA_ASESOR``).
``amr`` métodos de autenticación usados (``["app","biometria"]``, ``["otp"]``…).
``exp`` expiración (obligatoria).
``act`` ``acting_on_behalf_of``: solo en ``LOA_ASESOR``, la cuenta atendida.
======  =======================================================================

Matriz de niveles (literal de la sección 9)
-------------------------------------------
============  ===========================================================================
Nivel         Qué puede ver
============  ===========================================================================
``LOA0``      Solo ``/v1/catalogo``: definiciones de conceptos. Ningún dato del cliente.
``LOA1``      (WhatsApp) Existencia y **dirección** del cambio. **Ningún monto.**
``LOA2``      (App Mi Movistar) Explicación completa con importes.
``LOA_ASESOR``Como ``LOA2``, con ``acting_on_behalf_of`` obligatorio y **registrado en
              auditoría** en cada evento del turno.
============  ===========================================================================

LOA1 no se implementa "no llamando al motor": se calcula igual y se **redacta** la
respuesta con :func:`redactar_para_nivel`, que pasa todo texto por el saneador del
retriever —cuya garantía es que el resultado no contiene ni un dígito— y elimina los
bloques estructurados (``kv``, ``puente``, ``tabla``), que son importes por definición.
Así la misma explicación sirve a los tres canales y la diferencia es una única función
auditable, no tres caminos de código.

Regla innegociable
------------------
**El ``account_ref`` se deriva SIEMPRE del token, jamás del cuerpo, de la query ni del
texto del usuario.** Si una petición trae un ``cuenta_id`` distinto al del token se
rechaza con ``403 CUENTA_NO_AUTORIZADA`` (ver :func:`cuenta_autorizada`); no se "usa el
del token en silencio", porque un intento de acceso cruzado tiene que quedar registrado.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from apps.api.errores import ErrorApi, cuenta_no_autorizada, nivel_insuficiente
from apps.api.settings import Ajustes, obtener_ajustes
from packages.core_domain.enums import Canal, NivelAseguramiento
from packages.core_domain.esquemas.factset import FactSet
from packages.core_domain.esquemas.respuesta import (
    Bloque,
    BloqueAviso,
    BloqueTexto,
    Derivacion,
    Gobernanza,
    RespuestaCanalAgnostica,
)
from packages.retriever.saneador import sanear

__all__ = [
    "NIVELES_CON_MONTOS",
    "ORDEN_NIVELES",
    "Identidad",
    "cuenta_autorizada",
    "emitir_token",
    "identidad_actual",
    "nivel_alcanza",
    "redactar_para_nivel",
    "requiere_nivel",
]

_LOG = logging.getLogger(__name__)

#: Orden de los niveles. ``LOA_ASESOR`` alcanza lo mismo que ``LOA2``, con más deberes.
ORDEN_NIVELES: dict[NivelAseguramiento, int] = {
    NivelAseguramiento.LOA0: 0,
    NivelAseguramiento.LOA1: 1,
    NivelAseguramiento.LOA2: 2,
    NivelAseguramiento.LOA_ASESOR: 2,
}

#: Niveles a los que se les puede enseñar un importe.
NIVELES_CON_MONTOS: frozenset[NivelAseguramiento] = frozenset(
    {NivelAseguramiento.LOA2, NivelAseguramiento.LOA_ASESOR}
)

#: Aviso que sustituye a las cifras en LOA1.
AVISO_LOA1 = (
    "Por seguridad, en este canal puedo indicarle si su recibo subió o bajó y por qué, "
    "pero no los importes. Ingrese a la App Mi Movistar o autentíquese para ver el "
    "detalle completo."
)

_esquema_bearer = HTTPBearer(auto_error=False, description="JWT HS256 emitido por /dev/token")


# --------------------------------------------------------------------------- #
# Identidad
# --------------------------------------------------------------------------- #
class Identidad(BaseModel):
    """Identidad autenticada del solicitante, ya validada contra la firma del token."""

    model_config = ConfigDict(extra="forbid")

    sub: str = Field(description="account_ref del titular (o del asesor en LOA_ASESOR)")
    acr: NivelAseguramiento
    amr: list[str] = Field(default_factory=list)
    exp: datetime
    iat: datetime | None = None
    acting_on_behalf_of: str | None = Field(
        default=None, description="Cuenta atendida cuando el actor es un asesor"
    )
    canal: Canal | None = None
    jti: str | None = None

    @property
    def cuenta_ref(self) -> str:
        """Cuenta sobre la que se opera.

        Para el titular es ``sub``. Para un asesor (``LOA_ASESOR``) es la cuenta que
        atiende, declarada en ``act``: el asesor nunca consulta "su" cuenta.
        """
        if self.acr is NivelAseguramiento.LOA_ASESOR:
            return self.acting_on_behalf_of or self.sub
        return self.sub

    @property
    def actor(self) -> str:
        """Quién ejecuta la acción (el asesor, si lo hay). Va al campo ``actor``."""
        return self.sub

    @property
    def ve_montos(self) -> bool:
        """``True`` si el nivel autoriza a mostrar importes."""
        return self.acr in NIVELES_CON_MONTOS

    def contexto_auditoria(self) -> dict[str, Any]:
        """Campos de identidad que acompañan a cada evento de la bitácora."""
        return {
            "actor": self.actor,
            "cuenta_ref": self.cuenta_ref,
            "acting_on_behalf_of": self.acting_on_behalf_of,
            "nivel": self.acr,
        }


def nivel_alcanza(actual: NivelAseguramiento, minimo: NivelAseguramiento) -> bool:
    """``True`` si ``actual`` cubre el mínimo exigido por el recurso."""
    return ORDEN_NIVELES[actual] >= ORDEN_NIVELES[minimo]


# --------------------------------------------------------------------------- #
# Emisión y verificación
# --------------------------------------------------------------------------- #
def emitir_token(
    cuenta_id: str,
    nivel: NivelAseguramiento = NivelAseguramiento.LOA2,
    *,
    amr: Sequence[str] | None = None,
    acting_on_behalf_of: str | None = None,
    canal: Canal | None = None,
    minutos: int | None = None,
    ajustes: Ajustes | None = None,
) -> tuple[str, datetime]:
    """Firma un JWT HS256 y devuelve ``(token, expiración)``.

    Solo la usa ``POST /dev/token``: en producción el token lo emite el IdP de Movistar
    y aquí únicamente se verifica.

    Raises:
        ErrorApi: 403 ``ACTOR_REQUERIDO`` si el nivel es ``LOA_ASESOR`` y no se declara
            a nombre de quién se actúa.
    """
    configuracion = ajustes or obtener_ajustes()
    if nivel is NivelAseguramiento.LOA_ASESOR and not acting_on_behalf_of:
        raise ErrorApi(
            status.HTTP_403_FORBIDDEN,
            "ACTOR_REQUERIDO",
            "LOA_ASESOR exige acting_on_behalf_of: un asesor siempre actúa a nombre "
            "de una cuenta identificada, y así se registra en auditoría",
            nivel_requerido=NivelAseguramiento.LOA_ASESOR,
        )
    ahora = datetime.now(UTC)
    vida = timedelta(minutes=minutos or configuracion.jwt_ttl_min)
    expira = ahora + vida
    claims: dict[str, Any] = {
        "sub": cuenta_id,
        "acr": str(nivel),
        "amr": list(amr or _amr_por_defecto(nivel)),
        "iat": int(ahora.timestamp()),
        "exp": int(expira.timestamp()),
        "iss": configuracion.jwt_emisor,
        "aud": configuracion.jwt_audiencia,
        "jti": uuid.uuid4().hex,
    }
    if acting_on_behalf_of:
        claims["act"] = acting_on_behalf_of
    if canal is not None:
        claims["canal"] = str(canal)
    token = jwt.encode(claims, configuracion.jwt_secret, algorithm=configuracion.jwt_algoritmo)
    return token, expira


def _amr_por_defecto(nivel: NivelAseguramiento) -> list[str]:
    """Métodos de autenticación plausibles por nivel (solo para tokens de prueba)."""
    return {
        NivelAseguramiento.LOA0: ["anon"],
        NivelAseguramiento.LOA1: ["msisdn"],
        NivelAseguramiento.LOA2: ["pwd", "app"],
        NivelAseguramiento.LOA_ASESOR: ["pwd", "sso_interno"],
    }[nivel]


def _decodificar(token: str, ajustes: Ajustes) -> Identidad:
    """Verifica firma, emisor, audiencia y expiración, y construye la identidad."""
    try:
        claims = jwt.decode(
            token,
            ajustes.jwt_secret,
            algorithms=[ajustes.jwt_algoritmo],
            audience=ajustes.jwt_audiencia,
            issuer=ajustes.jwt_emisor,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ErrorApi(
            status.HTTP_401_UNAUTHORIZED,
            "TOKEN_EXPIRADO",
            "el token ha expirado; vuelva a autenticarse",
            cabeceras={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise ErrorApi(
            status.HTTP_401_UNAUTHORIZED,
            "TOKEN_INVALIDO",
            f"el token no es válido: {exc}",
            cabeceras={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        nivel = NivelAseguramiento(str(claims.get("acr", NivelAseguramiento.LOA0)))
    except ValueError as exc:
        raise ErrorApi(
            status.HTTP_401_UNAUTHORIZED,
            "TOKEN_INVALIDO",
            f"claim acr desconocido: {claims.get('acr')!r}",
            cabeceras={"WWW-Authenticate": "Bearer"},
        ) from exc

    amr = claims.get("amr") or []
    canal_bruto = claims.get("canal")
    identidad = Identidad(
        sub=str(claims["sub"]),
        acr=nivel,
        amr=[str(metodo) for metodo in amr] if isinstance(amr, list) else [str(amr)],
        exp=datetime.fromtimestamp(int(claims["exp"]), UTC),
        iat=datetime.fromtimestamp(int(claims["iat"]), UTC) if claims.get("iat") else None,
        acting_on_behalf_of=str(claims["act"]) if claims.get("act") else None,
        canal=Canal(str(canal_bruto)) if canal_bruto in set(Canal) else None,
        jti=str(claims["jti"]) if claims.get("jti") else None,
    )
    if identidad.acr is NivelAseguramiento.LOA_ASESOR and not identidad.acting_on_behalf_of:
        # La especificación lo exige y `RegistroAuditoria.emitir` también lo exige:
        # un evento LOA_ASESOR sin `acting_on_behalf_of` lanza ValueError. Se corta aquí,
        # en el borde, con un error de negocio legible.
        raise ErrorApi(
            status.HTTP_403_FORBIDDEN,
            "ACTOR_REQUERIDO",
            "el token de asesor no declara acting_on_behalf_of; sin él no se puede "
            "registrar a nombre de quién se consulta",
            nivel_requerido=NivelAseguramiento.LOA_ASESOR,
        )
    return identidad


def identidad_actual(
    request: Request,
    credenciales: Annotated[HTTPAuthorizationCredentials | None, Depends(_esquema_bearer)],
    ajustes: Annotated[Ajustes, Depends(obtener_ajustes)],
) -> Identidad:
    """Dependencia base: exige un ``Authorization: Bearer <jwt>`` válido."""
    if credenciales is None or not credenciales.credentials:
        raise ErrorApi(
            status.HTTP_401_UNAUTHORIZED,
            "TOKEN_AUSENTE",
            "esta operación exige autenticación: envíe 'Authorization: Bearer <jwt>'",
            cabeceras={"WWW-Authenticate": "Bearer"},
        )
    identidad = _decodificar(credenciales.credentials, ajustes)
    # Se deja en el `state` para que el registro de auditoría y el manejador de errores
    # puedan anotar quién hizo la petición sin volver a decodificar el token.
    request.state.identidad = identidad
    return identidad


def requiere_nivel(
    minimo: NivelAseguramiento,
) -> Callable[[Identidad], Identidad]:
    """Fabrica la dependencia que exige un nivel mínimo de aseguramiento.

    Uso::

        @router.get("/v1/hechos")
        def hechos(identidad: Annotated[Identidad, Depends(requiere_nivel(LOA2))]): ...

    Args:
        minimo: nivel mínimo del recurso según la matriz de la sección 9.

    Returns:
        Una dependencia que devuelve la :class:`Identidad` o lanza
        ``403 NIVEL_INSUFICIENTE`` con el nivel exigido en el cuerpo.
    """

    def dependencia(
        identidad: Annotated[Identidad, Depends(identidad_actual)],
    ) -> Identidad:
        if not nivel_alcanza(identidad.acr, minimo):
            raise nivel_insuficiente(identidad.acr, minimo)
        return identidad

    dependencia.__name__ = f"requiere_{str(minimo).lower()}"
    dependencia.__doc__ = f"Exige nivel de aseguramiento {minimo} o superior."
    return dependencia


def cuenta_autorizada(identidad: Identidad, cuenta_pedida: str | None) -> str:
    """Devuelve la cuenta sobre la que se puede operar.

    **El ``account_ref`` sale siempre del token.** ``cuenta_pedida`` (query o cuerpo) se
    admite solo como redundancia explícita del cliente: si coincide, se ignora; si no
    coincide, es un acceso cruzado y se rechaza.

    Raises:
        ErrorApi: 403 ``CUENTA_NO_AUTORIZADA`` si difieren.
    """
    del_token = identidad.cuenta_ref
    if cuenta_pedida and cuenta_pedida.strip() and cuenta_pedida.strip() != del_token:
        _LOG.warning(
            "acceso cruzado rechazado: token de %s pidió la cuenta %s",
            del_token,
            cuenta_pedida.strip(),
        )
        raise cuenta_no_autorizada(cuenta_pedida.strip(), del_token)
    return del_token


# --------------------------------------------------------------------------- #
# Redacción por nivel — LOA1 ve dirección, no importes
# --------------------------------------------------------------------------- #
def _direccion(delta_cent: int) -> str:
    """Palabra que describe el sentido de la variación, sin cifra alguna."""
    if delta_cent > 0:
        return "subió"
    if delta_cent < 0:
        return "bajó"
    return "se mantuvo igual"


def _sanear_texto(texto: str) -> str:
    """Neutraliza cualquier cifra del texto (garantía: no queda ni un dígito)."""
    saneado, _ = sanear(texto)
    return saneado


def redactar_para_nivel(
    respuesta: RespuestaCanalAgnostica,
    nivel: NivelAseguramiento,
    *,
    factset: FactSet | None = None,
) -> RespuestaCanalAgnostica:
    """Adapta la respuesta al nivel de aseguramiento del solicitante.

    ``LOA2`` y ``LOA_ASESOR`` la reciben íntegra. ``LOA1`` recibe una versión sin una
    sola cifra:

    1. Los bloques ``kv``, ``puente`` y ``tabla`` se eliminan: son importes por
       construcción y no hay forma de "resumirlos" sin números.
    2. Los bloques de texto y los avisos pasan por el saneador, que sustituye montos,
       fechas, porcentajes y cantidades por marcadores (``«un monto»``, ``«una fecha»``).
    3. Se antepone una frase con la **dirección** del cambio y la causa dominante, que
       es exactamente lo que la sección 9 autoriza en este nivel.
    4. Se vacían ``gobernanza.citas`` y ``gobernanza.aserciones``: sus offsets ya no
       corresponden al texto entregado y, sobre todo, ``asercion.texto_original``
       contiene los importes que este nivel no puede ver. Los **contadores** se
       conservan, porque son la prueba de verificación y no revelan ninguna cifra.
    5. ``derivacion.resumen_asesor`` también se sanea: el brief del asesor lleva
       importes.

    Args:
        respuesta: respuesta completa ya verificada.
        nivel: nivel del solicitante.
        factset: hechos del turno; si se pasa, permite nombrar la dirección y la causa.

    Returns:
        La respuesta original si el nivel ve importes, o una copia redactada si no.
    """
    if nivel in NIVELES_CON_MONTOS:
        return respuesta

    bloques: list[Bloque] = [BloqueAviso(severidad="info", texto=AVISO_LOA1)]
    narrados: list[Bloque] = []
    for bloque in respuesta.bloques:
        if bloque.tipo in {"kv", "puente", "tabla"}:
            continue
        titulo = _sanear_texto(bloque.titulo) if bloque.titulo else None
        if bloque.tipo == "aviso":
            narrados.append(
                BloqueAviso(
                    titulo=titulo,
                    severidad=bloque.severidad,
                    texto=_sanear_texto(bloque.texto),
                    fact_ids=[],
                )
            )
        else:  # texto
            narrados.append(
                BloqueTexto(
                    titulo=titulo,
                    texto=_sanear_texto(bloque.texto),
                    enfasis=bloque.enfasis,
                    fact_ids=[],
                )
            )

    # El resumen sintético es una RED DE SEGURIDAD, no un encabezado: garantiza que en
    # LOA1 el cliente sepa al menos si su recibo subió o bajó y por qué, incluso si el
    # saneado dejara la narración vacía. Añadirlo SIEMPRE hacía que el mensaje empezara
    # tres veces —el aviso del canal, este resumen y el resumen del propio modelo— con la
    # misma idea escrita distinto. Solo entra cuando no queda nada narrado.
    if factset is not None and not any(b.tipo == "texto" for b in narrados):
        causa = factset.causa_dominante()
        motivo = f" El motivo principal es {causa.etiqueta_cliente}." if causa else ""
        bloques.append(
            BloqueTexto(
                texto=(
                    f"Su recibo de este mes {_direccion(factset.delta_total_cent)} "
                    f"respecto del mes anterior.{motivo}"
                ),
                fact_ids=["factset:delta_total_cent"],
            )
        )
    bloques.extend(narrados)

    gobernanza = Gobernanza(
        **respuesta.gobernanza.model_dump(exclude={"citas", "aserciones"}),
        citas=[],
        aserciones=[],
    )
    derivacion = Derivacion(
        **respuesta.derivacion.model_dump(exclude={"resumen_asesor", "motivo"}),
        motivo=_sanear_texto(respuesta.derivacion.motivo) if respuesta.derivacion.motivo else None,
        resumen_asesor=(
            _sanear_texto(respuesta.derivacion.resumen_asesor)
            if respuesta.derivacion.resumen_asesor
            else None
        ),
    )
    telemetria = dict(respuesta.telemetria)
    telemetria["redactado_por_nivel"] = str(nivel)
    return RespuestaCanalAgnostica(
        conversation_id=respuesta.conversation_id,
        trace_id=respuesta.trace_id,
        bloques=bloques,
        acciones=respuesta.acciones,
        derivacion=derivacion,
        gobernanza=gobernanza,
        telemetria=telemetria,
    )
