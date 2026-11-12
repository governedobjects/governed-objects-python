# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Strict typed schemas for model-controlled tool arguments."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class QueryObjectsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: str
    include_subtypes: bool = False
    state: str | None = None


class ReadObjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str


class TraverseRelationshipsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str
    relation: str | None = None
    max_depth: int = 2


class ExecuteActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str
    action: str
    expected_version: int
    parameters: dict[str, Any] = {}


class GetHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str


FORBIDDEN_IDENTITY_FIELDS = frozenset({
    "actor_id",
    "application_id",
    "mandate_id",
    "principal_id",
    "purpose",
    "provider",
    "runtime",
})


def parse_tool_input(schema: type[BaseModel], arguments: dict[str, Any]) -> tuple[BaseModel | None, str | None]:
    for key in arguments:
        if key in FORBIDDEN_IDENTITY_FIELDS:
            return None, f"forbidden field: {key}"
    try:
        return schema.model_validate(arguments), None
    except ValidationError as exc:
        return None, str(exc.errors()[0]["msg"])
