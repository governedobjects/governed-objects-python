# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Test-owned non-business provider; usable without pytest or reference imports."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import HydratedObject
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.capabilities import ProviderCapabilities
from governed_objects.providers.errors import ProviderConflict, ProviderObjectNotFound, ProviderOutcomeUnknown
from governed_objects.providers.projection import ProviderProjection
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.validation import SchemaValidator

KIND = "projection.test/Cell"
VERSION = "opaque:type/first"


class RawProvider:
    def __init__(self, name="unrelated-b"):
        self.name = name
        self.records = {"wire-id": {"signal": "accepted", "phase": "O", "revision": "opaque:rev/zero"}}
        self.writes = []
        self.fetches = []
        self.keys = {}
        self.unknown = False
        self.fetch_hook = None

    def fetch(self, external_id):
        self.fetches.append(external_id)
        if self.fetch_hook:
            self.fetch_hook()
        if external_id not in self.records:
            raise ProviderObjectNotFound("missing cell")
        return deepcopy(self.records[external_id])

    def apply_patch(self, external_id, changes, *, expected_version, repair_id, idempotency_key):
        if idempotency_key in self.keys:
            return deepcopy(self.keys[idempotency_key])
        if self.records[external_id]["revision"] != expected_version:
            raise ProviderConflict("changed cell")
        self.writes.append((external_id, deepcopy(changes), expected_version, repair_id, idempotency_key))
        record = self.records[external_id]
        if "value" in changes:
            record["signal"] = changes["value"]
        if "state" in changes:
            record["phase"] = {"open": "O", "closed": "C"}[changes["state"]]
        record["revision"] = "opaque:rev/after-write"
        self.keys[idempotency_key] = deepcopy(record)
        if self.unknown:
            raise ProviderOutcomeUnknown("cell acknowledgement lost")
        return deepcopy(record)

    def create_from_canonical(self, external_id, data, state):
        self.writes.append((external_id, deepcopy(data), state))
        self.records[external_id] = {"signal": data["value"], "phase": {"open": "O", "closed": "C"}[state],
                                     "revision": "opaque:rev/created"}
        return deepcopy(self.records[external_id])


def normalize(record):
    return {"value": record["signal"]}, {"O": "open", "C": "closed"}[record["phase"]]


def revision(record):
    return record.get("revision")


def projection(provider_name="unrelated-b", **caps_changes):
    caps = ProviderCapabilities(provider_name=provider_name, writable_fields=frozenset({"value", "state"}),
                                representable_fields=frozenset({"value", "state"}),
                                supports_conditional_write=True, supports_bootstrap=True,
                                supports_create_from_canonical=True)
    return ProviderProjection(normalize, revision, replace(caps, **caps_changes), "test-cell")


class CellAdapter(TypeAdapter):
    def validate_observation_candidate(self, *, type_def, current, candidate, mode):
        from governed_objects.observations.validation import CandidateVerdict
        return CandidateVerdict.VALID

    def __init__(self, providers):
        self.providers = providers

    def supports(self, object_type):
        return object_type == KIND

    def validate_create(self, type_def, data):
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(self, obj, type_def, action, actor):
        valid, reason, check = LifecycleValidator().validate_transition(type_def, obj.state, action)
        return valid, [check], reason

    def load(self, obj, context):
        binding = ProviderBinding.from_metadata(obj.metadata)
        if binding is None or binding.is_local:
            return None
        descriptor, _ = self.providers.resolve_projection(binding.provider_name, obj.type, obj.type_version)
        record = self.providers.get(binding.provider_name).fetch(binding.external_id)
        data, state = descriptor.normalize(record)
        return HydratedObject(object=obj.model_copy(update={"data": data, "state": state}), source=binding.provider_name)


def build_scenario(path, *, provider_name="unrelated-b"):
    types = TypeRegistry()
    types.register(ObjectTypeDefinition(
        kind=KIND, type_version=VERSION, initial_state="open",
        states={"open": {"transitions": {"close": "closed"}}, "closed": {"terminal": True}},
        schema={"value": {"type": "string", "required": True}},
        actions={"create": {"requires": ["cell.create"]}, "close": {"requires": ["cell.close"]}},
        read_requires=["cell.read"], extra={"authority": {"fields": {
            "value": "CANONICAL_AUTHORITATIVE", "state": "CANONICAL_AUTHORITATIVE"}}},
    ))
    providers = ProviderRegistry()
    provider = RawProvider(provider_name)
    providers.register(provider)
    descriptor = projection(provider_name)
    providers.register_projection(provider_name, KIND, VERSION, descriptor)
    store = SQLiteStore(path)
    runtime = GovernedObjectRuntime.configured(
        store=store, type_registry=types, providers=providers, adapters=[CellAdapter(providers)],
        extension_registry=TypeExtensionRegistry(), application_registry=ApplicationRegistry(),
    )
    runtime.registration.register_with_binding(
        "cell", KIND, ProviderBinding(provider_name=provider_name, external_id="wire-id", provider_type="test-cell"),
        context=runtime.context,
    )
    assert store.get("cell").data == {"value": "accepted"}
    provider.records["wire-id"]["signal"] = "drift"
    provider.records["wire-id"]["revision"] = "opaque:rev/drift"
    return SimpleNamespace(runtime=runtime, store=store, provider=provider, providers=providers,
                           types=types, descriptor=descriptor)


def snapshot(scenario):
    runtime, store = scenario.runtime, scenario.store
    return dict(object=store.get("cell").model_dump(mode="json"),
                history=[event.model_dump(mode="json") for event in runtime.accepted_history("cell")],
                high_water=store.version_high_water("cell"),
                relationships=[rel.model_dump(mode="json") for rel in runtime.canonical_relationships("cell")])
