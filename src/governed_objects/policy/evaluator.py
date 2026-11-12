# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Capability-based policy evaluation for governed objects."""

from __future__ import annotations

from typing import Protocol

from governed_objects.applications.registry import ApplicationDefinition
from governed_objects.delegation.mandate import DelegatedMandate, MandateStatus
from governed_objects.delegation.resolver import ResolvedMandateAuthority
from governed_objects.delegation.scope import scope_allows
from governed_objects.domain.clock import Clock, SystemClock
from governed_objects.domain.models import Actor, Decision, GovernedObject
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.policy.reasons import DenyReason
from governed_objects.policy.constraints import ConstraintHandler
from governed_objects.types.schema import ObjectTypeDefinition


class PolicyDecision:
    def __init__(
        self,
        decision: Decision,
        reason: str,
        required: list[str],
        satisfied: list[str],
        missing: list[str],
        *,
        deny_reason: DenyReason | None = None,
    ) -> None:
        self.decision = decision
        self.reason = reason
        self.required_capabilities = required
        self.satisfied_capabilities = satisfied
        self.missing_capabilities = missing
        self.deny_reason = deny_reason


class PolicyEvaluator(Protocol):
    def evaluate(
        self,
        actor: Actor,
        action: str,
        obj: GovernedObject | None,
        type_def: ObjectTypeDefinition,
        *,
        required_capabilities: list[str] | None = None,
        application: ApplicationDefinition | None = None,
        mandate: DelegatedMandate | None = None,
        resolved_mandate: ResolvedMandateAuthority | None = None,
        purpose: str | None = None,
    ) -> PolicyDecision: ...


