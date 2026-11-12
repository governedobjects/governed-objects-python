# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Schema validation for governed object data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from governed_objects.domain.models import ValidationCheck
from governed_objects.types.schema import ObjectTypeDefinition


class SchemaValidator:
    """Validates object data against a type schema."""

    def validate(self, type_def: ObjectTypeDefinition, data: dict[str, Any]) -> tuple[bool, list[ValidationCheck], list[str]]:
        checks: list[ValidationCheck] = []
        errors: list[str] = []

        for field_name, field_schema in type_def.schema.items():
            value = data.get(field_name)
            if field_schema.required and (value is None or value == ""):
                msg = f"missing required field: {field_name}"
                checks.append(ValidationCheck(name=field_name, passed=False, message=msg))
                errors.append(msg)
                continue

            if value is None:
                checks.append(ValidationCheck(name=field_name, passed=True))
                continue

            if not self._type_matches(field_schema.type, value):
                msg = f"field {field_name}: expected {field_schema.type}, got {type(value).__name__}"
                checks.append(ValidationCheck(name=field_name, passed=False, message=msg))
                errors.append(msg)
            else:
                checks.append(ValidationCheck(name=field_name, passed=True))

        return len(errors) == 0, checks, errors

    @staticmethod
    def _type_matches(expected: str, value: Any) -> bool:
        mapping: dict[str, tuple[type, ...]] = {
            "string": (str,),
            "integer": (int,),
            "number": (int, float),
            "float": (float, int),
            "double": (float, int),
            "boolean": (bool,),
            "datetime": (str, datetime),
            "date": (str,),
            "array": (list,),
            "object": (dict,),
        }
        allowed = mapping.get(expected, (str, int, float, bool))
        return isinstance(value, allowed)
