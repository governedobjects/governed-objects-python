# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""DelegatedMandate adapter — schema validation only."""

from __future__ import annotations

from typing import Any

from governed_objects.adapters.base import TypeAdapter
from governed_objects.domain.models import Actor, GovernedObject, ValidationCheck
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator


class MandateAdapter(TypeAdapter):
    def supports(self, object_type: str) -> bool:
        return object_type == "DelegatedMandate"

    def validate_create(
        self,
        type_def: ObjectTypeDefinition,
        data: dict[str, Any],
    ) -> tuple[bool, list[ValidationCheck], str]:
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        if not data.get("capabilities"):
            return False, checks, "capabilities required"
        return valid, checks, "; ".join(errors)

    def validate_transition(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        action: str,
        actor: Actor,
    ) -> tuple[bool, list[ValidationCheck], str]:
        return True, [], ""

    def filter_read_data(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        actor: Actor,
        fields: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        return obj.data, []
