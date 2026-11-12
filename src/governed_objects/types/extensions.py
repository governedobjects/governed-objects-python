# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Event reducer protocol and type extension registry."""

from __future__ import annotations

from typing import Protocol

from governed_objects.domain.models import GovernedObject, GovernanceEvent
from governed_objects.types.canonicalizers.base import TypeCanonicalizer


class EventReducer(Protocol):
    def reduce(
        self,
        obj: GovernedObject | None,
        event: GovernanceEvent,
    ) -> GovernedObject | None: ...


class TypeExtensionRegistry:
    """Registers type-specific extension points (reducers, canonicalizers)."""

    def __init__(self) -> None:
        self._reducers: dict[str, EventReducer] = {}
        self._canonicalizers: dict[str, TypeCanonicalizer] = {}

    def register_reducer(self, object_type: str, reducer: EventReducer) -> None:
        self._reducers[object_type] = reducer

    def register_canonicalizer(self, object_type: str, canonicalizer: TypeCanonicalizer) -> None:
        self._canonicalizers[object_type] = canonicalizer

    def reducer_for(self, object_type: str) -> EventReducer | None:
        return self._reducers.get(object_type)

    def canonicalizer_for(self, object_type: str, type_registry=None) -> TypeCanonicalizer | None:
        if object_type in self._canonicalizers:
            return self._canonicalizers[object_type]
        if type_registry is None:
            return None
        try:
            type_def = type_registry.get(object_type)
        except KeyError:
            return None
        if type_def.extends:
            return self.canonicalizer_for(type_def.extends, type_registry)
        return None
