# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Bounded model/tool execution loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from governed_objects.llm.model import ReasoningModel
from governed_objects.llm.tools.results import ToolResult
from governed_objects.llm.tools.surface import GovernedAgentTools
from governed_objects.llm.trace import AgentExecutionTrace


@dataclass
class AgentLoopResult:
    trace: AgentExecutionTrace
    completed: bool = False
    stopped_reason: str = ""
    business_actions_allowed: int = 0
    duplicate_transitions: int = 0


class GovernedAgentLoop:
    """Bounded loop: model proposes tool calls, substrate executes and returns results."""

    DEFAULT_MAX_STEPS = 8
    DEFAULT_MAX_TOOL_CALLS = 16

    def __init__(
        self,
        model: ReasoningModel,
        tools: GovernedAgentTools,
        *,
        task: str = "",
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ) -> None:
        self._model = model
        self._tools = tools
        self._task = task
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls

    def run(self) -> AgentLoopResult:
        trace = AgentExecutionTrace(
            task=self._task,
            model_provider=self._model.provider,
            model_name=self._model.model_name,
            bound_context=self._tools.bound_context,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._task}]
        tool_call_count = 0
        business_allowed = 0
        seen_executions: set[tuple[str, str, int]] = set()
        duplicate_transitions = 0

        for step in range(self._max_steps):
            trace.steps = step + 1
            response = self._model.respond(messages, self._tools.definitions())
            if response.final is not None:
                trace.final_message = response.final
                return AgentLoopResult(
                    trace=trace,
                    completed=True,
                    stopped_reason="model_final",
                    business_actions_allowed=business_allowed,
                    duplicate_transitions=duplicate_transitions,
                )
            if not response.tool_calls:
                trace.final_message = "No tool calls — stopping."
                return AgentLoopResult(
                    trace=trace,
                    completed=True,
                    stopped_reason="no_tool_calls",
                    business_actions_allowed=business_allowed,
                    duplicate_transitions=duplicate_transitions,
                )

            for tool_call in response.tool_calls:
                if tool_call_count >= self._max_tool_calls:
                    trace.final_message = "Max tool calls reached."
                    return AgentLoopResult(
                        trace=trace,
                        completed=False,
                        stopped_reason="max_tool_calls",
                        business_actions_allowed=business_allowed,
                        duplicate_transitions=duplicate_transitions,
                    )

                result = self._tools.execute(tool_call)
                trace.record(step + 1, tool_call.name, tool_call.arguments, result)
                tool_call_count += 1

                if (
                    tool_call.name == "execute_action"
                    and result.decision == "ALLOW"
                ):
                    key = (result.object_id or "", tool_call.arguments.get("action", ""), result.version or 0)
                    if key in seen_executions:
                        duplicate_transitions += 1
                    seen_executions.add(key)
                    business_allowed += 1

                if result.decision == "CONFLICT":
                    trace.refreshes += 1

                messages.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result.to_model_dict(),
                })

        trace.final_message = "Max steps reached."
        return AgentLoopResult(
            trace=trace,
            completed=False,
            stopped_reason="max_steps",
            business_actions_allowed=business_allowed,
            duplicate_transitions=duplicate_transitions,
        )
