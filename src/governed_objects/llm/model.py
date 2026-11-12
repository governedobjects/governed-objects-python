# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Reasoning model protocol — vendor-neutral LLM adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    tool_calls: list[ToolCall] = field(default_factory=list)
    final: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


class ReasoningModel(Protocol):
    """Vendor-neutral interface for probabilistic reasoning models."""

    @property
    def provider(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def respond(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> ModelResponse: ...
