# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Operational execution trace — separate from canonical business events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from governed_objects.llm.tools.results import ToolResult


@dataclass
class ToolTraceEntry:
    step: int
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    decision: str | None = None


@dataclass
class AgentExecutionTrace:
    run_id: str = field(default_factory=lambda: str(uuid4()))
    task: str = ""
    model_provider: str = ""
    model_name: str = ""
    bound_context: dict[str, str] = field(default_factory=dict)
    tool_calls: list[ToolTraceEntry] = field(default_factory=list)
    final_message: str | None = None
    steps: int = 0
    conflicts: int = 0
    refreshes: int = 0
    blind_retries: int = 0
    unauthorized_attempts: int = 0
    schema_rejections: int = 0

    def record(self, step: int, tool_name: str, arguments: dict, result: ToolResult) -> None:
        decision = result.decision or ("ERROR" if result.error else None)
        entry = ToolTraceEntry(
            step=step,
            tool_name=tool_name,
            arguments=arguments,
            result=result.to_model_dict(),
            decision=decision,
        )
        self.tool_calls.append(entry)
        if result.decision == "CONFLICT":
            self.conflicts += 1
        if result.schema_rejected:
            self.schema_rejections += 1
        if result.decision == "DENY" and result.reason not in {
            None,
            "NOT_FOUND",
            "ACCESS_DENIED",
        }:
            self.unauthorized_attempts += 1
