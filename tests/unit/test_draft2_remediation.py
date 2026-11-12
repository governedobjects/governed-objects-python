# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Draft2 Core remediation — Type Version, relationships, outcomes, replay."""

from __future__ import annotations
from tests.generic_bootstrap import generic_dependencies

from pathlib import Path

import pytest
import yaml

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import AdapterContext, AdapterExecuteResult
from governed_objects.domain.actors import ActorRegistry
from governed_objects.domain.command import Command, CreateObject, TransitionState
from governed_objects.domain.models import Actor, GovernedObject, ValidationCheck
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Decision
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.policy.outcomes import OutcomeCategory
from governed_objects.policy.reasons import DenyReason
from governed_objects.relationships.canonical import is_canonical, list_canonical_for_object
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator


class Draft2WorkItemAdapter(TypeAdapter):
    def supports(self, object_type: str) -> bool:
        return object_type == "Draft2WorkItem"

    def validate_create(
        self, type_def: ObjectTypeDefinition, data: dict
    ) -> tuple[bool, list[ValidationCheck], str]:
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        action: str,
        actor: Actor,
    ) -> tuple[bool, list[ValidationCheck], str]:
        return True, [], ""

    def adapter_execute(
        self,
        obj: GovernedObject,
        command: Command,
        context: AdapterContext,
    ) -> AdapterExecuteResult | None:
        if command.action != "update_title":
            return None
        title = (command.payload or {}).get("title", obj.data.get("title"))
        new_data = dict(obj.data)
        new_data["title"] = title
        return AdapterExecuteResult(
            success=True,
            object=obj.model_copy(update={"data": new_data, "version": obj.version + 1}),
        )

    def filter_read_data(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        actor: Actor,
        fields: list[str],
    ) -> tuple[dict, list[str]]:
        return obj.data or {}, []


@pytest.fixture
def draft2_types(tmp_path: Path) -> Path:
    types_dir = tmp_path / "types"
    types_dir.mkdir()
    (types_dir / "work_item.yaml").write_text(
        yaml.dump(
            {
                "kind": "Draft2WorkItem",
                "version": "1",
                "schema": {"title": {"type": "string", "required": True}},
                "read_requires": ["draft2.read"],
                "actions": {
                    "create": {"requires": ["draft2.create"]},
                    "update_title": {"requires": ["draft2.mutate"]},
                    "add_relationship": {"requires": ["draft2.mutate"]},
                    "remove_relationship": {"requires": ["draft2.mutate"]},
                },
                "states": {
                    "draft": {"transitions": {"update_title": "draft"}},
                    "active": {"transitions": {"update_title": "active"}},
                },
                "initial_state": "draft",
            }
        )
    )
    return types_dir


@pytest.fixture
def draft2_actors(tmp_path: Path) -> Path:
    actors_dir = tmp_path / "actors"
    actors_dir.mkdir()
    (actors_dir / "writer.yaml").write_text(
        yaml.dump(
            {
                "apiVersion": "governedcontainer/v1alpha1",
                "kind": "Actor",
                "metadata": {"id": "writer"},
                "spec": {
                    "type": "service",
                    "capabilities": ["draft2.read", "draft2.mutate", "draft2.create"],
                },
            }
        )
    )
    (actors_dir / "reader.yaml").write_text(
        yaml.dump(
            {
                "apiVersion": "governedcontainer/v1alpha1",
                "kind": "Actor",
                "metadata": {"id": "reader"},
                "spec": {"type": "service", "capabilities": ["draft2.read"]},
            }
        )
    )
    return actors_dir


@pytest.fixture
def runtime(tmp_path: Path, draft2_types: Path, draft2_actors: Path) -> GovernedObjectRuntime:
    types = TypeRegistry()
    types.load_directory(draft2_types)
    return GovernedObjectRuntime(
        SQLiteStore(tmp_path / "draft2.db"),
        type_registry=types,
        **generic_dependencies(),
        actors=ActorRegistry(draft2_actors),
        adapters=[Draft2WorkItemAdapter()],
    )


@pytest.fixture
def writer(runtime: GovernedObjectRuntime):
    return runtime._actors.resolve("writer")


@pytest.fixture
def reader(runtime: GovernedObjectRuntime):
    return runtime._actors.resolve("reader")


def _create(runtime, writer, object_id: str = "wi-1") -> None:
    result = runtime.execute(
        CreateObject(object_id, "Draft2WorkItem", {"title": "Initial"}),
        actor=writer,
    )
    assert result.decision == Decision.ALLOW


class TestTypeVersion:
    def test_explicit_type_version_on_create(self, runtime, writer):
        _create(runtime, writer)
        obj = runtime.get_object("wi-1", raw=True)
        assert obj.type_version == "1"

    def test_mutation_preserves_type_version(self, runtime, writer):
        _create(runtime, writer)
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        runtime.execute(
            TransitionState("wi-1", "update_title"),
            actor=writer,
            command_context=CommandContext(),
        )
        obj = runtime.get_object("wi-1", raw=True)
        assert obj.type_version == "1"
        assert obj.version > 1

    def test_replay_preserves_type_version(self, runtime, writer):
        _create(runtime, writer)
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        replayed = runtime.replay("wi-1")
        assert replayed is not None
        assert replayed.type_version == "1"


