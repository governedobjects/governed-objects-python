# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Outbound provider repair models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class RepairStatus(StrEnum):
    PLANNED = "PLANNED"
    APPLIED = "APPLIED"
    ALREADY_IN_SYNC = "ALREADY_IN_SYNC"
    CANONICAL_CONFLICT = "CANONICAL_CONFLICT"
    PROVIDER_CONFLICT = "PROVIDER_CONFLICT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    PROVIDER_INVALID_RESPONSE = "PROVIDER_INVALID_RESPONSE"
    PROVIDER_OUTCOME_UNKNOWN = "PROVIDER_OUTCOME_UNKNOWN"
    UNREPAIRABLE_FIELD = "UNREPAIRABLE_FIELD"
    REJECTED_TAMPER = "REJECTED_TAMPER"
    UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"


class ProviderRepairRequest(BaseModel):
    """Organization intends to make provider conform to accepted canonical state."""

    repair_id: str = Field(default_factory=lambda: str(uuid4()))
    object_id: str
    provider_name: str
    external_id: str
    canonical_expected_version: int
    provider_expected_version: str
    changes: dict[str, Any] = Field(default_factory=dict)
    reason: str = "CANONICAL_AUTHORITATIVE_DRIFT"
    originating_observation_id: str | None = None
    review_case_id: str | None = None
    correlation_id: str | None = None
    initiated_by: str = "system"


class ProviderRepairPlan(BaseModel):
    """Dry-run repair plan derived only from canonical accepted state."""

    plan_id: str = Field(default_factory=lambda: str(uuid4()))
    object_id: str
    provider_name: str
    external_id: str
    type_identity: str
    type_version: str
    projection_token: str
    canonical_expected_version: int
    provider_expected_version: str
    changes: dict[str, Any] = Field(default_factory=dict)
    drifts: list[str] = Field(default_factory=list)
    repair_fields: list[str] = Field(default_factory=list)
    projection_gaps: list[str] = Field(default_factory=list)
    unrepairable_fields: list[str] = Field(default_factory=list)
    reason: str = "CANONICAL_AUTHORITATIVE_DRIFT"
    originating_observation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_request(self, *, initiated_by: str = "system") -> ProviderRepairRequest:
        return ProviderRepairRequest(
            repair_id=self.plan_id,
            object_id=self.object_id,
            provider_name=self.provider_name,
            external_id=self.external_id,
            canonical_expected_version=self.canonical_expected_version,
            provider_expected_version=self.provider_expected_version,
            changes=dict(self.changes),
            reason=self.reason,
            originating_observation_id=self.originating_observation_id,
            initiated_by=initiated_by,
        )


class ProviderRepairResult(BaseModel):
    object_id: str
    repair_id: str
    status: RepairStatus
    reason: str = ""
    canonical_expected_version: int | None = None
    canonical_actual_version: int | None = None
    provider_expected_version: str | None = None
    provider_actual_version: str | None = None
    provider_name: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
