"""Aplicación FastAPI de **recibo-claro**.

Monta los routers de la sección 9, abre el CORS solo en desarrollo y normaliza todos los
errores al mismo cuerpo (:class:`~packages.core_domain.esquemas.respuesta.RespuestaError`)
para que App, Bot Lucía y WhatsApp enruten por ``codigo`` y no por el texto.

El ``lifespan`` construye al arrancar lo caro —reglas, índices RAG, proveedor
generativo, clientes del ACL— para que la primera petición no lo pague: la ficha exige
soportar picos de hasta tres veces la volumetría normal, y eso empieza por no construir
un índice vectorial dentro de una petición.

Arranque local::

    uvicorn apps.api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.deps import calentar, cerrar_recursos
from apps.api.errores import ErrorApi
from apps.api.routers import (
    asesor,
    auditoria,
    catalogo,
    derivacion,
    dev,
    evidencia,
    explicar,
    hechos,
    historial,
    live,
    salud,
)
from apps.api.settings import obtener_ajustes, raiz_proyecto
from packages.core_domain.esquemas.respuesta import RespuestaError

__all__ = ["app", "crear_aplicacion"]

_LOG = logging.getLogger(__name__)

DESCRIPCION = """
**Asistente de explicación de recibos Movistar** — Hackathon AI Telecom 2026, Desafío 1.

Explica en lenguaje simple por qué varió el recibo de un cliente comparándolo con los
cinco previos que expone BrainyBill, atribuye la variación a las **nueve causas
oficiales** de la ficha (cambio de plan, equipo financiado, compra de paquetes, cargos
adicionales, promociones vencidas, notas de crédito/débito, prorrateos, reconexiones y
ajustes por días de suspensión), sugiere la siguiente acción y deriva a un asesor humano
**con el contexto cargado** cuando no puede sostener una respuesta.

### Garantías de diseño

* **El modelo generativo no calcula.** Todas las cifras salen de un `FactSet`
  determinístico, conciliado al céntimo y sellado con SHA-256.
* **Verificador numérico en código.** Cada cifra del texto final se ancla contra el
  `FactSet`; una sola sin anclar bloquea la respuesta y dispara la derivación.
* **Invariante de conciliación.** Si la suma de variaciones por concepto no reproduce la
  diferencia entre totales (±1 céntimo), no se explica: se deriva.
* **Autenticación por niveles.** `LOA0` solo ve el catálogo; `LOA1` ve la dirección del
  cambio sin importes; `LOA2` la explicación completa; `LOA_ASESOR` actúa a nombre de
  una cuenta y queda registrado.
* **Auditoría encadenada.** Cada turno deja una cadena de eventos con hash verificable
  en `GET /v1/auditoria`.

### Recorrido de la demo

