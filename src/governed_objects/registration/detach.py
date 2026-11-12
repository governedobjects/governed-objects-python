# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Detach a governed object from its provider backing."""

from __future__ import annotations

from governed_objects.adapters.context import AdapterContext
from governed_objects.domain.models import GovernedObject
from governed_objects.materialization.service import MaterializationService
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.errors import ProviderError


def detach_from_provider(
    store: SQLiteStore,
    materializer: MaterializationService,
    context: AdapterContext,
    object_id: str,
) -> GovernedObject | None:
    """Remove provider binding; canonical accepted state is already org-owned.

    Detach changes only operational metadata. It records no revision and
    leaves the Object Version unchanged, so it cannot exhaust the version
    space and does not invalidate a current expected version."""
    obj = store.get(object_id)
    if obj is None:
        return None

    try:
        materializer.materialize(object_id, context, hydrate=True, persist=True)
    except ProviderError:
        pass

    binding = ProviderBinding.from_metadata(obj.metadata)
    metadata = dict(obj.metadata)
    metadata.pop("provider_binding", None)
    if binding:
        metadata["detached_from"] = binding.model_dump(exclude_none=True)

    detached = obj.model_copy(update={"metadata": metadata})
    store.save(detached)
    return detached
