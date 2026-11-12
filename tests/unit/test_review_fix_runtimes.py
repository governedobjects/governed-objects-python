# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Regression tests for the independent review fixes IR-006 (deny by default),
IR-007 (atomic commands), and deny on empty for portable types."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from governed_objects.adapters.base import TypeAdapter
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import AddRelationship, CreateObject
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType, Decision
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ActionDef, FieldSchema, ObjectTypeDefinition, StateDef
from governed_objects.types.validation import SchemaValidator
from governed_objects.policy.evaluator import CapabilityPolicyEvaluator

KIND = "review-fix.test/Record"
NO_CAPS_ACTOR = Actor(id="no-caps", type=ActorType.SERVICE)


class ProbeAdapter(TypeAdapter):
    def supports(self, object_type):
        return object_type == KIND

    def validate_create(self, type_def, data):
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(self, obj, type_def, action, actor):
        return True, [], ""


def _runtime(path, type_def):
    store = SQLiteStore(path)
    types = TypeRegistry()
    types.register(type_def)
    runtime = GovernedObjectRuntime(
        store,
        type_registry=types,
        adapters=[ProbeAdapter()],
        extension_registry=TypeExtensionRegistry(),
        providers=ProviderRegistry(),
        application_registry=ApplicationRegistry(),
    )
    return runtime, store


# --- IR-006: deny by default ------------------------------------------------


def test_undeclared_action_requirement_denies_every_actor():
    """A type that omits ``requires`` for an action denies, it does not open."""
    evaluator = CapabilityPolicyEvaluator()
    type_def = ObjectTypeDefinition(kind=KIND, actions={"create": ActionDef()})
    decision = evaluator.evaluate(NO_CAPS_ACTOR, "create", None, type_def)
    assert decision.decision == Decision.DENY


def test_undeclared_read_requirement_denies_every_actor():
    """A type that omits ``read_requires`` denies reads, it does not open."""
    evaluator = CapabilityPolicyEvaluator()
    type_def = ObjectTypeDefinition(kind=KIND)
    decision = evaluator.evaluate(NO_CAPS_ACTOR, "read", None, type_def)
    assert decision.decision == Decision.DENY


def test_explicit_open_marker_allows_every_actor():
    """``open: true`` (actions) and ``read_open: true`` (reads) are the only
    way to open an action or read to every actor."""
    evaluator = CapabilityPolicyEvaluator()
    type_def = ObjectTypeDefinition(
        kind=KIND, actions={"create": ActionDef(open=True)}, read_open=True,
    )
    assert evaluator.evaluate(NO_CAPS_ACTOR, "create", None, type_def).decision == Decision.ALLOW
    assert evaluator.evaluate(NO_CAPS_ACTOR, "read", None, type_def).decision == Decision.ALLOW


def test_create_through_runtime_denies_without_declared_or_open_capability(tmp_path):
    type_def = ObjectTypeDefinition(
        kind=KIND, initial_state="open", states={"open": StateDef()},
        fields={"value": FieldSchema(type="string")},
        actions={"create": ActionDef()},
    )
    runtime, store = _runtime(tmp_path / "ir006.db", type_def)
    try:
        result = runtime.execute(CreateObject("a", KIND, {"value": "x"}), actor=NO_CAPS_ACTOR)
        assert result.decision == Decision.DENY
    finally:
        store.close()


# --- Deny on empty for portable types ----------------------------------------


def test_portable_type_empty_requirement_denies():
    """``build_type_definition`` with an empty requirement list for a key
    denies that action/read, the same as the native runtime default."""
    from governed_objects.interchange.types import REQUIREMENT_KEYS, build_type_definition

    portable = dict(
        type_identity=KIND, type_version="opaque:1",
        state_fields={"value": "string"}, relationship_types=[], actions={},
    )
    type_def = build_type_definition(portable, {k: [] for k in REQUIREMENT_KEYS})
    evaluator = CapabilityPolicyEvaluator()
    assert evaluator.evaluate(NO_CAPS_ACTOR, "create", None, type_def).decision == Decision.DENY
    assert evaluator.evaluate(NO_CAPS_ACTOR, "read", None, type_def).decision == Decision.DENY
    assert evaluator.evaluate(NO_CAPS_ACTOR, "add_relationship", None, type_def).decision == Decision.DENY


