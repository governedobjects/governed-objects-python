# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Adapter registry — resolves the adapter for a governed object."""

from __future__ import annotations

from governed_objects.adapters.base import TypeAdapter
from governed_objects.domain.models import GovernedObject


class AdapterRegistry:
    def __init__(self, adapters: list[TypeAdapter]) -> None:
        self._adapters = adapters

    def resolve(self, obj: GovernedObject) -> TypeAdapter:
        for adapter in self._adapters:
            if adapter.supports(obj.type):
                return adapter
        raise KeyError(f"no adapter for type: {obj.type}")

    def for_type(self, object_type: str) -> TypeAdapter | None:
        for adapter in self._adapters:
            if adapter.supports(object_type):
                return adapter
        return None
