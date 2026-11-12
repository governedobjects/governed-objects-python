# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Generic monotonic delegation validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from governed_objects.delegation.mandate import DelegatedMandate
from governed_objects.delegation.scope import EffectiveScope, MandateScope, scope_allows
from governed_objects.domain.models import Actor
from governed_objects.persistence.sqlite import SQLiteStore


class DelegationViolation(StrEnum):
    CAPABILITY_EXPANSION = "CAPABILITY_EXPANSION"
    SCOPE_EXPANSION = "SCOPE_EXPANSION"
    PURPOSE_EXPANSION = "PURPOSE_EXPANSION"
    TIME_EXPANSION = "TIME_EXPANSION"
    DELEGATION_CYCLE = "DELEGATION_CYCLE"
    DELEGATION_DEPTH_EXCEEDED = "DELEGATION_DEPTH_EXCEEDED"
    PRINCIPAL_CAPABILITY_MISSING = "PRINCIPAL_CAPABILITY_MISSING"


@dataclass
class DelegationValidationResult:
    valid: bool
    violations: list[DelegationViolation] = field(default_factory=list)


@dataclass(frozen=True)
class DelegationSpec:
    capabilities: frozenset[str]
    scope: MandateScope
    purposes: frozenset[str]
    valid_from: datetime | None
    valid_until: datetime | None
    parent_mandate_id: str | None = None
    mandate_id: str | None = None


@dataclass(frozen=True)
class DelegationAuthority:
    capabilities: frozenset[str]
    scope: MandateScope
    purposes: frozenset[str]
    valid_from: datetime | None
    valid_until: datetime | None


class DelegationValidator:
    MAX_DELEGATION_DEPTH = 2

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def validate(
        self,
        *,
        principal: Actor,
        spec: DelegationSpec,
        parent_mandate: DelegatedMandate | None = None,
        ancestor_ids: frozenset[str] | None = None,
        max_depth: int | None = None,
    ) -> DelegationValidationResult:
        violations: list[DelegationViolation] = []

        if spec.mandate_id and spec.parent_mandate_id:
            if spec.mandate_id == spec.parent_mandate_id:
                violations.append(DelegationViolation.DELEGATION_CYCLE)

        limit = self.MAX_DELEGATION_DEPTH if max_depth is None else max_depth
        if parent_mandate is not None and len(ancestor_ids or ()) + 2 > limit:
            violations.append(DelegationViolation.DELEGATION_DEPTH_EXCEEDED)

        if parent_mandate is None:
            ceiling = DelegationAuthority(
                capabilities=frozenset(principal.capabilities),
                scope=MandateScope(),
                purposes=frozenset(),
                valid_from=None,
                valid_until=None,
            )
            if not spec.capabilities.issubset(ceiling.capabilities):
                violations.append(DelegationViolation.PRINCIPAL_CAPABILITY_MISSING)
        else:
            ceiling = DelegationAuthority(
                capabilities=parent_mandate.capabilities,
                scope=parent_mandate.scope,
                purposes=parent_mandate.purposes,
                valid_from=parent_mandate.valid_from,
                valid_until=parent_mandate.valid_until,
            )
            principal_caps = set(principal.capabilities)
            if not spec.capabilities.issubset(principal_caps):
                violations.append(DelegationViolation.PRINCIPAL_CAPABILITY_MISSING)

        if not spec.capabilities.issubset(ceiling.capabilities):
            violations.append(DelegationViolation.CAPABILITY_EXPANSION)

        if not _scope_subset(spec.scope, ceiling.scope, self._store):
            violations.append(DelegationViolation.SCOPE_EXPANSION)

        if ceiling.purposes:
            if spec.purposes and not spec.purposes.issubset(ceiling.purposes):
                violations.append(DelegationViolation.PURPOSE_EXPANSION)
        elif spec.purposes and parent_mandate is not None:
            pass

        if not _time_subset(
            spec.valid_from,
            spec.valid_until,
            ceiling.valid_from,
            ceiling.valid_until,
        ):
            violations.append(DelegationViolation.TIME_EXPANSION)

        return DelegationValidationResult(valid=not violations, violations=violations)


def _time_subset(
    child_from: datetime | None,
    child_until: datetime | None,
    parent_from: datetime | None,
    parent_until: datetime | None,
) -> bool:
    if parent_from is not None and child_from is not None and child_from < parent_from:
        return False
    if parent_until is not None and child_until is not None and child_until > parent_until:
        return False
    return True


def _scope_subset(child: MandateScope, parent: MandateScope, store: SQLiteStore) -> bool:
    if isinstance(parent, EffectiveScope):
        return all(_scope_subset(child, bound, store) for bound in parent.bounds)
    if parent.is_empty():
        return True
    if child.is_empty():
        return True

    for oid in child.object_ids:
        obj = store.get(oid)
        if obj is None:
            return False
        if not scope_allows(parent, obj, store):
            return False

    for ctype in child.object_types:
        if ctype not in parent.object_types:
            return False

    for constraint in child.relationship_constraints:
        if constraint not in parent.relationship_constraints:
            return False

    return True