def test_portable_type_open_keys_allows():
    """Naming a key in ``open_keys`` explicitly opens it to every actor, even
    though its requirement list is empty."""
    from governed_objects.interchange.types import REQUIREMENT_KEYS, build_type_definition

    portable = dict(
        type_identity=KIND, type_version="opaque:1",
        state_fields={"value": "string"}, relationship_types=[], actions={},
    )
    type_def = build_type_definition(
        portable, {k: [] for k in REQUIREMENT_KEYS}, open_keys=REQUIREMENT_KEYS,
    )
    evaluator = CapabilityPolicyEvaluator()
    assert evaluator.evaluate(NO_CAPS_ACTOR, "create", None, type_def).decision == Decision.ALLOW
    assert evaluator.evaluate(NO_CAPS_ACTOR, "read", None, type_def).decision == Decision.ALLOW
    assert evaluator.evaluate(NO_CAPS_ACTOR, "add_relationship", None, type_def).decision == Decision.ALLOW


# --- IR-007: atomic commands -------------------------------------------------


def _record_type():
    return ObjectTypeDefinition(
        kind=KIND, initial_state="open", states={"open": StateDef()},
        fields={"value": FieldSchema(type="string")},
        actions={
            "create": ActionDef(open=True),
            "add_relationship": ActionDef(open=True),
            "remove_relationship": ActionDef(open=True),
        },
        read_open=True,
    )


def test_concurrent_add_relationship_threads_never_both_commit_same_version(tmp_path):
    """Two threads racing the same expected_version on one connection: exactly
    one is accepted, the other is refused as a conflict, and the object never
    ends up with a duplicated or skipped Object Version."""
    path = tmp_path / "race.db"
    runtime, store = _runtime(path, _record_type())
    try:
        created = runtime.execute(CreateObject("a", KIND, {"value": "1"}), actor=NO_CAPS_ACTOR)
        assert created.decision == Decision.ALLOW

        results = []
        barrier = threading.Barrier(2)

        def worker(target):
            barrier.wait()
            results.append(
                runtime.execute(
                    AddRelationship("a", "links", target),
                    actor=NO_CAPS_ACTOR,
                    command_context=CommandContext(expected_version=1),
                )
            )

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("x", "y")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        decisions = [r.decision for r in results]
        assert decisions.count(Decision.ALLOW) == 1
        assert decisions.count(Decision.CONFLICT) == 1

        final = store.get("a")
        assert final.version == 2
        history = runtime.accepted_history("a")
        assert [e.object_version for e in history] == [1, 2]
    finally:
        store.close()


def test_two_connections_to_one_file_do_not_duplicate_object_version(tmp_path):
    """Two independent SQLiteStore connections (two processes would behave the
    same way) to the same database file racing a create of the same identity:
    only one wins; the loser is denied, never silently overwritten."""
    path = tmp_path / "two_conn.db"
    store_a = SQLiteStore(path)
    store_b = SQLiteStore(path)
    types = TypeRegistry()
    types.register(_record_type())

    def make_runtime(store):
        return GovernedObjectRuntime(
            store, type_registry=types, adapters=[ProbeAdapter()],
            extension_registry=TypeExtensionRegistry(), providers=ProviderRegistry(),
            application_registry=ApplicationRegistry(),
        )

    runtime_a, runtime_b = make_runtime(store_a), make_runtime(store_b)
    results = []
    barrier = threading.Barrier(2)

    def worker(runtime, value):
        barrier.wait()
        results.append(runtime.execute(CreateObject("shared", KIND, {"value": value}), actor=NO_CAPS_ACTOR))

    threads = [
        threading.Thread(target=worker, args=(runtime_a, "from-a")),
        threading.Thread(target=worker, args=(runtime_b, "from-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        decisions = [r.decision for r in results]
        assert decisions.count(Decision.ALLOW) == 1
        assert decisions.count(Decision.DENY) == 1
        final = store_a.get("shared")
        assert final.version == 1
        assert len(runtime_a.accepted_history("shared")) == 1
    finally:
        store_a.close()
        store_b.close()


def test_fault_between_object_write_and_history_append_leaves_no_partial_state(tmp_path):
    """A fault injected between the object write and the history append rolls
    the whole transaction back: no state without history, no orphaned write."""
    path = tmp_path / "fault.db"
    runtime, store = _runtime(path, _record_type())
    try:
        created = runtime.execute(CreateObject("a", KIND, {"value": "1"}), actor=NO_CAPS_ACTOR)
        assert created.decision == Decision.ALLOW
        before = store.get("a")
        before_history = runtime.accepted_history("a")

        real_append = store.append_accepted

        def faulty_append(event):
            raise RuntimeError("injected fault between write and history append")

        store.append_accepted = faulty_append
        try:
            with pytest.raises(RuntimeError):
                runtime.execute(
                    AddRelationship("a", "links", "x"),
                    actor=NO_CAPS_ACTOR,
                    command_context=CommandContext(expected_version=1),
                )
        finally:
            store.append_accepted = real_append

        after = store.get("a")
        assert after.version == before.version
        assert runtime.accepted_history("a") == before_history
        assert store._conn.in_transaction is False
    finally:
        store.close()
