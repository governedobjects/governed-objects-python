# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Fixed governed tool registry — no dynamic runtime exposure."""

from __future__ import annotations

from governed_objects.llm.model import ToolDefinition

REGISTERED_TOOL_NAMES = frozenset({
    "query_objects",
    "read_object",
    "traverse_relationships",
    "execute_action",
    "get_history",
})


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="query_objects",
            description="Query governed objects visible to this session.",
            parameters={
                "type": "object",
                "properties": {
                    "object_type": {"type": "string"},
                    "include_subtypes": {"type": "boolean"},
                    "state": {"type": "string"},
                },
                "required": ["object_type"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="read_object",
            description="Read one authorized governed object.",
            parameters={
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="traverse_relationships",
            description="Traverse authorized relationships from an object.",
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "relation": {"type": "string"},
                    "max_depth": {"type": "integer"},
                },
                "required": ["object_id"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="execute_action",
            description="Execute a governed lifecycle action on an object.",
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "action": {"type": "string"},
                    "expected_version": {"type": "integer"},
                    "parameters": {"type": "object"},
                },
                "required": ["object_id", "action", "expected_version"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="get_history",
            description="Get authorized object event/audit history, which may include accepted changes and nonaccepted or audit activity.",
            parameters={
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
                "additionalProperties": False,
            },
        ),
    ]


def is_registered_tool(name: str) -> bool:
    return name in REGISTERED_TOOL_NAMES
