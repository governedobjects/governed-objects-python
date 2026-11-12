# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Relationship synchronization during hydration."""

from __future__ import annotations

from contextlib import nullcontext

from governed_objects.domain.models import Relationship
from governed_objects.persistence.sqlite import SQLiteStore

PROVIDER_SOURCE = "provider"
LOCAL_SOURCE = "local"


class RelationshipSyncService:
    """Maintain provider-owned outgoing derived cache rows, not canonical admission."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def sync_provider_relationships(
        self,
        governed_id: str,
        relationships: list[Relationship],
        *,
        provider_name: str,
    ) -> None:
        """Apply a named provider's outgoing snapshot without taking occupied slots."""
        if not provider_name:
            return
        prepared = [rel.model_copy(deep=True) for rel in relationships if rel.source_id == governed_id]
        # Ownership must be inspected only after acquiring the shared connection
        # guard. A foreign owner finishes first; a legitimate same-owner caller
        # participates without ending or committing that caller's transaction.
        with self._store._connection_guard:
            unit = nullcontext() if self._store._observation_transaction_active else self._store.observation_transaction()
            with unit:
                protected = set()
                for rel in self._store.list_for_source(governed_id):
                    if rel.metadata.get("source") != PROVIDER_SOURCE or rel.metadata.get("provider") != provider_name:
                        protected.add((rel.source_id, rel.relation, rel.target_id))
                        continue
                    self._store.remove(rel.source_id, rel.relation, rel.target_id)
                for rel in prepared:
                    if (rel.source_id, rel.relation, rel.target_id) in protected:
                        continue
                    tagged = rel.model_copy(
                        update={
                            "metadata": {
                                **rel.metadata,
                                "source": PROVIDER_SOURCE,
                                "provider": provider_name,
                            }
                        }
                    )
                    self._store.add(tagged)

    def add_local_relationship(self, rel: Relationship) -> None:
        tagged = rel.model_copy(
            update={"metadata": {**rel.metadata, "source": LOCAL_SOURCE}}
        )
        self._store.add(tagged)