class CapabilityPolicyEvaluator:
    """Deterministic capability-based policy evaluator.

    Effective authority = actor ∩ mandate ∩ application capabilities,
    plus mandate time/purpose/scope when a mandate is present.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        store: SQLiteStore | None = None,
        constraint_handlers: dict[str, ConstraintHandler] | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._store = store
        self._constraint_handlers = dict(constraint_handlers or {})
        self._application_source = None
        self._mandate_source = None

    def bind_context_sources(self, *, application_source, mandate_source) -> None:
        """Explicit local authority sources; snapshots alone are never grants."""
        self._application_source = application_source
        self._mandate_source = mandate_source

    def evaluate(
        self,
        actor: Actor,
        action: str,
        obj: GovernedObject | None,
        type_def: ObjectTypeDefinition,
        *,
        required_capabilities: list[str] | None = None,
        application: ApplicationDefinition | None = None,
        mandate: DelegatedMandate | None = None,
        resolved_mandate: ResolvedMandateAuthority | None = None,
        purpose: str | None = None,
    ) -> PolicyDecision:
        required = (
            required_capabilities
            if required_capabilities is not None
            else type_def.required_capabilities(action)
        )
        open_action = required_capabilities is None and type_def.is_open(action)

        if application is not None:
            try:
                if self._application_source is None or not isinstance(application.id, str) or not application.id:
                    raise ValueError('unresolved application')
                application = self._application_source(application.id)
                if not isinstance(application, ApplicationDefinition):
                    raise ValueError('unresolved application')
            except (ValueError, TypeError, KeyError, AttributeError):
                return PolicyDecision(Decision.DENY, 'application authority unavailable', required, [], required,
                                      deny_reason=DenyReason.APPLICATION_UNKNOWN)
        if mandate is not None or resolved_mandate is not None:
            try:
                identities = [value for value in (
                    mandate.id if mandate is not None else None,
                    resolved_mandate.mandate_id if resolved_mandate is not None else None,
                ) if value is not None]
                if (self._mandate_source is None or not identities
                        or any(not isinstance(value, str) or not value for value in identities)
                        or len(set(identities)) != 1):
                    raise ValueError('unresolved mandate')
                resolved_mandate = self._mandate_source(identities[0])
                if not isinstance(resolved_mandate, ResolvedMandateAuthority):
                    raise ValueError('unresolved mandate')
                mandate = None
            except (ValueError, TypeError, KeyError, AttributeError):
                return PolicyDecision(Decision.DENY, 'mandate authority unavailable', required, [], required,
                                      deny_reason=DenyReason.MANDATE_INVALID)

        for constraint in actor.constraints:
            handler = self._constraint_handlers.get(constraint)
            if handler is None or handler(actor, action, obj, type_def) is not True:
                return PolicyDecision(
                    Decision.DENY,
                    f"actor constraint violated: {constraint}",
                    required,
                    [],
                    required,
                    deny_reason=DenyReason.CONSTRAINT_VIOLATION,
                )

        actor_decision = self._evaluate_caps(
            set(actor.capabilities),
            required,
            subject="actor",
            open_action=open_action,
        )
        if actor_decision.decision == Decision.DENY:
            actor_decision.deny_reason = DenyReason.ACTOR_CAPABILITY_MISSING
            return actor_decision

        if mandate is not None or resolved_mandate is not None:
            mandate_deny = self._evaluate_mandate(
                actor, mandate, resolved_mandate, required, purpose, obj, open_action=open_action
            )
            if mandate_deny is not None:
                return mandate_deny

        if application is not None:
            app_decision = self._evaluate_caps(
                set(application.capabilities),
                required,
                subject=f"application:{application.id}",
                open_action=open_action,
            )
            if app_decision.decision == Decision.DENY:
                app_decision.deny_reason = DenyReason.APPLICATION_CAPABILITY_MISSING
                return app_decision

        return PolicyDecision(
            Decision.ALLOW,
            "policy satisfied",
            required,
            actor_decision.satisfied_capabilities,
            actor_decision.missing_capabilities,
        )

    def _evaluate_mandate(
        self,
        actor: Actor,
        mandate: DelegatedMandate | None,
        resolved: ResolvedMandateAuthority | None,
        required: list[str],
        purpose: str | None,
        obj: GovernedObject | None,
        *,
        open_action: bool = False,
    ) -> PolicyDecision | None:
        authority = resolved
        if authority is None:
            return PolicyDecision(Decision.DENY, 'mandate authority unavailable', required, [], required,
                                  deny_reason=DenyReason.MANDATE_INVALID)

        if authority.delegate_actor_id != actor.id:
            return PolicyDecision(
                Decision.DENY,
                "mandate delegate mismatch",
                required,
                [],
                required,
                deny_reason=DenyReason.MANDATE_DELEGATE_MISMATCH,
            )

        if authority.ancestor_ineffective_reason is not None:
            return PolicyDecision(
                Decision.DENY,
                authority.ancestor_ineffective_reason.value.lower().replace("_", " "),
                required,
                [],
                required,
                deny_reason=authority.ancestor_ineffective_reason,
            )

        now = self._clock.now()
        effective, time_reason = authority.is_effective_at(now)
        if not effective and time_reason is not None:
            return PolicyDecision(
                Decision.DENY,
                time_reason.value.lower().replace("_", " "),
                required,
                [],
                required,
                deny_reason=time_reason,
            )

        if authority.purposes or authority.purpose_restricted:
            if purpose is None or purpose not in authority.purposes:
                return PolicyDecision(
                    Decision.DENY,
                    "purpose mismatch",
                    required,
                    [],
                    required,
                    deny_reason=DenyReason.PURPOSE_MISMATCH,
                )

        mandate_decision = self._evaluate_caps(
            set(authority.capabilities),
            required,
            subject=f"mandate:{authority.mandate_id}",
            open_action=open_action,
        )
        if mandate_decision.decision == Decision.DENY:
            mandate_decision.deny_reason = DenyReason.MANDATE_CAPABILITY_MISSING
            return mandate_decision

        if not authority.scope.is_empty():
            if obj is None or self._store is None or not scope_allows(authority.scope, obj, self._store):
                return PolicyDecision(
                    Decision.DENY,
                    "mandate scope mismatch",
                    required,
                    [],
                    required,
                    deny_reason=DenyReason.MANDATE_SCOPE_MISMATCH,
                )
        return None

    @staticmethod
    def _evaluate_caps(
        caps: set[str],
        required: list[str],
        *,
        subject: str,
        open_action: bool = False,
    ) -> PolicyDecision:
        satisfied = [cap for cap in required if cap in caps]
        missing = [cap for cap in required if cap not in caps]

        if not required:
            if open_action:
                return PolicyDecision(
                    Decision.ALLOW,
                    f"no capabilities required for {subject} (type marked open)",
                    required,
                    satisfied,
                    missing,
                )
            return PolicyDecision(
                Decision.DENY,
                f"no capabilities declared for {subject} and type is not marked open",
                required,
                satisfied,
                missing,
            )

        if missing:
            return PolicyDecision(
                Decision.DENY,
                f"{subject} missing capabilities: {', '.join(missing)}",
                required,
                satisfied,
                missing,
            )

        return PolicyDecision(
            Decision.ALLOW,
            f"{subject} policy satisfied",
            required,
            satisfied,
            missing,
        )
