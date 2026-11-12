# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Declarative type definitions for governed objects."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FieldSchema(BaseModel):
    type: str
    required: bool = False


class ActionDef(BaseModel):
    requires: list[str] = Field(default_factory=list)
    open: bool = False


class StateDef(BaseModel):
    transitions: dict[str, str] = Field(default_factory=dict)
    terminal: bool = False


class FieldAccessRule(BaseModel):
    read: list[str] = Field(default_factory=list)


class ObjectTypeDefinition(BaseModel):
    """Declarative definition for a governed object type."""

    kind: str
    type_version: str = "1"
    extends: str | None = None
    fields: dict[str, FieldSchema] = Field(default_factory=dict, alias="schema")
    states: dict[str, StateDef] = Field(default_factory=dict)
    actions: dict[str, ActionDef] = Field(default_factory=dict)
    field_access: dict[str, FieldAccessRule] = Field(default_factory=dict)
    initial_state: str | None = None
    read_requires: list[str] = Field(default_factory=list)
    read_open: bool = False
    canonical_state_map: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @property
    def schema(self) -> dict[str, FieldSchema]:
        """Alias for fields — matches YAML key name."""
        return self.fields

    def default_state(self) -> str:
        if self.initial_state:
            return self.initial_state
        if self.states:
            return next(iter(self.states))
        return "active"

    def required_capabilities(self, action: str) -> list[str]:
        if action == "read":
            return self.read_requires
        action_def = self.actions.get(action)
        if action_def:
            return action_def.requires
        return []

    def is_open(self, action: str) -> bool:
        """Whether ``action`` (or ``"read"``) is explicitly opened to every
        actor regardless of declared capabilities. An undeclared or empty
        requirement list denies unless this marker is set."""
        if action == "read":
            return self.read_open
        action_def = self.actions.get(action)
        return bool(action_def and action_def.open)

    def transition_target(self, current_state: str, action: str) -> str | None:
        state_def = self.states.get(current_state)
        if not state_def:
            return None
        return state_def.transitions.get(action)

    def is_terminal(self, state: str) -> bool:
        state_def = self.states.get(state)
        return bool(state_def and state_def.terminal)

    def canonical_state(self, domain_state: str) -> str:
        """Map domain-specific lifecycle state to canonical primitive state."""
        return self.canonical_state_map.get(domain_state, domain_state)
