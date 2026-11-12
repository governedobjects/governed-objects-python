# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Canonical (accepted) relationship helpers."""

from __future__ import annotations

from governed_objects.domain.models import Relationship
from governed_objects.persistence.sqlite import SQLiteStore

CANONICAL_SOURCE = "canonical"
PROVIDER_SOURCE = "provider"
LOCAL_SOURCE = "local"


def is_canonical(rel: Relationship) -> bool:
    return rel.metadata.get("source") == CANONICAL_SOURCE


def canonical_relationship(
    source_id: str,
    relation: str,
    target_id: str,
    *,
    target_type: str | None = None,
    extra_metadata: dict | None = None,
) -> Relationship:
    metadata = {"source": CANONICAL_SOURCE}
    if extra_metadata:
        metadata.update(extra_metadata)
    return Relationship(
        source_id=source_id,
        relation=relation,
        target_id=target_id,
        target_type=target_type,
        metadata=metadata,
    )


def list_canonical_for_object(store: SQLiteStore, object_id: str) -> list[Relationship]:
    rels = store.list_for_source(object_id)
    return [r for r in rels if is_canonical(r)]


def replace_canonical_for_object(
    store: SQLiteStore,
    object_id: str,
    relationships: list[Relationship],
) -> None:
    """Replace accepted canonical outgoing edges for an object."""
    existing = store.list_for_source(object_id)
    for rel in existing:
        if is_canonical(rel):
            store.remove(rel.source_id, rel.relation, rel.target_id)
    for rel in relationships:
        store.add(rel)
