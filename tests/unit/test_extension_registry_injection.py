# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Registry selection and real normalization, not a reconstruction claim.

Factory spies are used only for constructor diagnostics. The governed consumer
test below uses the real policy, schema, adapter, runtime and SQLite path.
"""
from __future__ import annotations

import inspect

import pytest

from governed_objects.adapters.base import TypeAdapter
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import CreateObject
from governed_objects.domain.models import Actor, ActorType, Decision
from governed_objects.events.replay import EventReplayer
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator

KIND = "extension.test/Probe"


class FalseyRegistry(TypeExtensionRegistry):
    def __bool__(self) -> bool:
        return False


class ProbeCanonicalizer:
    def __init__(self):
        self.calls = []

    def canonicalize_create(self, data):
        self.calls.append(dict(data))
        normalized = dict(data)
        if isinstance(normalized.get("value"), str):
            normalized["value"] = normalized["value"].strip()
        return normalized

    def canonicalize_update(self, data):
        return dict(data)


class UnusedReducer:
    def reduce(self, obj, event):
        raise AssertionError("These tests make no replay/reconstruction claim")


class ProbeAdapter(TypeAdapter):
    def supports(self, object_type):
        return object_type == KIND

    def validate_create(self, type_def, data):
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(self, obj, type_def, action, actor):
        valid, reason, check = LifecycleValidator().validate_transition(type_def, obj.state, action)
        return valid, [check], reason


@pytest.fixture
def store(tmp_path):
    database = SQLiteStore(tmp_path / "runtime.db")
    yield database
    database.close()


def make_runtime(store, **options):
    types = TypeRegistry()
    types.register(ObjectTypeDefinition(
        kind=KIND,
        type_version="test-v1",
        schema={"value": {"type": "string", "required": True}},
        initial_state="new",
        states={"new": {"terminal": True}},
        actions={"create": {"requires": ["extension.create"]}},
        read_requires=["extension.read"],
    ))
    return GovernedObjectRuntime(
        store, type_registry=types, adapters=[ProbeAdapter()], providers=ProviderRegistry(),
        application_registry=ApplicationRegistry(), **options,
    )


def populated_registry(registry_class, populated):
    registry = registry_class()
    if populated:
        registry.register_reducer(KIND, UnusedReducer())
        registry.register_canonicalizer(KIND, ProbeCanonicalizer())
    return registry


def reject_legacy_factory(monkeypatch):
    def forbidden(cls):
        raise AssertionError("Explicit registry must not call the legacy factory")
    assert not hasattr(TypeExtensionRegistry, "with_builtins")


def test_explicit_signature_retires_positional_dependencies_and_fake_paths():
    signature = inspect.signature(GovernedObjectRuntime)
    assert signature.parameters["store"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind == inspect.Parameter.KEYWORD_ONLY for name, p in signature.parameters.items() if name != "store")
    assert "fake_provider_path" not in signature.parameters and "fake_contract_path" not in signature.parameters
    assert signature.parameters["extension_registry"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        signature.bind(object(), object())


@pytest.mark.parametrize("explicit_none", [False, True], ids=["omitted", "none"])
@pytest.mark.parametrize("consumer", ["runtime", "replayer"])
def test_implicit_extensions_are_rejected(store, monkeypatch, explicit_none, consumer):
    reject_legacy_factory(monkeypatch)
    with pytest.raises(ValueError if explicit_none else TypeError):
        if consumer == "runtime":
            make_runtime(store, **({"extension_registry": None} if explicit_none else {}))
        else:
            EventReplayer(store, **({"extensions": None} if explicit_none else {}))


@pytest.mark.parametrize("registry_class", [TypeExtensionRegistry, FalseyRegistry])
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "nonempty"])
@pytest.mark.parametrize("consumer", ["runtime", "replayer"])
def test_explicit_registry_identity_without_factory(store, monkeypatch, registry_class, populated, consumer):
    registry = populated_registry(registry_class, populated)
    reducers_before = dict(registry._reducers)
    canonicalizers_before = dict(registry._canonicalizers)
    reject_legacy_factory(monkeypatch)
    if consumer == "runtime":
        runtime = make_runtime(store, extension_registry=registry)
        assert runtime._extensions is registry
        assert runtime._replayer._extensions is registry
    else:
        replayer = EventReplayer(store, registry)
        assert replayer._extensions is registry
    assert registry._reducers == reducers_before
    assert registry._canonicalizers == canonicalizers_before
    if populated:
        assert registry.reducer_for(KIND) is reducers_before[KIND]
        assert registry.canonicalizer_for(KIND) is canonicalizers_before[KIND]


@pytest.mark.parametrize("registry_class", [TypeExtensionRegistry, FalseyRegistry])
def test_selected_canonicalizer_consumed_by_governed_create(store, registry_class):
    # No factory spy or command/policy substitution in this consumer test.
    extensions = populated_registry(registry_class, True)
    canonicalizer = extensions.canonicalizer_for(KIND)
    runtime = make_runtime(store, extension_registry=extensions)
    actor = Actor(id="extension-writer", type=ActorType.SERVICE,
                  capabilities=["extension.create", "extension.read"])
    result = runtime.execute(CreateObject("probe", KIND, {"value": "  padded  "}), actor=actor)
    assert result.decision == Decision.ALLOW
    assert result.outcome_category == "ACCEPTED"
    assert canonicalizer.calls == [{"value": "  padded  "}]
    stored = runtime.get_object("probe", raw=True)
    assert stored.data == {"value": "padded"}
    assert stored.type == KIND and stored.type_version == "test-v1" and stored.version == 1
    assert len(runtime.accepted_history("probe")) == 1
    assert runtime._extensions is extensions
    assert runtime._replayer._extensions is extensions
    assert set(extensions._reducers) == {KIND} and set(extensions._canonicalizers) == {KIND}
