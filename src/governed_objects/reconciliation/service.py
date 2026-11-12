# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Reconciliation — detect drift and produce external observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from governed_objects.adapters.context import AdapterContext
from governed_objects.adapters.registry import AdapterRegistry
from governed_objects.domain.models import GovernedObject
from governed_objects.observations.models import ExternalObservation, ObservationResult
from governed_objects.observations.ingestion import ObservationIngestionService
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.errors import ProviderError, ProviderObjectNotFound, ProviderUnavailable
from governed_objects.observations.mapping import reconciliation_to_observation


class ReconciliationStatus(StrEnum):
    IN_SYNC = "IN_SYNC"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    LOCAL_OBJECT = "LOCAL_OBJECT"
    OBSERVATION_INGESTED = "OBSERVATION_INGESTED"


@dataclass
class ReconciliationResult:
    object_id: str
    status: ReconciliationStatus
    canonical_state: str | None = None
    provider_state: str | None = None
    differences: list[str] = field(default_factory=list)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    provider_binding: dict[str, Any] | None = None
    observation_result: ObservationResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "status": self.status.value,
            "canonical_state": self.canonical_state,
            "provider_state": self.provider_state,
            "differences": self.differences,
            "observed_at": self.observed_at.isoformat(),
            "provider_binding": self.provider_binding,
            "observation_result": (
                self.observation_result.model_dump() if self.observation_result else None
            ),
        }


class ReconciliationService:
    """Compare stored canonical state with provider view; optionally ingest observations."""

    def __init__(
        self,
        store: SQLiteStore,
        adapters: AdapterRegistry,
        ingestion: ObservationIngestionService | None = None,
    ) -> None:
        self._store = store
        self._adapters = adapters
        self._ingestion = ingestion

    def check(self, object_id: str, context: AdapterContext) -> ReconciliationResult:
        stored = self._store.get(object_id)
        if stored is None:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.PROVIDER_NOT_FOUND,
            )

        binding = ProviderBinding.from_metadata(stored.metadata)
        if binding is None or binding.is_local:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.LOCAL_OBJECT,
                canonical_state=stored.state,
            )

        adapter = self._adapters.resolve(stored)
        try:
            hydrated = adapter.load(stored, context)
        except ProviderObjectNotFound:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.PROVIDER_NOT_FOUND,
                canonical_state=stored.state,
                provider_binding=binding.to_metadata().get("provider_binding"),
            )
        except (ProviderUnavailable, ProviderError):
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.PROVIDER_UNAVAILABLE,
                canonical_state=stored.state,
                provider_binding=binding.to_metadata().get("provider_binding"),
            )

        if hydrated is None:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.IN_SYNC,
                canonical_state=stored.state,
                provider_binding=binding.to_metadata().get("provider_binding"),
            )

        return self._compare(stored, hydrated.object, binding)

    def reconcile_and_ingest(
        self, object_id: str, context: AdapterContext
    ) -> ReconciliationResult:
        """Poll provider, build observation, feed authority pipeline."""
        stored = self._store.get(object_id)
        if stored is None:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.PROVIDER_NOT_FOUND,
            )

        binding = ProviderBinding.from_metadata(stored.metadata)
        if binding is None or binding.is_local:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.LOCAL_OBJECT,
                canonical_state=stored.state,
            )

        if self._ingestion is None:
            return self.check(object_id, context)

        adapter = self._adapters.resolve(stored)
        try:
            hydrated = adapter.load(stored, context)
        except (ProviderObjectNotFound, ProviderUnavailable, ProviderError):
            return self.check(object_id, context)

        if hydrated is None:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.IN_SYNC,
                canonical_state=stored.state,
            )

        observation = reconciliation_to_observation(
            object_id=object_id,
            provider_name=binding.provider_name,
            provider_type=binding.provider_type,
            external_id=binding.external_id or object_id,
            canonical=stored.data,
            canonical_state=stored.state,
            provider_data=hydrated.object.data,
            provider_state=hydrated.object.state,
        )
        if observation is None:
            return ReconciliationResult(
                object_id=object_id,
                status=ReconciliationStatus.IN_SYNC,
                canonical_state=stored.state,
                provider_state=hydrated.object.state,
            )

        result = self._ingestion.ingest(observation)
        differences = self._diff_labels(stored, hydrated.object)
        return ReconciliationResult(
            object_id=object_id,
            status=ReconciliationStatus.OBSERVATION_INGESTED,
            canonical_state=self._store.get(object_id).state if self._store.get(object_id) else stored.state,
            provider_state=hydrated.object.state,
            differences=differences,
            provider_binding=binding.to_metadata().get("provider_binding"),
            observation_result=result,
        )

    def _compare(
        self,
        stored: GovernedObject,
        provider_view: GovernedObject,
        binding: ProviderBinding,
    ) -> ReconciliationResult:
        differences = self._diff_labels(stored, provider_view)
        status = (
            ReconciliationStatus.DRIFT_DETECTED
            if differences
            else ReconciliationStatus.IN_SYNC
        )
        return ReconciliationResult(
            object_id=stored.id,
            status=status,
            canonical_state=stored.state,
            provider_state=provider_view.state,
            differences=differences,
            provider_binding=binding.to_metadata().get("provider_binding"),
        )

    @staticmethod
    def _diff_labels(stored: GovernedObject, provider_view: GovernedObject) -> list[str]:
        differences: list[str] = []
        if stored.state != provider_view.state:
            differences.append(
                f"state: canonical={stored.state} provider={provider_view.state}"
            )
        for key, value in provider_view.data.items():
            if stored.data.get(key) != value:
                differences.append(
                    f"{key}: canonical={stored.data.get(key)} provider={value}"
                )
        return differences