class TestRelationshipAdd:
    def test_accepted_add_advances_version_and_history(self, runtime, writer):
        _create(runtime, writer)
        before = runtime.get_object("wi-1", raw=True)
        result = runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        assert result.decision == Decision.ALLOW
        assert result.outcome_category == OutcomeCategory.ACCEPTED.value
        after = runtime.get_object("wi-1", raw=True)
        assert after.version == before.version + 1
        rels = list_canonical_for_object(runtime._store, "wi-1")
        assert len(rels) == 1
        assert rels[0].relation == "owned_by"
        history = [e for e in runtime.history("wi-1") if e.decision == Decision.ALLOW]
        assert any(e.action == "add_relationship" for e in history)


class TestRelationshipRemove:
    def test_accepted_remove(self, runtime, writer):
        runtime.execute(
            CreateObject(
                "wi-2",
                "Draft2WorkItem",
                {"title": "R"},
                relationships=[{"relation": "depends_on", "target_id": "other"}],
            ),
            actor=writer,
        )
        before = runtime.get_object("wi-2", raw=True)
        result = runtime.remove_relationship("wi-2", "depends_on", "other", actor=writer)
        assert result.decision == Decision.ALLOW
        after = runtime.get_object("wi-2", raw=True)
        assert after.version == before.version + 1
        assert not list_canonical_for_object(runtime._store, "wi-2")
        history = [e for e in runtime.history("wi-2") if e.decision == Decision.ALLOW]
        assert any(e.action == "remove_relationship" for e in history)


class TestRelationshipConcurrency:
    def test_stale_add_conflicts(self, runtime, writer):
        _create(runtime, writer)
        v1 = runtime.get_object("wi-1", raw=True).version
        runtime.execute(
            TransitionState("wi-1", "update_title"),
            actor=writer,
            command_context=CommandContext(),
        )
        result = runtime.add_relationship(
            "wi-1",
            "owned_by",
            "party-1",
            actor=writer,
            command_context=CommandContext(expected_version=v1),
        )
        assert result.decision == Decision.CONFLICT
        assert result.outcome_category == OutcomeCategory.CONFLICT.value

    def test_stale_remove_conflicts(self, runtime, writer):
        runtime.execute(
            CreateObject(
                "wi-3",
                "Draft2WorkItem",
                {"title": "R"},
                relationships=[{"relation": "depends_on", "target_id": "other"}],
            ),
            actor=writer,
        )
        v1 = runtime.get_object("wi-3", raw=True).version
        runtime.add_relationship("wi-3", "owned_by", "party-1", actor=writer)
        result = runtime.remove_relationship(
            "wi-3",
            "depends_on",
            "other",
            actor=writer,
            command_context=CommandContext(expected_version=v1),
        )
        assert result.decision == Decision.CONFLICT

    def test_relationship_mutation_makes_stale_state_update_conflict(self, runtime, writer):
        _create(runtime, writer)
        v1 = runtime.get_object("wi-1", raw=True).version
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        result = runtime.execute(
            TransitionState("wi-1", "update_title"),
            actor=writer,
            command_context=CommandContext(expected_version=v1),
        )
        assert result.decision == Decision.CONFLICT

    def test_state_mutation_makes_stale_relationship_conflict(self, runtime, writer):
        _create(runtime, writer)
        v1 = runtime.get_object("wi-1", raw=True).version
        runtime.execute(
            TransitionState("wi-1", "update_title"),
            actor=writer,
            command_context=CommandContext(),
        )
        result = runtime.add_relationship(
            "wi-1",
            "owned_by",
            "party-1",
            actor=writer,
            command_context=CommandContext(expected_version=v1),
        )
        assert result.decision == Decision.CONFLICT


class TestRelationshipReplay:
    def test_replay_reconstructs_final_relationship_set(self, runtime, writer):
        runtime.execute(
            CreateObject("wi-r", "Draft2WorkItem", {"title": "Replay"}),
            actor=writer,
        )
        runtime.add_relationship("wi-r", "owned_by", "party-a", actor=writer)
        runtime.add_relationship("wi-r", "depends_on", "party-b", actor=writer)
        runtime.remove_relationship("wi-r", "owned_by", "party-a", actor=writer)
        runtime.execute(
            TransitionState("wi-r", "update_title"),
            actor=writer,
            command_context=CommandContext(),
        )

        runtime._store.remove("wi-r", "depends_on", "party-b")
        replayed = runtime.replay("wi-r")
        assert replayed is not None
        rels = list_canonical_for_object(runtime._store, "wi-r")
        assert len(rels) == 1
        assert rels[0].relation == "depends_on"
        assert rels[0].target_id == "party-b"
        assert replayed.type_version == "1"


class TestOutcomeCategories:
    def test_auth_failure(self, runtime, writer, reader):
        _create(runtime, writer)
        result = runtime.add_relationship("wi-1", "owned_by", "party-1", actor=reader)
        assert result.outcome_category == OutcomeCategory.AUTHORIZATION_DENIED.value

    def test_schema_invalid_on_create(self, runtime, writer):
        result = runtime.execute(
            CreateObject("bad", "Draft2WorkItem", {}),
            actor=writer,
        )
        assert result.outcome_category == OutcomeCategory.SEMANTICALLY_INVALID.value
        assert result.deny_reason == DenyReason.SCHEMA_INVALID.value

    def test_conflict(self, runtime, writer):
        _create(runtime, writer)
        v1 = runtime.get_object("wi-1", raw=True).version
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        result = runtime.add_relationship(
            "wi-1",
            "owned_by",
            "party-2",
            actor=writer,
            command_context=CommandContext(expected_version=v1),
        )
        assert result.outcome_category == OutcomeCategory.CONFLICT.value

    def test_accepted(self, runtime, writer):
        _create(runtime, writer)
        result = runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        assert result.outcome_category == OutcomeCategory.ACCEPTED.value
