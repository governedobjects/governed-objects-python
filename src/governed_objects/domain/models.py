# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Core domain models for the Governed Object runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    CONFLICT = "CONFLICT"


class ActorType(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"


class Relationship(BaseModel):
    source_id: str
    relation: str
    target_id: str
    target_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GovernedObject(BaseModel):
    """Materialized state of a governed object."""

    id: str
    type: str
    type_version: str = "1"
    version: int = 1
    state: str
    data: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    contract: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    storage_ref: str | None = None
    # EXPERIMENTAL: opaque type-scoped governance
    # reference recorded at creation from the configured governance. Grants nothing.
    governance_reference: str | None = None


class GovernanceEvent(BaseModel):
    """Append-only event in object history."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    object_id: str
    object_type: str
    type_version: str | None = None
    actor_id: str
    actor_type: str
    action: str
    decision: Decision
    event_type: str
    previous_state: str | None = None
    new_state: str | None = None
    reason: str = ""
    object_version: int = 1
    details: dict[str, Any] = Field(default_factory=dict)
    # EXPERIMENTAL: per-object accepted-history
    # position assigned by the store when an accepted canonical change is
    # appended. None for every other row (reads, denials, conflicts, legacy rows).
    history_position: int | None = None
    # EXPERIMENTAL: False only on a row
    # restored by interchange import whose artifact carried no acceptance
    # instant; ``timestamp`` is then a placeholder, not evidence.
    accepted_at_recorded: bool = True


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    message: str | None = None


class CommandResult(BaseModel):
    decision: Decision
    outcome_category: str | None = None
    reason: str = ""
    deny_reason: str | None = None
    conflict_reason: str | None = None
    expected_version: int | None = None
    actual_version: int | None = None
    action: str = ""
    object_id: str | None = None
    previous_state: str | None = None
    new_state: str | None = None
    object_version: int | None = None
    events: list[GovernanceEvent] = Field(default_factory=list)
    validation_checks: list[ValidationCheck] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class Actor(BaseModel):
    id: str
    type: ActorType
    capabilities: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    display_name: str | None = None
