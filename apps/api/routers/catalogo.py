"""``GET /v1/catalogo`` — definiciones de conceptos en lenguaje de cliente.

Es el único recurso accesible con **LOA0**: no contiene ni un dato del cliente, solo el
catálogo de conceptos con su explicación *"categorizando los motivos de consulta en
lenguaje cliente alineado al de la atención humana Movistar (ej. prorrateos,
reconexiones)"*, tal como pide la ficha.

El texto que se devuelve viene del corpus RAG y por tanto pasa por el saneador: las
cifras de ejemplo del catálogo (*"por ejemplo S/ 49,90"*) se sustituyen por marcadores.
Un importe de un documento genérico no es un importe de este cliente y no puede salir de
aquí como si lo fuera.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import RecuperadorDep, ReglasDep
from apps.api.errores import no_encontrado
from apps.api.security import Identidad, requiere_nivel
from packages.core_domain.enums import CausaOficial, FamiliaConcepto, NivelAseguramiento
from packages.core_domain.esquemas.recibo import ConceptoCatalogo

__all__ = ["FichaConcepto", "router"]

router = APIRouter(prefix="/v1/catalogo", tags=["catalogo"])


class FichaConcepto(BaseModel):
    """Ficha de un concepto tal como se le muestra al cliente."""

    model_config = ConfigDict(extra="forbid")

    concepto_id: str
    nombre_comercial: str
    familia: FamiliaConcepto
    definicion_cliente: str
    causa_oficial: CausaOficial | None = None
    causas_permitidas: list[str] = Field(default_factory=list)
    sinonimos: list[str] = Field(default_factory=list)
    ejemplo_variacion: str | None = None
    prorrateable: bool = False
    afecto_igv: bool = True
    detalle_rag: str | None = Field(
        default=None, description="Texto ampliado del corpus, ya saneado de cifras"
    )
    cifras_retiradas: list[str] = Field(
        default_factory=list,
        description="Marcadores que el saneador puso en lugar de las cifras del corpus",
    )


class ResumenConcepto(BaseModel):
    """Entrada del listado del catálogo."""

    model_config = ConfigDict(extra="forbid")

    concepto_id: str
    nombre_comercial: str
    familia: FamiliaConcepto
    causa_oficial: CausaOficial | None = None


def _ficha(concepto: ConceptoCatalogo, detalle: str | None, retirados: list[str]) -> FichaConcepto:
    """Arma la ficha combinando el catálogo de reglas con el texto del corpus."""
    return FichaConcepto(
        concepto_id=concepto.concepto_id,
        nombre_comercial=concepto.nombre_comercial,
        familia=concepto.familia,
        definicion_cliente=concepto.definicion_cliente,
        causa_oficial=concepto.causa_oficial,
        causas_permitidas=[str(causa) for causa in concepto.causas_permitidas],
        sinonimos=list(concepto.sinonimos),
        ejemplo_variacion=concepto.ejemplo_variacion,
        prorrateable=concepto.prorrateable,
        afecto_igv=concepto.afecto_igv,
        detalle_rag=detalle,
        cifras_retiradas=retirados,
    )


@router.get("", summary="Lista de conceptos visibles para el cliente")
def listar(
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA0))],
    reglas: ReglasDep,
    familia: Annotated[FamiliaConcepto | None, Query(description="Filtra por familia")] = None,
) -> list[ResumenConcepto]:
    """Devuelve el catálogo de conceptos que se le pueden nombrar a un cliente."""
    conceptos = reglas.conceptos_por_familia(familia) if familia else reglas.catalogo
    return [
        ResumenConcepto(
            concepto_id=concepto.concepto_id,
            nombre_comercial=concepto.nombre_comercial,
            familia=concepto.familia,
            causa_oficial=concepto.causa_oficial,
        )
        for concepto in sorted(conceptos, key=lambda item: item.concepto_id)
        if concepto.visible_cliente
    ]


@router.get(
    "/{concepto_id}",
    summary="Definición de un concepto en lenguaje de cliente",
    response_model=FichaConcepto,
    responses={404: {"description": "CONCEPTO_NO_ENCONTRADO"}},
)
def obtener_concepto(
    concepto_id: str,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA0))],
    reglas: ReglasDep,
    recuperador: RecuperadorDep,
) -> FichaConcepto:
    """Explica un concepto del recibo sin exponer ningún dato de cuenta.

    Un ``concepto_id`` desconocido es un 404, no una explicación inventada: en la
    conversación ese mismo caso dispara la regla dura *"concepto fuera de catálogo"* y
    deriva a un asesor.
    """
    clave = concepto_id.strip().upper()
    concepto = reglas.concepto(clave)
    if concepto is None:
        raise no_encontrado(
            "CONCEPTO_NO_ENCONTRADO",
            f"el concepto {clave} no está en el catálogo de la versión "
            f"{reglas.rules_version} de las reglas",
            concepto_id=clave,
        )
    detalle: str | None = None
    retirados: list[str] = []
    if recuperador is not None:
        fragmento = recuperador.explicar_concepto(clave)
        if fragmento is not None:
            detalle = fragmento.texto
            retirados = list(fragmento.retirados)
    return _ficha(concepto, detalle, retirados)


@router.get("/{concepto_id}/crudo", summary="Ficha completa del concepto (diagnóstico)")
def obtener_concepto_crudo(
    concepto_id: str,
    identidad: Annotated[Identidad, Depends(requiere_nivel(NivelAseguramiento.LOA0))],
    reglas: ReglasDep,
) -> dict[str, Any]:
    """Vuelca la entrada de ``rules.yaml`` del concepto, incluida su parte técnica.

    Sirve para auditar qué causas admite cada concepto (``regla_concepto_causa``) sin
    abrir el fichero de reglas en el servidor.
    """
    clave = concepto_id.strip().upper()
    concepto = reglas.concepto(clave)
    if concepto is None:
        raise no_encontrado(
            "CONCEPTO_NO_ENCONTRADO", f"el concepto {clave} no existe", concepto_id=clave
        )
    return concepto.model_dump(mode="json")
