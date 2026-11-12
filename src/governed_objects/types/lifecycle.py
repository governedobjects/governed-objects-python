# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Lifecycle validation for governed object types."""

from __future__ import annotations

from governed_objects.domain.models import ValidationCheck
from governed_objects.types.schema import ObjectTypeDefinition


class LifecycleValidator:
    """Validates state transitions against a type definition."""

    def validate_transition(
        self,
        type_def: ObjectTypeDefinition,
        current_state: str,
        action: str,
    ) -> tuple[bool, str, ValidationCheck]:
        if type_def.is_terminal(current_state):
            check = ValidationCheck(
                name="lifecycle",
                passed=False,
                message=f"state {current_state!r} is terminal",
            )
            return False, f"cannot transition from terminal state {current_state!r}", check

        target = type_def.transition_target(current_state, action)
        if target is None:
            check = ValidationCheck(
                name="lifecycle",
                passed=False,
                message=f"action {action!r} not allowed from state {current_state!r}",
            )
            return (
                False,
                f"action {action!r} not permitted in state {current_state!r}",
                check,
            )

        check = ValidationCheck(
            name="lifecycle",
            passed=True,
            message=f"{current_state} --{action}--> {target}",
        )
        return True, "", check

    def can_reach_state(
        self,
        object_type: str,
        current_state: str,
        target_state: str,
        type_registry,
    ) -> bool:
        """Check whether target_state is reachable from current_state in one hop."""
        if current_state == target_state:
            return True
        try:
            type_def = type_registry.get(object_type)
        except KeyError:
            return False
        state_def = type_def.states.get(current_state)
        if state_def is None:
            return False
        return target_state in state_def.transitions.values()
