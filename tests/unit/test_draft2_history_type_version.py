# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Draft2 remediation — accepted Canonical History preserves the bound Type Version."""

from __future__ import annotations
from tests.generic_bootstrap import generic_dependencies, synthetic_extensions

import sqlite3
from pathlib import Path

import pytest
import yaml

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import AdapterContext, AdapterExecuteResult
from governed_objects.domain.actors import ActorRegistry
from governed_objects.domain.command import Command, CreateObject, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import (
    Actor,
    Decision,
    GovernanceEvent,
    GovernedObject,
    ValidationCheck,
)
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.policy.outcomes import OutcomeCategory
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator

BOUND_VERSION = "2026.09-opaque+b"


class HistoryWorkItemAdapter(TypeAdapter):
    def supports(self, object_type: str) -> bool:
        return object_type in {"HistoryWorkItem", "LegalContract"}

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
        new_data = dict(obj.data)
        new_data["title"] = (command.payload or {}).get("title", obj.data.get("title"))
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
def types_dir(tmp_path: Path) -> Path:
    d = tmp_path / "types"
    d.mkdir()
    (d / "work_item.yaml").write_text(
        yaml.dump(
            {
                "kind": "HistoryWorkItem",
                "version": BOUND_VERSION,
                "schema": {"title": {"type": "string", "required": True}},
                "read_requires": ["hist.read"],
                "actions": {
                    "create": {"requires": ["hist.mutate"]},
                    "update_title": {"requires": ["hist.mutate"]},
                    "add_relationship": {"requires": ["hist.mutate"]},
                    "remove_relationship": {"requires": ["hist.mutate"]},
                },
                "states": {"draft": {"transitions": {"update_title": "draft"}}},
                "initial_state": "draft",
            }
        )
    )
    (d / "legal_contract.yaml").write_text(
        yaml.dump(
            {
                "kind": "LegalContract",
                "version": BOUND_VERSION,
                "schema": {"title": {"type": "string", "required": True}},
                "read_requires": ["hist.read"],
                "actions": {
                    "create": {"requires": ["hist.mutate"]},
                    "activate": {"requires": ["hist.mutate"]},
                },
                "states": {
                    "draft": {"transitions": {"activate": "active"}},
                    "active": {"terminal": True},
                },
                "initial_state": "draft",
            }
        )
    )
    return d


@pytest.fixture
def actors_dir(tmp_path: Path) -> Path:
    d = tmp_path / "actors"
    d.mkdir()
    for actor_id, caps in {
        "writer": ["hist.read", "hist.mutate"],
        "reader": ["hist.read"],
    }.items():
        (d / f"{actor_id}.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "governedcontainer/v1alpha1",
                    "kind": "Actor",
                    "metadata": {"id": actor_id},
                    "spec": {"type": "service", "capabilities": caps},
                }
            )
        )
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "history.db"


@pytest.fixture
def runtime(db_path: Path, types_dir: Path, actors_dir: Path) -> GovernedObjectRuntime:
    types = TypeRegistry()
    types.load_directory(types_dir)
    return GovernedObjectRuntime(
        SQLiteStore(db_path),
        type_registry=types,
        **generic_dependencies(extensions=synthetic_extensions("LegalContract", "HistoryWorkItem")),
        actors=ActorRegistry(actors_dir),
        adapters=[HistoryWorkItemAdapter()],
    )


@pytest.fixture
def writer(runtime: GovernedObjectRuntime) -> Actor:
    return runtime._actors.resolve("writer")


@pytest.fixture
def reader(runtime: GovernedObjectRuntime) -> Actor:
    return runtime._actors.resolve("reader")


def _create(runtime, writer, object_id: str = "wi-1", relationships=None) -> None:
    result = runtime.execute(
        CreateObject(
            object_id,
            "HistoryWorkItem",
            {"title": "Initial"},
            relationships=relationships or [],
        ),
        actor=writer,
    )
    assert result.decision == Decision.ALLOW


