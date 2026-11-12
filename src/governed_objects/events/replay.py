# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Event replay — reconstruct canonical state from event history."""

from __future__ import annotations

from governed_objects.domain.models import Decision, GovernedObject, GovernanceEvent, Relationship
from governed_objects.events.lineage import has_legacy_history, is_read_audit
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.relationships.canonical import CANONICAL_SOURCE, canonical_relationship
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition


class ReplayRefused(Exception):
    """Content-free refusal of unestablished reconstruction support/binding."""

    code = "REPLAY_NOT_SUPPORTED"

    def __init__(self) -> None:
        super().__init__("reconstruction unavailable")


def bound_type_definition(
    types: TypeRegistry | None, obj: GovernedObject
) -> ObjectTypeDefinition | None:
    """Resolve an explicit nonempty binding, never replacement semantics."""
    if types is None or not obj.type or not obj.type_version:
        return None
    try:
        definition = types.get(obj.type)
    except KeyError:
        return None
    if (definition.kind, definition.type_version) != (obj.type, obj.type_version):
        return None
    return definition


class LegacyHistoryRefused(Exception):
    """Replay of history without recorded positions was not explicitly requested."""

    def __init__(self, object_id: str) -> None:
        super().__init__(f"{object_id}: history has unpositioned accepted rows (legacy)")
        self.object_id = object_id


class EventReplayer:
    """Reconstruct canonical state from accepted revisions using type reducers.

    Canonical mode (default) consumes only positioned accepted records, in
    recorded position order, and restores each revision's recorded Object
    Version. Objects whose history contains unpositioned accepted rows are
    refused unless ``allow_legacy=True``. Legacy mode orders those rows by
    timestamp and is not Draft 2.2-canonical replay.
    """

    def __init__(
        self,
        store: SQLiteStore,
        extensions: TypeExtensionRegistry,
        type_registry: TypeRegistry | None = None,
    ) -> None:
        if extensions is None:
            raise ValueError("replay requires non-None extensions")
        self._store = store
        self._extensions = extensions
        self._types = type_registry

    def reconstruct(self, object_id: str, *, allow_legacy: bool = False) -> GovernedObject | None:
        stored = self._store.get(object_id)
        if stored is None:
            return None

        events = self._admitted_events(stored, allow_legacy)
        if not events:
            return stored

        reducer = self._extensions.reducer_for(stored.type)
        type_version = stored.type_version

        if reducer is None:
            return stored.model_copy(update={"type_version": type_version})

        state = reducer.reduce(None, events[0])
        if state is None:
            return stored.model_copy(update={"type_version": type_version})
        self._check_result(stored, state)

        version = events[0].object_version
        current_state = state.state
        data = dict(state.data)

        for event in events[1:]:
            current_obj = GovernedObject(
                id=object_id,
                type=stored.type,
                type_version=type_version,
                version=version,
                state=current_state,
                data=data,
            )
            version = event.object_version
            result = reducer.reduce(current_obj, event)
            if result is None:
                continue
            self._check_result(stored, result)
            current_state = result.state
            data = result.data

        return GovernedObject(
            id=object_id,
            type=stored.type,
            type_version=type_version,
            version=version,
            state=current_state,
            data=data,
            metadata=stored.metadata,
            governance_reference=stored.governance_reference,
        )

    def reconstruct_relationships(
        self, object_id: str, *, allow_legacy: bool = False
    ) -> list[Relationship]:
        stored = self._store.get(object_id)
        if stored is None:
            return []
        events = self._admitted_events(stored, allow_legacy)
        rel_index: dict[tuple[str, str], Relationship] = {}
        for event in events:
            self._apply_relationship_event(object_id, event, rel_index)
        return list(rel_index.values())

    def _admitted_events(
        self, stored: GovernedObject, allow_legacy: bool
    ) -> list[GovernanceEvent]:
        if not self.check_type_version_supported(stored):
            raise ReplayRefused()
        events = self._accepted_events(stored.id, allow_legacy)
        # Preflight the whole selection before invoking any reducer or edge fold.
        for event in events:
            if (event.object_id, event.object_type, event.type_version) != (
                stored.id, stored.type, stored.type_version
            ):
                raise ReplayRefused()
            if event.action == "create" and any(
                rel.get("source_id", stored.id) != stored.id
                for rel in event.details.get("relationships", [])
            ):
                raise ReplayRefused()
        return events

    @staticmethod
    def _check_result(stored: GovernedObject, result: GovernedObject) -> None:
        if (result.id, result.type) != (stored.id, stored.type) or (
            "type_version" in result.model_fields_set
            and result.type_version is not None
            and result.type_version != stored.type_version
        ):
            raise ReplayRefused()

    def is_legacy(self, object_id: str) -> bool:
        return has_legacy_history(self._store, object_id)

    def _accepted_events(self, object_id: str, allow_legacy: bool) -> list[GovernanceEvent]:
        if not self.is_legacy(object_id):
            return self._store.list_accepted_for_object(object_id)
        if not allow_legacy:
            raise LegacyHistoryRefused(object_id)
        return [
            e for e in self._store.list_for_object(object_id)
            if e.decision == Decision.ALLOW and not is_read_audit(e)
        ]

    @staticmethod
    def _apply_relationship_event(
        object_id: str,
        event: GovernanceEvent,
        rel_index: dict[tuple[str, str], Relationship],
    ) -> None:
        if event.action == "create":
            for rel_data in event.details.get("relationships", []):
                rel = canonical_relationship(
                    rel_data.get("source_id", object_id),
                    rel_data["relation"],
                    rel_data["target_id"],
                    target_type=rel_data.get("target_type"),
                )
                rel_index[(rel.relation, rel.target_id)] = rel
            return

        if event.action == "add_relationship":
            details = event.details
            rel = canonical_relationship(
                object_id,
                details["relation"],
                details["target_id"],
                target_type=details.get("target_type"),
            )
            rel_index[(rel.relation, rel.target_id)] = rel
            return

        if event.action == "remove_relationship":
            details = event.details
            rel_index.pop((details["relation"], details["target_id"]), None)

    def check_type_version_supported(self, obj: GovernedObject) -> bool:
        return bound_type_definition(self._types, obj) is not None
