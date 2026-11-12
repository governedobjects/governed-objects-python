# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Non-authoritative mandate index projection over canonical Governed Objects."""

from __future__ import annotations

from governed_objects.delegation.mandate import DelegatedMandate
from governed_objects.delegation.resolver import MANDATE_OBJECT_TYPE
from governed_objects.persistence.sqlite import SQLiteStore


class MandateProjection:
    """Optional lookup index — not source of truth.

    Canonical mandate state lives in Governed Object persistence.
    This projection can be deleted and rebuilt without changing authorization.
    """

    def __init__(self, store: SQLiteStore, *, resolver=None) -> None:
        self._store = store
        self._resolver = resolver
        self._cache: dict[str, DelegatedMandate] = {}

    def rebuild(self) -> int:
        self._cache.clear()
        count = 0
        for obj in self._store.list_all():
            if obj.type != MANDATE_OBJECT_TYPE:
                continue
            self.get(obj.id)
            count += 1
        return count

    def get(self, mandate_id: str) -> DelegatedMandate | None:
        if self._resolver is None:
            raise ValueError('delegation configuration unavailable')
        mandate = self._resolver.resolve(mandate_id)
        if mandate is None:
            return None
        self._cache[mandate_id] = mandate
        return mandate

    def invalidate(self, mandate_id: str) -> None:
        self._cache.pop(mandate_id, None)

    def sync(self, mandate_id: str) -> None:
        self.invalidate(mandate_id)
        self.get(mandate_id)
