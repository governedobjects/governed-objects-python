# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Materialize DelegatedMandate dataclass from canonical Governed Objects."""

from __future__ import annotations

from datetime import datetime

from governed_objects.delegation.mandate import DelegatedMandate, MandateStatus
from governed_objects.delegation.scope import MandateScope, RelationshipConstraint
from governed_objects.domain.models import GovernedObject, Relationship
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.relationships.canonical import list_canonical_for_object
from governed_objects.relationships.vocabulary import (
    DELEGATED_BY,
    DELEGATED_TO,
    DERIVED_FROM,
    SCOPED_TO,
)


def materialize_mandate(
    obj: GovernedObject,
    store: SQLiteStore,
    *,
    scope_decoder=None,
    relationships: list[Relationship] | None = None,
) -> DelegatedMandate:
    """Build mandate view from stored GO and canonical relationships."""
    if set(obj.data) - {'capabilities', 'purposes', 'scope', 'valid_from', 'valid_until', 'parent_mandate_id'}:
        raise ValueError('unsupported mandate data')
    relationships = list_canonical_for_object(store, obj.id) if relationships is None else relationships
    rel_map: dict[str, list[str]] = {}
    for rel in relationships:
        if rel.source_id != obj.id:
            raise ValueError('invalid mandate relationship source')
        rel_map.setdefault(rel.relation, []).append(rel.target_id)

    principal_id = _single(rel_map.get(DELEGATED_BY, []), field="delegated_by")
    delegate_actor_id = _single(rel_map.get(DELEGATED_TO, []), field="delegated_to")
    parent_field = obj.data.get('parent_mandate_id')
    if parent_field is not None and (not isinstance(parent_field, str) or not parent_field):
        raise ValueError('invalid parent mandate')
    parent_relation = _optional_single(rel_map.get(DERIVED_FROM, []))
    if parent_field is not None and parent_relation is not None and parent_field != parent_relation:
        raise ValueError('conflicting parent mandate')
    parent_mandate_id = parent_field if parent_field is not None else parent_relation

    scope = _build_scope(obj, rel_map.get(SCOPED_TO, []), store, scope_decoder)

    valid_from = _parse_dt(obj.data.get("valid_from"))
    valid_until = _parse_dt(obj.data.get("valid_until"))
    if valid_from is not None and valid_until is not None and valid_from > valid_until:
        raise ValueError('invalid validity interval')

    status = _map_status(obj.state)

    return DelegatedMandate(
        id=obj.id,
        principal_id=principal_id,
        delegate_actor_id=delegate_actor_id,
        capabilities=_strings(obj.data.get('capabilities'), required=True),
        scope=scope,
        purposes=_strings(obj.data.get('purposes', [])),
        valid_from=valid_from,
        valid_until=valid_until,
        status=status,
        parent_mandate_id=parent_mandate_id,
        version=obj.version,
    )


def _build_scope(
    obj: GovernedObject,
    scoped_to: list[str],
    store: SQLiteStore,
    scope_decoder=None,
) -> MandateScope:
    raw = obj.data.get('scope', {})
    if not isinstance(raw, dict) or set(raw) - {'object_ids', 'object_types', 'relationship_constraints'}:
        raise ValueError('unsupported mandate scope')
    if raw:
        raw_constraints = raw.get('relationship_constraints', [])
        if not isinstance(raw_constraints, list) or any(
            not isinstance(c, dict) or set(c) != {'relation', 'target_id'}
            or any(not isinstance(value, str) or not value for value in c.values())
            for c in raw_constraints
        ):
            raise ValueError('invalid relationship constraint')
        constraints = tuple(
            RelationshipConstraint(relation=c["relation"], target_id=c["target_id"])
            for c in raw_constraints
        )
        object_ids = _strings(raw.get('object_ids', []))
        if scoped_to and set(scoped_to) != set(object_ids) | {c.target_id for c in constraints}:
            raise ValueError('conflicting scope encodings')
        return MandateScope(
            object_ids=object_ids,
            object_types=_strings(raw.get('object_types', [])),
            relationship_constraints=constraints,
        )

    decoder = scope_decoder or reject_legacy_scope
    scope = decoder(tuple(scoped_to), store)
    if not isinstance(scope, MandateScope):
        raise ValueError('unsupported scope decoder result')
    return scope


def reject_legacy_scope(scoped_to, store) -> MandateScope:
    if scoped_to:
        raise ValueError('legacy scope requires an explicit interpreter')
    return MandateScope()


def _map_status(state: str) -> MandateStatus:
    if state == "draft":
        return MandateStatus.DRAFT
    if state == "revoked":
        return MandateStatus.REVOKED
    if state == 'active':
        return MandateStatus.ACTIVE
    raise ValueError('unsupported mandate status')


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError('invalid mandate time')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('mandate time requires timezone')
    return result


def _single(values: list[str], *, field: str) -> str:
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise ValueError(f"mandate requires one relationship: {field}")
    return values[0]


def _optional_single(values: list[str]) -> str | None:
    return _single(values, field='derived_from') if values else None


def _strings(value, *, required=False) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError('invalid mandate string list')
    if required and not value:
        raise ValueError('mandate capabilities required')
    return frozenset(value)
