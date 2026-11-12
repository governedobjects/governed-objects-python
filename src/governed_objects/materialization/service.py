# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Unified canonical object materialization — no silent provider mutation."""

from __future__ import annotations

from governed_objects.adapters.context import AdapterContext, HydratedObject
from governed_objects.adapters.registry import AdapterRegistry
from governed_objects.domain.models import Actor, GovernedObject
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.relationships.sync import RelationshipSyncService
from governed_objects.types.registry import TypeRegistry


class MaterializationService:
    """Produce canonical view; provider freshness is observed, not silently accepted."""

    def __init__(
        self,
        store: SQLiteStore,
        adapter_registry: AdapterRegistry,
        type_registry: TypeRegistry,
        rel_sync: RelationshipSyncService | None = None,
    ) -> None:
        self._store = store
        self._adapters = adapter_registry
        self._types = type_registry
        self._rel_sync = rel_sync or RelationshipSyncService(store)

    def materialize_stored(
        self,
        stored: GovernedObject,
        context: AdapterContext,
        *,
        hydrate: bool = True,
        persist: bool = True,
    ) -> GovernedObject:
        if not hydrate:
            return stored

        adapter = self._adapters.resolve(stored)
        hydrated = adapter.load(stored, context)
        if hydrated is None:
            return stored

        # Hydration observes provider view — never silently mutates canonical data/state.
        meta = dict(stored.metadata)
        meta["_provider_observed"] = {
            "data": hydrated.object.data,
            "state": hydrated.object.state,
            "source": hydrated.source,
            "observed_at": hydrated.observed_at.isoformat(),
            "provider_metadata": hydrated.provider_metadata,
        }
        if hydrated.provider_observation is not None:
            meta["_provider_observed"]["observation"] = hydrated.provider_observation

        result = stored.model_copy(update={"metadata": meta})
        if persist:
            self._store.save(result)
        return result

    def observe_provider(
        self,
        stored: GovernedObject,
        context: AdapterContext,
    ) -> HydratedObject | None:
        """Fetch provider view without persisting canonical business state."""
        adapter = self._adapters.resolve(stored)
        return adapter.load(stored, context)

    def materialize(
        self,
        object_id: str,
        context: AdapterContext,
        actor: Actor | None = None,
        *,
        hydrate: bool = True,
        field_filter: bool = False,
        fields: list[str] | None = None,
        persist: bool = True,
    ) -> GovernedObject | None:
        stored = self._store.get(object_id)
        if stored is None:
            return None

        obj = self.materialize_stored(stored, context, hydrate=hydrate, persist=persist)

        if actor and field_filter:
            obj = self._apply_field_filter(obj, actor, fields or [])
        return obj

    def _apply_field_filter(
        self,
        obj: GovernedObject,
        actor: Actor,
        fields: list[str],
    ) -> GovernedObject:
        adapter = self._adapters.resolve(obj)
        try:
            type_def = self._types.get(obj.type)
        except KeyError:
            return obj
        filtered, _ = adapter.filter_read_data(obj, type_def, actor, fields)
        if filtered == obj.data:
            return obj
        return obj.model_copy(update={"data": filtered})
