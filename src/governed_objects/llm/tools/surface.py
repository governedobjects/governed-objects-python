# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Experimental optional-use tool surface over a trusted bound agent session."""

from __future__ import annotations

from typing import Any

from governed_objects.agent.session import GovernedAgentSession
from governed_objects.domain.command import TransitionState
from governed_objects.domain.models import Decision
from governed_objects.policy.context import AuthorizationContextError
from governed_objects.llm.model import ToolCall, ToolDefinition
from governed_objects.llm.tools.registry import is_registered_tool, tool_definitions
from governed_objects.llm.tools.results import ToolResult
from governed_objects.llm.tools.schemas import (
    ExecuteActionInput,
    GetHistoryInput,
    QueryObjectsInput,
    ReadObjectInput,
    TraverseRelationshipsInput,
    parse_tool_input,
)


class GovernedAgentTools:
    """Fixed tool surface — model receives this, not the runtime."""

    def __init__(self, session: GovernedAgentSession) -> None:
        self._session = session

    @property
    def session(self) -> GovernedAgentSession:
        return self._session

    @property
    def bound_context(self) -> dict[str, str]:
        mandate = self._session.auth.mandate
        return {
            "actor_id": self._session.actor.id,
            "application_id": self._session._application_id,
            "mandate_id": self._session._mandate_id,
            "principal_id": mandate.principal_id if mandate else "",
            "purpose": self._session._purpose,
        }

    def definitions(self) -> list[ToolDefinition]:
        return tool_definitions()

    def execute(self, tool_call: ToolCall) -> ToolResult:
        if not is_registered_tool(tool_call.name):
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=False,
                error=f"unknown tool: {tool_call.name}",
            )
        handler = {
            "query_objects": self._query_objects,
            "read_object": self._read_object,
            "traverse_relationships": self._traverse_relationships,
            "execute_action": self._execute_action,
            "get_history": self._get_history,
        }[tool_call.name]
        try:
            return handler(tool_call)
        except AuthorizationContextError:
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=False,
                error="authorization context unavailable",
            )

    def _query_objects(self, tool_call: ToolCall) -> ToolResult:
        parsed, err = parse_tool_input(QueryObjectsInput, tool_call.arguments)
        if err:
            return self._schema_error(tool_call, err)
        assert parsed is not None
        objects = self._session.query(
            object_type=parsed.object_type,
            state=parsed.state,
            include_subtypes=parsed.include_subtypes,
        )
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=True,
            decision="ALLOW",
            objects=[
                {
                    "id": o.id,
                    "type": o.type,
                    "state": o.state,
                    "version": o.version,
                    "data": o.data,
                }
                for o in objects
            ],
        )

    def _read_object(self, tool_call: ToolCall) -> ToolResult:
        parsed, err = parse_tool_input(ReadObjectInput, tool_call.arguments)
        if err:
            return self._schema_error(tool_call, err)
        assert parsed is not None
        result = self._session.read(parsed.object_id)
        if result.decision == Decision.DENY:
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=True,
                decision="DENY",
                reason=result.deny_reason or "ACCESS_DENIED",
                object_id=parsed.object_id,
                message="Object unavailable or access denied.",
            )
        stored = self._session._runtime.get_object(parsed.object_id)
        state = stored.state if stored else result.new_state
        version = stored.version if stored else result.object_version
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=True,
            decision="ALLOW",
            object_id=parsed.object_id,
            state=state,
            version=version,
            objects=[{
                "id": parsed.object_id,
                "state": state,
                "version": version,
                "data": result.data,
            }],
        )

    def _traverse_relationships(self, tool_call: ToolCall) -> ToolResult:
        parsed, err = parse_tool_input(TraverseRelationshipsInput, tool_call.arguments)
        if err:
            return self._schema_error(tool_call, err)
        assert parsed is not None
        relations = [parsed.relation] if parsed.relation else None
        results = self._session.traverse(
            parsed.object_id,
            max_depth=parsed.max_depth,
            relations=relations,
        )
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=True,
            decision="ALLOW",
            objects=[
                {
                    "id": r.object.id,
                    "type": r.object.type,
                    "state": r.object.state,
                    "version": r.object.version,
                    "relation": r.path[-1].relation if r.path else None,
                    "data": r.object.data,
                }
                for r in results
            ],
        )

    def _execute_action(self, tool_call: ToolCall) -> ToolResult:
        parsed, err = parse_tool_input(ExecuteActionInput, tool_call.arguments)
        if err:
            return self._schema_error(tool_call, err)
        assert parsed is not None
        result = self._session.execute(
            TransitionState(parsed.object_id, parsed.action),
            expected_version=parsed.expected_version,
        )
        if result.decision == Decision.CONFLICT:
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=True,
                decision="CONFLICT",
                reason=result.conflict_reason,
                object_id=parsed.object_id,
                expected_version=result.expected_version,
                actual_version=result.actual_version,
                state=result.new_state,
                message="Object changed. Refresh before deciding again.",
            )
        if result.decision == Decision.DENY:
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=True,
                decision="DENY",
                reason=result.deny_reason or "DENIED",
                object_id=parsed.object_id,
                state=result.new_state,
                version=result.object_version,
            )
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=True,
            decision="ALLOW",
            object_id=parsed.object_id,
            state=result.new_state,
            version=result.object_version,
        )

    def _get_history(self, tool_call: ToolCall) -> ToolResult:
        parsed, err = parse_tool_input(GetHistoryInput, tool_call.arguments)
        if err:
            return self._schema_error(tool_call, err)
        assert parsed is not None
        events = self._session.history(parsed.object_id)
        if not events:
            read = self._session.read(parsed.object_id)
            if read.decision != Decision.ALLOW:
                return ToolResult(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    success=True,
                    decision="DENY",
                    reason=read.deny_reason or "ACCESS_DENIED",
                    object_id=parsed.object_id,
                )
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=True,
            decision="ALLOW",
            object_id=parsed.object_id,
            objects=[
                {
                    "action": e.action,
                    "decision": e.decision.value,
                    "previous_state": e.previous_state,
                    "new_state": e.new_state,
                    "object_version": e.object_version,
                }
                for e in events
            ],
        )

    @staticmethod
    def _schema_error(tool_call: ToolCall, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            success=False,
            error=message,
            schema_rejected=True,
        )
