# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Trusted internal mandate resolution from canonical Governed Objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from governed_objects.delegation.mandate import DelegatedMandate, MandateStatus
from governed_objects.delegation.materializer import materialize_mandate
from governed_objects.delegation.scope import EffectiveScope, MandateScope
from governed_objects.domain.clock import Clock, SystemClock
from governed_objects.domain.models import GovernedObject
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.policy.reasons import DenyReason


MANDATE_OBJECT_TYPE = "DelegatedMandate"


@dataclass(frozen=True)
class ResolvedMandateAuthority:
    mandate_id: str
    principal_id: str
    delegate_actor_id: str
    capabilities: frozenset[str]
    scope: MandateScope | EffectiveScope
    purposes: frozenset[str]
    valid_from: datetime | None
    valid_until: datetime | None
    canonical_state: str
    version: int
    parent_mandate_ids: tuple[str, ...]
    ancestor_ineffective_reason: DenyReason | None = None
    purpose_restricted: bool = False

    def is_effective_at(self, when: datetime) -> tuple[bool, DenyReason | None]:
        if self.ancestor_ineffective_reason is not None:
            return False, self.ancestor_ineffective_reason
        if not self.capabilities:
            return False, DenyReason.MANDATE_CAPABILITY_MISSING
        if self.purpose_restricted and not self.purposes:
            return False, DenyReason.PURPOSE_MISMATCH
        if self.canonical_state == "revoked":
            return False, DenyReason.MANDATE_REVOKED
        if self.canonical_state != "active":
            return False, DenyReason.MANDATE_NOT_YET_VALID
        if self.valid_from is not None and when < self.valid_from:
            return False, DenyReason.MANDATE_NOT_YET_VALID
        if self.valid_until is not None and when > self.valid_until:
            return False, DenyReason.MANDATE_EXPIRED
        if self.valid_from is not None and self.valid_until is not None and self.valid_from > self.valid_until:
            return False, DenyReason.MANDATE_NOT_YET_VALID
        return True, None


class MandateResolver:
    """Trusted runtime path for loading mandate authority.

    This bypasses mandate authorization checks to avoid recursion
    (evaluating mandate A must not require mandate A).
    """

    # Four ancestors are supported. A fifth unresolved ancestor always refuses.
    MAX_CHAIN_DEPTH = 4

    def __init__(
        self,
        store: SQLiteStore,
        *,
        clock: Clock | None = None,
        validate_binding=None,
        scope_decoder=None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._validate_binding = validate_binding
        self._scope_decoder = scope_decoder

    def get_object(self, mandate_id: str) -> GovernedObject | None:
        if self._validate_binding is None:
            raise ValueError('delegation configuration unavailable')
        obj = self._store.get(mandate_id)
        if obj is None or obj.type != MANDATE_OBJECT_TYPE:
            return None
        self._validate_binding(obj)
        return obj

    def resolve(self, mandate_id: str) -> DelegatedMandate | None:
        obj = self.get_object(mandate_id)
        if obj is None:
            return None
        return materialize_mandate(obj, self._store, scope_decoder=self._scope_decoder)

    def resolve_authority(self, mandate_id: str) -> ResolvedMandateAuthority | None:
        obj = self.get_object(mandate_id)
        if obj is None:
            return None
        mandate = materialize_mandate(obj, self._store, scope_decoder=self._scope_decoder)
        parent_ids: list[str] = []
        ancestor_reason: DenyReason | None = None
        current_parent = mandate.parent_mandate_id
        visited: set[str] = {mandate_id}
        bounds = [mandate]
        depth = 0
        while current_parent:
            if depth >= self.MAX_CHAIN_DEPTH:
                ancestor_reason = DenyReason.DELEGATION_DEPTH_EXCEEDED
                break
            if current_parent in visited:
                ancestor_reason = DenyReason.DELEGATION_CYCLE
                break
            visited.add(current_parent)
            parent_ids.append(current_parent)
            parent_obj = self.get_object(current_parent)
            if parent_obj is None:
                ancestor_reason = DenyReason.MANDATE_UNKNOWN
                break
            parent = materialize_mandate(parent_obj, self._store, scope_decoder=self._scope_decoder)
            bounds.append(parent)
            if parent.status == MandateStatus.REVOKED or parent_obj.state == "revoked":
                ancestor_reason = DenyReason.PARENT_MANDATE_REVOKED
                break
            now = self._clock.now()
            if parent.valid_from is not None and now < parent.valid_from:
                ancestor_reason = DenyReason.MANDATE_NOT_YET_VALID
                break
            if parent.valid_until is not None and now > parent.valid_until:
                ancestor_reason = DenyReason.PARENT_MANDATE_EXPIRED
                break
            if parent_obj.state != "active":
                ancestor_reason = DenyReason.PARENT_MANDATE_REVOKED
                break
            current_parent = parent.parent_mandate_id
            depth += 1

        effective_caps = mandate.capabilities
        effective_scope = EffectiveScope(tuple(bound.scope for bound in bounds))
        purpose_bounds = [bound.purposes for bound in bounds if bound.purposes]
        effective_purposes = frozenset.intersection(*purpose_bounds) if purpose_bounds else frozenset()
        effective_from = mandate.valid_from
        effective_until = mandate.valid_until

        for parent in bounds[1:]:
            effective_caps = effective_caps & parent.capabilities
            effective_from = _max_dt(effective_from, parent.valid_from)
            effective_until = _min_dt(effective_until, parent.valid_until)

        return ResolvedMandateAuthority(
            mandate_id=mandate.id,
            principal_id=mandate.principal_id,
            delegate_actor_id=mandate.delegate_actor_id,
            capabilities=effective_caps,
            scope=effective_scope,
            purposes=effective_purposes,
            valid_from=effective_from,
            valid_until=effective_until,
            canonical_state=obj.state,
            version=obj.version,
            parent_mandate_ids=tuple(parent_ids),
            ancestor_ineffective_reason=ancestor_reason,
            purpose_restricted=bool(purpose_bounds),
        )


def _max_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _min_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
