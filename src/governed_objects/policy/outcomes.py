# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Portable minimum outcome categories (OP-OUTCOME-001)."""

from __future__ import annotations

from enum import StrEnum

from governed_objects.domain.models import Decision
from governed_objects.policy.reasons import ConflictReason, DenyReason


class OutcomeCategory(StrEnum):
    ACCEPTED = "ACCEPTED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    SEMANTICALLY_INVALID = "SEMANTICALLY_INVALID"
    CONFLICT = "CONFLICT"


# EXPERIMENTAL. A runtime capacity
# refusal: the command may be valid, authorized and current, but no unused
# Object Version remains. Deliberately outside the four portable categories.
REFUSED = "REFUSED"


AUTH_DENY_REASONS = frozenset(
    {
        DenyReason.READ_NOT_AUTHORIZED,
        DenyReason.ACTOR_CAPABILITY_MISSING,
        DenyReason.APPLICATION_CAPABILITY_MISSING,
        DenyReason.MANDATE_CAPABILITY_MISSING,
        DenyReason.MANDATE_SCOPE_MISMATCH,
        DenyReason.MANDATE_EXPIRED,
        DenyReason.MANDATE_REVOKED,
        DenyReason.MANDATE_NOT_YET_VALID,
        DenyReason.MANDATE_DELEGATE_MISMATCH,
        DenyReason.MANDATE_UNKNOWN,
        DenyReason.MANDATE_INVALID,
        DenyReason.PURPOSE_MISMATCH,
        DenyReason.PARENT_MANDATE_REVOKED,
        DenyReason.PARENT_MANDATE_EXPIRED,
        DenyReason.DELEGATION_CYCLE,
        DenyReason.DELEGATION_DEPTH_EXCEEDED,
        DenyReason.APPLICATION_UNKNOWN,
        DenyReason.CONSTRAINT_VIOLATION,
    }
)

SEMANTIC_DENY_REASONS = frozenset(
    {
        DenyReason.LIFECYCLE_INVALID,
        DenyReason.OBJECT_POLICY_DENIED,
        DenyReason.NOT_FOUND,
        DenyReason.SCHEMA_INVALID,
        DenyReason.RELATIONSHIP_INVALID,
        DenyReason.TYPE_VERSION_UNSUPPORTED,
        DenyReason.IDENTITY_EXISTS,
        DenyReason.STATE_INVALID,
    }
)

# Non-accepted outcomes whose condition none of the four minimum classes
# describes; reported as REFUSED, never as SEMANTICALLY_INVALID.
REFUSAL_DENY_REASONS = frozenset(
    {
        DenyReason.OBJECT_VERSION_SPACE_EXHAUSTED,
        DenyReason.ADAPTER_RESULT_INVALID,
        DenyReason.PROVIDER_FAILED,
    }
)


def classify_outcome(
    decision: Decision,
    *,
    deny_reason: str | DenyReason | None = None,
    conflict_reason: str | ConflictReason | None = None,
    has_validation_checks: bool = False,
) -> OutcomeCategory:
    if decision == Decision.ALLOW:
        return OutcomeCategory.ACCEPTED
    if decision == Decision.CONFLICT:
        return OutcomeCategory.CONFLICT
    reason = DenyReason(deny_reason) if isinstance(deny_reason, str) and deny_reason else deny_reason
    if reason in AUTH_DENY_REASONS:
        return OutcomeCategory.AUTHORIZATION_DENIED
    if reason in SEMANTIC_DENY_REASONS:
        return OutcomeCategory.SEMANTICALLY_INVALID
    if has_validation_checks:
        return OutcomeCategory.SEMANTICALLY_INVALID
    if decision == Decision.DENY:
        return OutcomeCategory.SEMANTICALLY_INVALID
    return OutcomeCategory.SEMANTICALLY_INVALID
