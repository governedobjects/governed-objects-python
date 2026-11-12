# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Structured tool results returned to the reasoning model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    success: bool
    decision: str | None = None
    reason: str | None = None
    message: str | None = None
    object_id: str | None = None
    state: str | None = None
    version: int | None = None
    actual_version: int | None = None
    expected_version: int | None = None
    objects: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    schema_rejected: bool = False

    def to_model_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool_call_id": self.tool_call_id}
        if self.error:
            payload["error"] = self.error
            if self.schema_rejected:
                payload["schema_rejected"] = True
            return payload
        if self.decision:
            payload["decision"] = self.decision
        if self.reason:
            payload["reason"] = self.reason
        if self.message:
            payload["message"] = self.message
        if self.object_id:
            payload["object_id"] = self.object_id
        if self.state is not None:
            payload["state"] = self.state
        if self.version is not None:
            payload["version"] = self.version
        if self.actual_version is not None:
            payload["actual_version"] = self.actual_version
        if self.expected_version is not None:
            payload["expected_version"] = self.expected_version
        if self.objects:
            payload["objects"] = self.objects
        return payload
