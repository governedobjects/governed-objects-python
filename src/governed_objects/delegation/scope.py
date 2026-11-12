# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Generic mandate scope evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from governed_objects.domain.models import GovernedObject
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.relationships.canonical import list_canonical_for_object


@dataclass(frozen=True)
class RelationshipConstraint:
    relation: str
    target_id: str


@dataclass(frozen=True)
class MandateScope:
    object_ids: frozenset[str] = frozenset()
    object_types: frozenset[str] = frozenset()
    relationship_constraints: tuple[RelationshipConstraint, ...] = ()

    def __post_init__(self):
        for values in (self.object_ids, self.object_types):
            if not isinstance(values, frozenset) or any(not isinstance(value, str) or not value for value in values):
                raise ValueError('invalid mandate scope')
        if not isinstance(self.relationship_constraints, tuple) or any(
            not isinstance(value, RelationshipConstraint)
            or not isinstance(value.relation, str) or not value.relation
            or not isinstance(value.target_id, str) or not value.target_id
            for value in self.relationship_constraints
        ):
            raise ValueError('invalid mandate scope')

    def is_empty(self) -> bool:
        return (
            not self.object_ids
            and not self.object_types
            and not self.relationship_constraints
        )


@dataclass(frozen=True)
class EffectiveScope:
    """Conjunction of declarations, never a flattened declaration or saved data."""

    bounds: tuple[MandateScope, ...]

    def is_empty(self) -> bool:
        return all(bound.is_empty() for bound in self.bounds)


def scope_allows(
    scope: MandateScope | EffectiveScope,
    obj: GovernedObject,
    store: SQLiteStore,
) -> bool:
    if isinstance(scope, EffectiveScope):
        return all(scope_allows(bound, obj, store) for bound in scope.bounds)
    if scope.is_empty():
        return True
    if obj.id in scope.object_ids:
        return True
    if obj.type in scope.object_types:
        return True
    if scope.relationship_constraints:
        relationships = list_canonical_for_object(store, obj.id)
        edges = {(r.relation, r.target_id) for r in relationships if r.source_id == obj.id}
        for constraint in scope.relationship_constraints:
            if (constraint.relation, constraint.target_id) in edges:
                return True
    return False
