# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Mandate creation, activation, and revocation via Governed Object runtime."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from governed_objects.delegation.mandate import DelegatedMandate, MandateStatus
from governed_objects.delegation.resolver import MandateResolver
from governed_objects.delegation.scope import MandateScope
from governed_objects.delegation.validator import (
    DelegationSpec,
    DelegationValidator,
    DelegationViolation,
)
from governed_objects.domain.actors import ActorRegistry
from governed_objects.domain.command import CreateObject, TransitionState
from governed_objects.domain.models import Actor, Decision
from governed_objects.relationships.vocabulary import (
    DELEGATED_BY,
    DELEGATED_TO,
    DERIVED_FROM,
    SCOPED_TO,
)

if TYPE_CHECKING:
    from governed_objects.runtime.runtime import GovernedObjectRuntime


class MandateValidationError(ValueError):
    def __init__(self, message: str, violations: list[DelegationViolation] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


class MandateService:
    """Manages DelegatedMandate Governed Objects."""

    def __init__(
        self,
        runtime: GovernedObjectRuntime,
        resolver: MandateResolver,
        validator: DelegationValidator,
        actors: ActorRegistry | None = None,
    ) -> None:
        self._runtime = runtime
        self._resolver = resolver
        self._validator = validator
        self._actors = actors

    def create_mandate(
        self,
        *,
        mandate_id: str,
        principal: Actor,
        delegate_actor_id: str,
        capabilities: frozenset[str],
        scope: MandateScope | None = None,
        purposes: frozenset[str] | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        parent_mandate_id: str | None = None,
        auto_activate: bool = True,
    ) -> DelegatedMandate:
        if (not isinstance(capabilities, frozenset) or not capabilities
                or any(not isinstance(cap, str) or not cap for cap in capabilities)
                or (scope is not None and not isinstance(scope, MandateScope))
                or (purposes is not None and (not isinstance(purposes, frozenset)
                    or any(not isinstance(purpose, str) or not purpose for purpose in purposes)))):
            raise MandateValidationError('invalid delegation input')
        if any(value is not None and (not isinstance(value, datetime) or value.tzinfo is None
                                      or value.utcoffset() is None) for value in (valid_from, valid_until)):
            raise MandateValidationError('invalid delegation time')
        try:
            configuration = self._runtime.require_delegation()
            configuration.actor(principal.id)
            configuration.actor(delegate_actor_id)
        except ValueError:
            raise MandateValidationError('delegation configuration unavailable') from None
        parent = None
        ancestor_ids: frozenset[str] = frozenset()
        if parent_mandate_id:
            if mandate_id == parent_mandate_id:
                raise MandateValidationError(
                    "delegation validation failed: DELEGATION_CYCLE",
                    violations=[DelegationViolation.DELEGATION_CYCLE],
                )
            parent = self._resolver.resolve(parent_mandate_id)
            if parent is None:
                raise MandateValidationError(f"unknown parent mandate: {parent_mandate_id}")
            resolved = self._resolver.resolve_authority(parent_mandate_id)
            if resolved is not None:
                ancestor_ids = frozenset(resolved.parent_mandate_ids)

        spec = DelegationSpec(
            capabilities=capabilities,
            scope=scope or MandateScope(),
            purposes=purposes or frozenset(),
            valid_from=valid_from,
            valid_until=valid_until,
            parent_mandate_id=parent_mandate_id,
            mandate_id=mandate_id,
        )
        result = self._validator.validate(
            principal=principal,
            spec=spec,
            parent_mandate=parent,
            ancestor_ids=ancestor_ids,
        )
        if not result.valid:
            raise MandateValidationError(
                f"delegation validation failed: {', '.join(v.value for v in result.violations)}",
                violations=result.violations,
            )

        if self._actors is not None:
            self._actors.resolve(delegate_actor_id)

        scope = scope or MandateScope()
        relationships = [
            {"source_id": mandate_id, "relation": DELEGATED_BY, "target_id": principal.id},
            {"source_id": mandate_id, "relation": DELEGATED_TO, "target_id": delegate_actor_id},
        ]
        if parent_mandate_id:
            relationships.append(
                {
                    "source_id": mandate_id,
                    "relation": DERIVED_FROM,
                    "target_id": parent_mandate_id,
                }
            )
        for target_id in _scoped_targets(scope):
            relationships.append(
                {"source_id": mandate_id, "relation": SCOPED_TO, "target_id": target_id}
            )

        data = {
            "capabilities": sorted(capabilities),
            "purposes": sorted(purposes or []),
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_until": valid_until.isoformat() if valid_until else None,
            "parent_mandate_id": parent_mandate_id,
            "scope": _scope_to_data(scope),
        }

        create = self._runtime.execute(
            CreateObject(
                mandate_id,
                "DelegatedMandate",
                data,
                state="draft",
                relationships=relationships,
            ),
            actor=principal,
        )
        if create.decision != Decision.ALLOW:
            raise MandateValidationError(create.reason or "mandate create denied")

        self._runtime.mandate_projection.sync(mandate_id)

        if auto_activate:
            activated = self.activate(mandate_id, actor=principal)
            if activated is None:
                raise MandateValidationError("mandate activation failed")
            return activated

        mandate = self.get(mandate_id)
        if mandate is None:
            raise MandateValidationError("mandate not found after create")
        return mandate

    def activate(
        self,
        mandate_id: str,
        *,
        actor: Actor,
        expected_version: int | None = None,
    ) -> DelegatedMandate | None:
        obj = self._resolver.get_object(mandate_id)
        if obj is None:
            return None
        version = expected_version if expected_version is not None else obj.version
        result = self._runtime.execute(
            TransitionState(mandate_id, "activate"),
            actor=actor,
            command_context=self._runtime._make_context(expected_version=version),
        )
        if result.decision != Decision.ALLOW:
            return None
        self._runtime.mandate_projection.sync(mandate_id)
        return self.get(mandate_id)

    def revoke(
        self,
        mandate_id: str,
        *,
        actor: Actor | None = None,
        expected_version: int | None = None,
    ) -> DelegatedMandate | None:
        mandate = self.get(mandate_id)
        if mandate is None:
            return None
        if actor is None:
            if self._actors is None:
                return None
            actor = self._actors.resolve(mandate.principal_id)
        obj = self._resolver.get_object(mandate_id)
        if obj is None:
            return None
        version = expected_version if expected_version is not None else obj.version
        result = self._runtime.execute(
            TransitionState(mandate_id, "revoke"),
            actor=actor,
            command_context=self._runtime._make_context(expected_version=version),
        )
        if result.decision != Decision.ALLOW:
            return None
        self._runtime.mandate_projection.sync(mandate_id)
        revoked = self.get(mandate_id)
        if revoked is not None:
            revoked.status = MandateStatus.REVOKED
        return revoked

    def get(self, mandate_id: str) -> DelegatedMandate | None:
        return self._resolver.resolve(mandate_id)


def _scoped_targets(scope: MandateScope) -> list[str]:
    targets = list(scope.object_ids)
    for constraint in scope.relationship_constraints:
        targets.append(constraint.target_id)
    return sorted(set(targets))


def _scope_to_data(scope: MandateScope) -> dict:
    return {
        "object_ids": sorted(scope.object_ids),
        "object_types": sorted(scope.object_types),
        "relationship_constraints": [
            {"relation": c.relation, "target_id": c.target_id}
            for c in scope.relationship_constraints
        ],
    }
