# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Command definitions for the governed object runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Command:
    action: str
    object_id: str | None = None
    object_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CreateObject(Command):
    def __init__(
        self,
        object_id: str,
        object_type: str,
        data: dict[str, Any],
        *,
        state: str | None = None,
        metadata: dict[str, Any] | None = None,
        relationships: list[dict[str, str]] | None = None,
    ) -> None:
        object.__setattr__(self, "action", "create")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_type", object_type)
        object.__setattr__(
            self,
            "payload",
            {
                "data": data,
                "state": state,
                "metadata": metadata or {},
                "relationships": relationships or [],
            },
        )


@dataclass(frozen=True)
class TransitionState(Command):
    def __init__(self, object_id: str, transition: str) -> None:
        object.__setattr__(self, "action", transition)
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_type", None)
        object.__setattr__(self, "payload", {})


@dataclass(frozen=True)
class ReadObject(Command):
    def __init__(self, object_id: str, fields: list[str] | None = None) -> None:
        object.__setattr__(self, "action", "read")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_type", None)
        object.__setattr__(self, "payload", {"fields": fields or []})


@dataclass(frozen=True)
class AddRelationship(Command):
    def __init__(
        self,
        object_id: str,
        relation: str,
        target_id: str,
        *,
        target_type: str | None = None,
    ) -> None:
        object.__setattr__(self, "action", "add_relationship")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_type", None)
        object.__setattr__(
            self,
            "payload",
            {"relation": relation, "target_id": target_id, "target_type": target_type},
        )


@dataclass(frozen=True)
class RemoveRelationship(Command):
    def __init__(self, object_id: str, relation: str, target_id: str) -> None:
        object.__setattr__(self, "action", "remove_relationship")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "object_type", None)
        object.__setattr__(
            self,
            "payload",
            {"relation": relation, "target_id": target_id},
        )
