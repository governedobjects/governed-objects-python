# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Type-driven authority rules for external observations."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition


class AuthorityMode(StrEnum):
    PROVIDER_AUTO_ACCEPT = "PROVIDER_AUTO_ACCEPT"
    CANONICAL_AUTHORITATIVE = "CANONICAL_AUTHORITATIVE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class AuthorityFieldRule(BaseModel):
    mode: AuthorityMode


class AuthorityRules(BaseModel):
    fields: dict[str, AuthorityFieldRule] = Field(default_factory=dict)


class AuthorityRuleResolver:
    """Resolve field authority with type inheritance."""

    def __init__(self, type_registry: TypeRegistry) -> None:
        self._types = type_registry

    def rules_for(self, object_type: str) -> AuthorityRules:
        return self.rules_for_definitions(tuple(self._inheritance_chain(object_type)))

    @staticmethod
    def rules_for_definitions(definitions: tuple[ObjectTypeDefinition, ...]) -> AuthorityRules:
        """Merge already captured parent-first definitions, with no registry I/O."""
        merged: dict[str, AuthorityFieldRule] = {}
        for type_def in definitions:
            raw = type_def.extra.get("authority") or {}
            fields = raw.get("fields") or {}
            for field_name, spec in fields.items():
                if isinstance(spec, dict):
                    merged[field_name] = AuthorityFieldRule(mode=AuthorityMode(spec["mode"]))
                else:
                    merged[field_name] = AuthorityFieldRule(mode=AuthorityMode(str(spec)))
        return AuthorityRules(fields=merged)

    def mode_for_field(self, object_type: str, field_name: str) -> AuthorityMode | None:
        return self.rules_for(object_type).fields.get(field_name, AuthorityFieldRule(mode=AuthorityMode.REQUIRES_REVIEW)).mode

    def _inheritance_chain(self, object_type: str) -> list[ObjectTypeDefinition]:
        chain: list[ObjectTypeDefinition] = []
        current = self._types.get(object_type)
        while current.kind not in {t.kind for t in chain}:
            chain.append(current)
            if not current.extends:
                break
            current = self._types.get(current.extends)
        return list(reversed(chain))
