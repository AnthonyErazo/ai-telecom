"""Eventos de auditoría con cadena de hashes (append-only, un JSON por línea).

La ficha exige que la tasa de alucinación sea *"cero invenciones financieras
comprobables mediante logs de la terminal"*. La cadena hace que esos logs no se
puedan retocar: cambiar un evento invalida todos los posteriores.

``hash_n = SHA256(hash_{n-1} || json_canonico(evento_n))``
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from packages.core_domain.enums import EtapaAuditoria, NivelAseguramiento

__all__ = [
    "HASH_GENESIS",
    "EventoAuditoria",
    "json_canonico",
]

#: Hash inicial de la cadena (no hay evento anterior al primero).
HASH_GENESIS = "0" * 64


def json_canonico(datos: dict[str, Any]) -> str:
    """Serializa un diccionario de forma determinista: claves ordenadas y sin espacios."""
    return json.dumps(datos, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class EventoAuditoria(BaseModel):
    """Una línea del JSONL de auditoría.

    ``payload`` es libre por etapa, pero hay contenidos comprometidos:
    ``FACTS_BUILT`` incluye ``residual_cent`` y ``VERIFY`` incluye la lista completa
    de aserciones con su estado y su fuente.
    """

    model_config = ConfigDict(extra="forbid")

    indice: int = Field(ge=0, description="Posición en la cadena, empezando en 0")
    trace_id: str
    etapa: EtapaAuditoria
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str | None = Field(default=None, description="Componente o usuario que originó el evento")
    cuenta_ref: str | None = Field(default=None, description="Referencia tokenizada, nunca PII")
    acting_on_behalf_of: str | None = Field(
        default=None, description="Obligatorio cuando el actor es un asesor (LOA_ASESOR)"
    )
    nivel: NivelAseguramiento | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    hash_previo: str = HASH_GENESIS
    hash: str = Field(default="", description="SHA-256 de este evento encadenado al anterior")

    def json_canonico(self) -> str:
        """JSON determinista del evento **sin** el campo ``hash``."""
        datos = self.model_dump(mode="json", exclude={"hash"})
        return json_canonico(datos)

    def calcular_hash(self) -> str:
        """``SHA256(hash_previo || json_canonico(evento))`` en hexadecimal."""
        materia = self.hash_previo.encode("utf-8") + self.json_canonico().encode("utf-8")
        return hashlib.sha256(materia).hexdigest()

    def sellar(self) -> Self:
        """Calcula y fija el ``hash`` del evento; devuelve el propio evento."""
        self.hash = self.calcular_hash()
        return self

    def verificar(self) -> bool:
        """Comprueba que el ``hash`` almacenado corresponde al contenido del evento."""
        return bool(self.hash) and self.hash == self.calcular_hash()

    @classmethod
    def encadenar(
        cls,
        *,
        indice: int,
        trace_id: str,
        etapa: EtapaAuditoria,
        payload: dict[str, Any] | None = None,
        hash_previo: str = HASH_GENESIS,
        actor: str | None = None,
        cuenta_ref: str | None = None,
        acting_on_behalf_of: str | None = None,
        nivel: NivelAseguramiento | None = None,
        ts: datetime | None = None,
    ) -> EventoAuditoria:
        """Crea el evento siguiente de la cadena, ya sellado."""
        evento = cls(
            indice=indice,
            trace_id=trace_id,
            etapa=etapa,
            payload=payload or {},
            hash_previo=hash_previo,
            actor=actor,
            cuenta_ref=cuenta_ref,
            acting_on_behalf_of=acting_on_behalf_of,
            nivel=nivel,
            **({"ts": ts} if ts is not None else {}),
        )
        return evento.sellar()

    def a_linea_jsonl(self) -> str:
        """Serializa el evento como una línea de JSONL (sin salto de línea final)."""
        return json_canonico(self.model_dump(mode="json"))
