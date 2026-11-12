# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""External observation models — provider claims, not accepted canonical truth."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ObservationSource(StrEnum):
    WEBHOOK = "WEBHOOK"
    RECONCILIATION = "RECONCILIATION"
    MANUAL_IMPORT = "MANUAL_IMPORT"


class ObservationDecision(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    NO_CHANGE = "NO_CHANGE"
    STALE = "STALE"
    ECHO = "ECHO"
    REJECT_LIFECYCLE_INVALID = "REJECT_LIFECYCLE_INVALID"


UNKNOWN_EXTERNAL_ACTOR = "UNKNOWN_EXTERNAL_ACTOR"


class FieldChange(BaseModel):
    previous_observed: Any | None = None
    observed: Any


class ExternalObservation(BaseModel):
    """Provider X reports that canonical-relevant fact Y currently looks like Z."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    object_id: str
    provider_type: str = "generic"
    provider_name: str
    external_id: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: ObservationSource = ObservationSource.WEBHOOK
    changes: dict[str, FieldChange] = Field(default_factory=dict)
    provider_actor_id: str | None = None
    provider_event_id: str | None = None
    provider_version: str | None = None
    provider_sequence: int | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    state_change: FieldChange | None = None

    def dedupe_key(self) -> str:
        if self.provider_event_id:
            return f"{self.provider_name}:{self.provider_event_id}"
        parts = [
            self.provider_name,
            self.external_id,
            self.provider_version or "",
            str(self.provider_sequence or ""),
            ",".join(sorted(self.changes.keys())),
        ]
        if self.state_change:
            parts.append(f"state:{self.state_change.observed}")
        return ":".join(parts)


class ObservationResult(BaseModel):
    observation_id: str
    decision: ObservationDecision
    reason: str = ""
    differences: list[str] = Field(default_factory=list)
    field_results: dict[str, str] = Field(default_factory=dict)
    repair_candidates: list[str] = Field(default_factory=list)
    canonical_event_id: str | None = None
    review_case_id: str | None = None
    object_version: int | None = None
