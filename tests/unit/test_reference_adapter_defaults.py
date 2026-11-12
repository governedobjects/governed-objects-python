# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Legacy selection diagnostics and real supplied-adapter consumers.

Native imports are warmed by compatibility cases: only the separate installed
driver makes fresh-process concrete-module absence assertions.
"""
from __future__ import annotations

import pytest

import governed_objects.runtime.runtime as runtime_module
from governed_objects.adapters.base import TypeAdapter
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import CreateObject, ReadObject, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType, Decision, GovernedObject
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator

KIND = "adapter.test/Record"


class ProbeAdapter(TypeAdapter):
    def __init__(self):
        self.calls = []

    def supports(self, object_type):
        return object_type == KIND

    def validate_create(self, type_def, data):
        self.calls.append(("validate_create", dict(data)))
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(self, obj, type_def, action, actor):
        self.calls.append(("validate_transition", action))
        valid, reason, check = LifecycleValidator().validate_transition(type_def, obj.state, action)
        return valid, [check], reason

    def load(self, obj, context):
        self.calls.append(("load", obj.id))
        return None


class AdapterList(list):
    pass


class FalseyAdapterList(list):
    def __bool__(self):
        return False


@pytest.fixture
def store(tmp_path):
    database = SQLiteStore(tmp_path / "runtime.db")
    yield database
    database.close()


def configured_runtime(store, **options):
    types = TypeRegistry()
    types.register(ObjectTypeDefinition(
        kind=KIND, type_version="test-v1",
        schema={"value": {"type": "string", "required": True}},
        initial_state="new",
        states={"new": {"transitions": {"finish": "done"}}, "done": {"terminal": True}},
        actions={"create": {"requires": ["adapter.create"]}, "finish": {"requires": ["adapter.finish"]}},
        read_requires=["adapter.read"],
    ))
    return GovernedObjectRuntime(
        store, type_registry=types, providers=ProviderRegistry(), application_registry=ApplicationRegistry(),
        extension_registry=TypeExtensionRegistry(), **options,
    )


@pytest.mark.parametrize("selection", ["omitted", "none"])
def test_implicit_adapters_are_rejected(store, monkeypatch, selection):
    assert not hasattr(runtime_module, "_legacy_reference_adapters")
    monkeypatch.setattr(runtime_module, "SystemClock", lambda: pytest.fail("helper constructed"))
    options = {} if selection == "omitted" else {"adapters": None}
    with pytest.raises(TypeError if selection == "omitted" else ValueError):
        configured_runtime(store, **options)


@pytest.mark.parametrize("list_class", [list, AdapterList, FalseyAdapterList])
def test_truthy_list_retained_without_factory_or_extra_defaults(store, monkeypatch, list_class):
    def forbidden(providers):
        raise AssertionError("Explicit supplied adapters must bypass legacy defaults")

    assert not hasattr(runtime_module, "_legacy_reference_adapters")
    adapter = ProbeAdapter()
    supplied = list_class([adapter])
    runtime = configured_runtime(store, adapters=supplied)
    assert runtime._adapters_list is supplied and supplied == [adapter]
    assert runtime._adapters._adapters is supplied
    assert runtime._materializer._adapters is runtime._adapters
    assert runtime._traversal._adapters is supplied
    assert runtime._adapters.for_type(KIND) is adapter
    assert runtime._adapters.for_type("adapter.test/Unsupported") is None
    unknown = GovernedObject(id="unsupported", type="adapter.test/Unsupported", state="new")
    with pytest.raises(KeyError, match="no adapter for type"):
        runtime._adapters.resolve(unknown)
    assert adapter.calls == []


def test_empty_list_retained_without_factory(store, monkeypatch):
    def forbidden(providers):
        raise AssertionError("Explicit empty adapters must bypass legacy defaults")

    assert not hasattr(runtime_module, "_legacy_reference_adapters")
    supplied = []
    runtime = configured_runtime(store, adapters=supplied)
    assert runtime._adapters_list is supplied and supplied == []
    assert runtime._adapters._adapters is supplied
    assert runtime._materializer._adapters is runtime._adapters
    assert runtime._traversal._adapters is supplied
    assert runtime._adapters.for_type(KIND) is None


def test_supplied_adapter_real_governed_and_materialization_consumers(store):
    # No factory spy, policy/command substitution or raw canonical writes.
    adapter = ProbeAdapter()
    supplied = [adapter]
    runtime = configured_runtime(store, adapters=supplied)
    writer = Actor(id="adapter-writer", type=ActorType.SERVICE,
                   capabilities=["adapter.create", "adapter.read", "adapter.finish"])
    reader = Actor(id="adapter-reader", type=ActorType.SERVICE, capabilities=["adapter.read"])

    invalid = runtime.execute(CreateObject("invalid", KIND, {}), actor=writer)
    assert invalid.outcome_category == "SEMANTICALLY_INVALID"
    assert runtime.get_object("invalid", raw=True) is None
    assert runtime.accepted_history("invalid") == []
    created = runtime.execute(CreateObject("probe", KIND, {"value": "synthetic"}), actor=writer)
    assert created.decision == Decision.ALLOW and created.object_version == 1
    assert ("validate_create", {}) in adapter.calls
    assert ("validate_create", {"value": "synthetic"}) in adapter.calls

    def snapshot():
        return (runtime.get_object("probe", raw=True).model_dump(mode="json"),
                [e.model_dump(mode="json") for e in runtime.accepted_history("probe")],
                store.version_high_water("probe"))

    initial = snapshot()
    read = runtime.execute(ReadObject("probe"), actor=reader)
    assert read.decision == Decision.ALLOW and read.data == {"value": "synthetic"}
    assert ("load", "probe") in adapter.calls and snapshot() == initial
    before_traversal = len(adapter.calls)
    traversed = runtime.traverse(reader, "probe", max_depth=0)
    assert [row.object.id for row in traversed] == ["probe"]
    assert len(adapter.calls) > before_traversal and snapshot() == initial
    denied = runtime.execute(TransitionState("probe", "finish"), actor=reader,
                             command_context=CommandContext(expected_version=1))
    assert denied.outcome_category == "AUTHORIZATION_DENIED" and snapshot() == initial
    finished = runtime.execute(TransitionState("probe", "finish"), actor=writer,
                               command_context=CommandContext(expected_version=1))
    assert finished.decision == Decision.ALLOW and finished.object_version == 2
    assert ("validate_transition", "finish") in adapter.calls
    final = snapshot()
    assert final[0]["state"] == "done" and len(final[1]) == 2
    stale = runtime.execute(TransitionState("probe", "finish"), actor=writer,
                            command_context=CommandContext(expected_version=1))
    assert stale.outcome_category == "CONFLICT" and snapshot() == final
    invalid = runtime.execute(TransitionState("probe", "finish"), actor=writer,
                              command_context=CommandContext(expected_version=2))
    assert invalid.outcome_category == "SEMANTICALLY_INVALID" and snapshot() == final
    assert runtime._adapters_list is supplied and supplied == [adapter]
    assert all(not hasattr(runtime, name) for name in ("analytics", "provenance", "_analytics", "_provenance"))