1. `POST /dev/token` — obtener un token (solo con `ENTORNO=dev`).
2. `GET /v1/hechos` — ver los hechos conciliados del periodo.
3. `POST /v1/explicar` — la explicación verificada, lista para cualquier canal.
4. `GET /v1/evidencia/{explicacion_id}` — de dónde salió cada afirmación.
5. `GET /v1/auditoria?trace_id=...` — la prueba en el log.
6. `POST /dev/alucinar` — el caso adversario: cifra inventada, verificador que la caza.
"""

ETIQUETAS_OPENAPI: list[dict[str, Any]] = [
    {"name": "salud", "description": "Liveness, readiness y conectividad con BrainyBill/Amdocs."},
    {"name": "hechos", "description": "FactSet conciliado: la única fuente de cifras."},
    {"name": "explicar", "description": "Explicación verificada, canal-agnóstica."},
    {"name": "evidencia", "description": "Respaldo de cada afirmación entregada."},
    {"name": "derivacion", "description": "Hand-off a asesor humano con contexto."},
    {"name": "auditoria", "description": "Bitácora encadenada del turno."},
    {"name": "catalogo", "description": "Conceptos del recibo en lenguaje de cliente."},
    {"name": "desarrollo", "description": "Utilidades de demo. Solo con ENTORNO=dev."},
]


@asynccontextmanager
async def ciclo_vida(aplicacion: FastAPI) -> AsyncIterator[None]:
    """Calienta las dependencias al arrancar y las cierra al apagar."""
    ajustes = obtener_ajustes()
    logging.basicConfig(
        level=logging.DEBUG if ajustes.es_desarrollo else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    _LOG.info("arrancando recibo-claro con %s", ajustes.resumen())
    estado = calentar(ajustes)
    aplicacion.state.arranque = estado
    _LOG.info("dependencias listas: %s", estado)
    try:
        yield
    finally:
        cerrar_recursos()
        _LOG.info("recibo-claro apagado")


def crear_aplicacion() -> FastAPI:
    """Construye la aplicación con sus routers, su CORS y sus manejadores de error."""
    ajustes = obtener_ajustes()
    aplicacion = FastAPI(
        title="recibo-claro · Explicación inteligente de recibos Movistar",
        summary=(
            "Explica la variación del recibo con cero invenciones financieras, "
            "verificadas en código y comprobables por logs."
        ),
        description=DESCRIPCION,
        version="0.1.0",
        openapi_tags=ETIQUETAS_OPENAPI,
        contact={"name": "Equipo recibo-claro", "email": "concursos-cis@ulima.edu.pe"},
        license_info={"name": "MIT"},
        lifespan=ciclo_vida,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    origenes = ajustes.origenes_cors
    if origenes:
        # En desarrollo el CORS va abierto para poder probar desde cualquier front o
        # herramienta; en el resto de entornos exige CORS_ORIGENES explícito.
        aplicacion.add_middleware(
            CORSMiddleware,
            allow_origins=origenes,
            allow_credentials="*" not in origenes,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Trace-Id", "X-Degradado"],
        )

    aplicacion.include_router(salud.router)
    aplicacion.include_router(catalogo.router)
    aplicacion.include_router(hechos.router)
    aplicacion.include_router(historial.router)
    aplicacion.include_router(explicar.router)
    aplicacion.include_router(evidencia.router)
    aplicacion.include_router(derivacion.router)
    aplicacion.include_router(asesor.router)
    aplicacion.include_router(auditoria.router)
    aplicacion.include_router(live.router)
    if ajustes.es_desarrollo:
        aplicacion.include_router(dev.router)
        _LOG.warning("router /dev montado: emite tokens de prueba. ENTORNO=%s", ajustes.entorno)

    _montar_interfaz(aplicacion)
    _registrar_manejadores(aplicacion)
    return aplicacion


def _montar_interfaz(aplicacion: FastAPI) -> None:
    """Publica la consola de demostración (``apps/web/estatico``) en ``/ui``.

    Por qué se sirve desde la propia API y no aparte:

    * **Cero fricción de instalación.** La consola es HTML, CSS y JavaScript de
      navegador: sin ``npm``, sin ``node_modules`` y sin compilación. Montarla aquí hace
      que ``uvicorn apps.api.main:app`` sea lo único que hay que arrancar para la demo, y
      que funcione en una sala sin internet.
    * **Mismo origen que la API.** La página llama a ``/v1/*`` y a ``/dev/*`` sin CORS ni
      preflight, así que no hace falta tocar la configuración de CORS: la de desarrollo
      (``*`` con ``expose_headers`` de ``X-Trace-Id`` y ``X-Degradado``) ya cubre además
      el caso de abrir la consola contra otra instancia con ``/ui/?api=http://host:puerto``.
    * **No toca el contrato.** ``mount`` registra un ``Mount`` de Starlette, no una
      ``APIRoute``: no entra en ``/openapi.json`` y por tanto no mueve el snapshot de
      ``tests/contract/test_openapi_snapshot.py``. La superficie que consumen la App, el
      Bot Lucía y WhatsApp sigue siendo exactamente la misma.

    Si el directorio no existe (imagen de despliegue que no copia ``apps/web``), no se
    monta nada y la API arranca igual: la interfaz es andamiaje de demostración, no una
    dependencia del servicio.
    """
    raiz_web = raiz_proyecto() / "apps" / "web"
    compilado = raiz_web / "dist"
    legado = raiz_web / "estatico"
    directorio = compilado if (compilado / "index.html").is_file() else legado
    if not directorio.is_dir():
        _LOG.info("no se monta /ui: no existe el directorio %s", directorio)
        return
    # html=True hace que "/ui/" sirva index.html y que las rutas sin extensión no den 404.
    aplicacion.mount("/ui", StaticFiles(directory=directorio, html=True), name="ui")
    _LOG.info("consola de demostración montada en /ui desde %s", directorio)


def _registrar_manejadores(aplicacion: FastAPI) -> None:
    """Normaliza todos los errores al cuerpo ``RespuestaError``."""

    @aplicacion.exception_handler(ErrorApi)
    async def _error_api(peticion: Request, error: ErrorApi) -> JSONResponse:
        """Errores de negocio: el cuerpo ya viene construido con su código estable."""
        cuerpo = error.cuerpo
        if cuerpo.trace_id is None:
            cuerpo = cuerpo.model_copy(update={"trace_id": peticion.headers.get("X-Trace-Id")})
        return JSONResponse(
            status_code=error.status_code,
            content=cuerpo.model_dump(mode="json"),
            headers=error.headers,
        )

    @aplicacion.exception_handler(StarletteHTTPException)
    async def _http(peticion: Request, error: StarletteHTTPException) -> JSONResponse:
        """404 de ruta, 405 y demás: mismo cuerpo, código genérico."""
        detalle = error.detail
        if isinstance(detalle, dict) and "codigo" in detalle:
            return JSONResponse(status_code=error.status_code, content=detalle)
        return JSONResponse(
            status_code=error.status_code,
            content=RespuestaError(
                codigo=f"HTTP_{error.status_code}", detalle=str(detalle)
            ).model_dump(mode="json"),
            headers=getattr(error, "headers", None),
        )

    @aplicacion.exception_handler(RequestValidationError)
    async def _validacion(peticion: Request, error: RequestValidationError) -> JSONResponse:
        """Cuerpo o query mal formados. ``extra="forbid"`` hace que un campo de más falle."""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=RespuestaError(
                codigo="PETICION_INVALIDA",
                detalle="la petición no cumple el contrato del endpoint",
                datos={"errores": error.errors()[:10]},
            ).model_dump(mode="json"),
        )

    @aplicacion.exception_handler(Exception)
    async def _inesperado(peticion: Request, error: Exception) -> JSONResponse:
        """Último recinto: se registra entero y al cliente se le da un código estable.

        No se filtra la traza al cuerpo: quien la necesita la tiene en el log del
        servidor y en la bitácora de auditoría.
        """
        _LOG.exception("error no controlado en %s %s", peticion.method, peticion.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=RespuestaError(
                codigo="ERROR_INTERNO",
                detalle="ocurrió un error inesperado; el incidente quedó registrado",
            ).model_dump(mode="json"),
        )


app = crear_aplicacion()