def _update_title(runtime, writer, object_id: str = "wi-1", **ctx):
    return runtime.execute(
        TransitionState(object_id, "update_title"),
        actor=writer,
        command_context=CommandContext(**ctx),
    )


def _accepted(runtime, object_id: str) -> list[GovernanceEvent]:
    return [
        e
        for e in runtime.history(object_id)
        if e.decision == Decision.ALLOW and e.action != "read"
    ]


class TestAcceptedHistoryTypeVersion:
    def test_create_history_carries_bound_type_version(self, runtime, writer):
        _create(runtime, writer)
        obj = runtime.get_object("wi-1", raw=True)
        assert obj.type_version == BOUND_VERSION
        [create] = _accepted(runtime, "wi-1")
        assert create.action == "create"
        assert create.object_type == obj.type
        assert create.type_version == BOUND_VERSION

    def test_state_mutation_history_carries_bound_type_version(self, runtime, writer):
        _create(runtime, writer)
        result = _update_title(runtime, writer)
        assert result.decision == Decision.ALLOW
        mutation = [e for e in _accepted(runtime, "wi-1") if e.action == "update_title"]
        assert len(mutation) == 1
        assert mutation[0].type_version == BOUND_VERSION

    def test_relationship_add_history_carries_bound_type_version(self, runtime, writer):
        _create(runtime, writer)
        result = runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        assert result.decision == Decision.ALLOW
        [add] = [e for e in _accepted(runtime, "wi-1") if e.action == "add_relationship"]
        assert add.type_version == BOUND_VERSION
        assert add.details["relation"] == "owned_by"

    def test_relationship_remove_history_carries_bound_type_version(self, runtime, writer):
        _create(
            runtime,
            writer,
            relationships=[{"relation": "depends_on", "target_id": "other"}],
        )
        result = runtime.remove_relationship("wi-1", "depends_on", "other", actor=writer)
        assert result.decision == Decision.ALLOW
        [remove] = [
            e for e in _accepted(runtime, "wi-1") if e.action == "remove_relationship"
        ]
        assert remove.type_version == BOUND_VERSION

    def test_command_result_events_match_persisted_history(self, runtime, writer):
        _create(runtime, writer)
        result = runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        [returned] = result.events
        [persisted] = [e for e in runtime.history("wi-1") if e.event_id == returned.event_id]
        assert returned.type_version == persisted.type_version == BOUND_VERSION


class TestTypeVersionStability:
    def test_multiple_mutations_retain_same_type_version(self, runtime, writer):
        _create(runtime, writer)
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        _update_title(runtime, writer)
        runtime.add_relationship("wi-1", "depends_on", "party-2", actor=writer)
        runtime.remove_relationship("wi-1", "owned_by", "party-1", actor=writer)
        _update_title(runtime, writer)

        history = _accepted(runtime, "wi-1")
        assert [e.action for e in history] == [
            "create",
            "add_relationship",
            "update_title",
            "add_relationship",
            "remove_relationship",
            "update_title",
        ]
        assert {e.type_version for e in history} == {BOUND_VERSION}
        assert [e.object_version for e in history] == [1, 2, 3, 4, 5, 6]

        obj = runtime.get_object("wi-1", raw=True)
        assert obj.type_version == BOUND_VERSION
        assert obj.version == 6

    def test_history_type_version_survives_store_reopen(
        self, runtime, writer, db_path, types_dir, actors_dir
    ):
        _create(runtime, writer)
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        runtime._store.close()

        types = TypeRegistry()
        types.load_directory(types_dir)
        reopened = GovernedObjectRuntime(
            SQLiteStore(db_path),
            type_registry=types,
            **generic_dependencies(extensions=synthetic_extensions("LegalContract", "HistoryWorkItem")),
            actors=ActorRegistry(actors_dir),
            adapters=[HistoryWorkItemAdapter()],
        )
        history = _accepted(reopened, "wi-1")
        assert len(history) == 2
        assert {e.type_version for e in history} == {BOUND_VERSION}


