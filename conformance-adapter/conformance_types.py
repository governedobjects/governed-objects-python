# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Type adapter for the suite-defined declarative fixture types.

It applies the requested field value (after the suite's fixture rules) and nothing else. It proposes no Object
Version: the resulting object keeps the stored version, and the runtime
issues the new one.
"""

from __future__ import annotations

from typing import Any

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import AdapterContext, AdapterExecuteResult
from governed_objects.domain.command import Command
from governed_objects.domain.models import Actor, GovernedObject, ValidationCheck
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator

try:
    from governed_objects.adapters.context import AdapterFailure
except ImportError:  # older runtime releases predate the explicit failure class
    AdapterFailure = None

def _rejected(message: str) -> AdapterExecuteResult:
    """The fixture's own domain rejection of the requested title."""
    if AdapterFailure is None:
        return AdapterExecuteResult(success=False, error_message=message)
    return AdapterExecuteResult(success=False, error_message=message, failure=AdapterFailure.INVALID)


class ConformanceFixtureAdapter(TypeAdapter):
    """Type adapter for the suite's declarative fixture types (not the portable path).

    ``rules`` maps native kind -> {"create": [fields], "update": [fields]}: the fields the
    suite's fixture rules say must not be blank. ``update_actions`` maps native kind ->
    {native action: field}. Nothing here defines a fixture rule; it applies the suite's."""

    def __init__(self, rules: dict[str, dict[str, list[str]]], update_actions: dict[str, dict[str, str]]):
        self._rules = rules
        self._updates = update_actions

    def supports(self, object_type: str) -> bool:
        return object_type in self._rules

    def validate_create(
        self,
        type_def: ObjectTypeDefinition,
        data: dict[str, Any],
    ) -> tuple[bool, list[ValidationCheck], str]:
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        errors = list(errors)
        for field in self._rules.get(type_def.kind, {}).get("create", []):
            if field in data and not str(data[field]).strip():
                valid = False
                errors.append(f"{field} must not be blank")
        return valid, checks, "; ".join(errors)

    def validate_transition(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        action: str,
        actor: Actor,
    ) -> tuple[bool, list[ValidationCheck], str]:
        return True, [], ""

    def adapter_execute(
        self,
        obj: GovernedObject,
        command: Command,
        context: AdapterContext,
    ) -> AdapterExecuteResult | None:
        field = self._updates.get(obj.type, {}).get(command.action)
        if field is None:
            return None
        value = (command.payload or {}).get(field)
        if value is None:
            return _rejected(f"missing {field} in payload")
        if field in self._rules.get(obj.type, {}).get("update", []) and not str(value).strip():
            return _rejected(f"{field} must not be blank")
        new_data = dict(obj.data or {})
        new_data[field] = value
        return AdapterExecuteResult(success=True, object=obj.model_copy(update={"data": new_data}))

    def filter_read_data(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        actor: Actor,
        fields: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        return obj.data or {}, []
