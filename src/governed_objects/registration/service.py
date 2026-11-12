# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Stage provider registration before its first accepted canonical image."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from governed_objects.adapters.context import AdapterContext
from governed_objects.adapters.registry import AdapterRegistry
from governed_objects.domain.models import ActorType, Decision, GovernedObject, GovernanceEvent
from governed_objects.materialization.service import MaterializationService
# Kept as a generic compatibility re-export; registration no longer fabricates
# a bootstrap observation after an accepted empty create.
from governed_objects.observations.mapping import reconciliation_to_observation
from governed_objects.observations.validation import (
    CandidateMode, CandidateVerdict, candidate_image, capture_admission, validate_candidate,
)
from governed_objects.persistence.sqlite import IdentityAlreadyExists, SQLiteStore
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.errors import ProviderError
from governed_objects.relationships.sync import RelationshipSyncService
from governed_objects.types.registry import TypeRegistry

if TYPE_CHECKING:
    from governed_objects.observations.ingestion import ObservationIngestionService


class RegistrationRefused(Exception):
    """Content-free failure to establish a valid supported initial image."""
    code = "REGISTRATION_NOT_ACCEPTED"

    def __init__(self) -> None:
        super().__init__("registration unavailable")


class RegistrationService:
    def __init__(self, store: SQLiteStore, adapter_registry: AdapterRegistry,
                 materializer: MaterializationService, type_registry: TypeRegistry,
                 observation_ingestion: ObservationIngestionService | None = None,
                 rel_sync: RelationshipSyncService | None = None) -> None:
        self._store = store
        self._adapters = adapter_registry
        self._materializer = materializer
        if type_registry is None:
            raise ValueError("registration requires an explicit TypeRegistry")
        self._types = type_registry
        self._observations = observation_ingestion
        self._rel_sync = rel_sync if rel_sync is not None else RelationshipSyncService(store)

    def register_with_binding(self, governed_id: str, object_type: str, binding: ProviderBinding,
                              *, state: str | None = None, context: AdapterContext) -> GovernedObject:
        definition = self._types.get(object_type)
        if not definition.type_version or "type_version" not in definition.model_fields_set:
            raise ValueError("registration requires a declared Type Version")
        if state is None:
            if not definition.initial_state and not definition.states:
                raise ValueError("registration requires a declared default state or explicit state")
            initial_state = definition.default_state()
        else:
            initial_state = state
        if not initial_state or (definition.states and initial_state not in definition.states):
            raise ValueError("registration state is not declared by the supplied type")
        if self._store.identities_present([governed_id]):
            raise IdentityAlreadyExists([governed_id])
        # Carrier version1 is not issued or persisted until admission commits.
        obj = GovernedObject(id=governed_id, type=object_type, type_version=definition.type_version,
                             version=1, state=initial_state, data={}, metadata=binding.to_metadata())
        relationships = []
        try:
            snapshot = capture_admission(self._types, self._adapters, obj)
            if not binding.is_local:
                hydrated = self._materializer.observe_provider(obj, context)
                if hydrated is None or (hydrated.object.id, hydrated.object.type, hydrated.object.type_version) != (
                    obj.id, obj.type, obj.type_version
                ):
                    raise RegistrationRefused()
                if state is not None and state != hydrated.object.state:
                    raise RegistrationRefused()
                obj = obj.model_copy(update={"data": deepcopy(hydrated.object.data), "state": hydrated.object.state})
                relationships = [rel.model_copy(deep=True) for rel in hydrated.relationships]
                if any(rel.source_id != obj.id for rel in relationships):
                    raise RegistrationRefused()
                provider_version = hydrated.provider_metadata.get("_provider_version")
                if isinstance(provider_version, str):
                    obj.metadata["_observation_state"] = {"last_accepted_provider_version": provider_version}
            verdict = validate_candidate(type_def=snapshot.definition, current=None, candidate=candidate_image(obj),
                                         mode=CandidateMode.REGISTRATION_INITIAL, adapter=snapshot.adapter)
            if verdict != CandidateVerdict.VALID or not snapshot.matches(self._types, self._adapters, obj):
                raise RegistrationRefused()
        except ProviderError:
            raise
        except Exception:
            raise RegistrationRefused() from None
        with self._store.observation_transaction():
            if self._store.identities_present([governed_id]):
                raise IdentityAlreadyExists([governed_id])
            if not snapshot.matches(self._types, self._adapters, obj):
                raise RegistrationRefused()
            self._store.save(obj)
            self._append_create_event(obj, source="provider_registration")
            if relationships:
                self._rel_sync.sync_provider_relationships(governed_id, relationships, provider_name=binding.provider_name)
        return self._store.get(governed_id) or obj

    def _append_create_event(self, obj: GovernedObject, *, source: str) -> None:
        self._store.append_accepted(GovernanceEvent(
            object_id=obj.id, object_type=obj.type, type_version=obj.type_version,
            actor_id="system", actor_type=ActorType.SERVICE.value, action="create", decision=Decision.ALLOW,
            event_type="object.create", previous_state=None, new_state=obj.state, reason="object created",
            object_version=obj.version, details={"data": dict(obj.data), "source": source},
        ))