class TestReplayTypeVersion:
    def test_replay_preserves_bound_type_version(self, runtime, writer):
        _create(runtime, writer)
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        runtime.remove_relationship("wi-1", "owned_by", "party-1", actor=writer)
        _update_title(runtime, writer)

        replayed = runtime.replay("wi-1")
        assert replayed is not None
        assert replayed.type_version == BOUND_VERSION
        assert runtime.get_object("wi-1", raw=True).type_version == BOUND_VERSION
        assert {e.type_version for e in _accepted(runtime, "wi-1")} == {BOUND_VERSION}

    def test_reducer_replay_does_not_rebind_type_version(self, runtime, writer):
        result = runtime.execute(
            CreateObject("contract-1", "LegalContract", {"title": "MSA"}),
            actor=writer,
        )
        assert result.decision == Decision.ALLOW
        activated = runtime.execute(TransitionState("contract-1", "activate"), actor=writer)
        assert activated.decision == Decision.ALLOW

        first = runtime.replay("contract-1")
        assert first is not None
        assert first.type_version == BOUND_VERSION
        assert first.state == "active"

        second = runtime.replay("contract-1")
        assert second is not None
        assert second.type_version == BOUND_VERSION
        assert runtime.get_object("contract-1", raw=True).type_version == BOUND_VERSION


class TestFailedOperationsCreateNoAcceptedHistory:
    def test_authorization_denied(self, runtime, writer, reader):
        _create(runtime, writer)
        before = _accepted(runtime, "wi-1")
        result = runtime.add_relationship("wi-1", "owned_by", "party-1", actor=reader)
        assert result.outcome_category == OutcomeCategory.AUTHORIZATION_DENIED.value
        assert _accepted(runtime, "wi-1") == before
        obj = runtime.get_object("wi-1", raw=True)
        assert obj.version == 1
        assert obj.type_version == BOUND_VERSION

    def test_semantically_invalid(self, runtime, writer):
        result = runtime.execute(CreateObject("bad", "HistoryWorkItem", {}), actor=writer)
        assert result.outcome_category == OutcomeCategory.SEMANTICALLY_INVALID.value
        assert _accepted(runtime, "bad") == []
        assert runtime.get_object("bad", raw=True) is None

    def test_conflict(self, runtime, writer):
        _create(runtime, writer)
        runtime.add_relationship("wi-1", "owned_by", "party-1", actor=writer)
        before = _accepted(runtime, "wi-1")
        result = _update_title(runtime, writer, expected_version=1)
        assert result.outcome_category == OutcomeCategory.CONFLICT.value
        assert _accepted(runtime, "wi-1") == before
        obj = runtime.get_object("wi-1", raw=True)
        assert obj.version == 2
        assert obj.type_version == BOUND_VERSION


class TestEventStoreSchema:
    def test_legacy_event_table_is_extended(self, tmp_path: Path):
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                object_id TEXT NOT NULL, object_type TEXT NOT NULL,
                actor_id TEXT NOT NULL, actor_type TEXT NOT NULL,
                action TEXT NOT NULL, decision TEXT NOT NULL,
                event_type TEXT NOT NULL, previous_state TEXT, new_state TEXT,
                reason TEXT, object_version INTEGER, details_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO events VALUES "
            "('e0', '2026-01-01T00:00:00+00:00', 'o', 'T', 'a', 'service', "
            "'create', 'ALLOW', 'object.create', NULL, 'draft', '', 1, '{}')"
        )
        conn.commit()
        conn.close()

        store = SQLiteStore(db)
        [legacy] = store.list_for_object("o")
        assert legacy.type_version is None

        store.append(
            GovernanceEvent(
                object_id="o",
                object_type="T",
                type_version=BOUND_VERSION,
                actor_id="a",
                actor_type="service",
                action="update",
                decision=Decision.ALLOW,
                event_type="object.update",
            )
        )
        assert [e.type_version for e in store.list_for_object("o")] == [None, BOUND_VERSION]
