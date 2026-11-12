# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Runs the README's example verbatim so it stays accurate."""
from __future__ import annotations

from pathlib import Path

from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import CreateObject
from governed_objects.domain.models import Actor, ActorType, Decision
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import FieldSchema, ObjectTypeDefinition


def test_readme_example_accepts_and_denies_as_documented():
    types = TypeRegistry()
    types.register(ObjectTypeDefinition(
        kind="example.widget", initial_state="active",
        states={"active": {}},
        fields={"label": FieldSchema(type="string", required=True)},
        actions={"create": {"requires": ["widget.create"]}},
        read_requires=["widget.read"],
    ))

    store = SQLiteStore(Path(":memory:"))
    runtime = GovernedObjectRuntime.configured(
        store=store, type_registry=types, adapters=[],
        extension_registry=TypeExtensionRegistry(), providers=ProviderRegistry(),
        application_registry=ApplicationRegistry(),
    )
    try:
        writer = Actor(id="writer", type=ActorType.SERVICE, capabilities=["widget.create", "widget.read"])
        result = runtime.execute(CreateObject("w-1", "example.widget", {"label": "first"}), actor=writer)
        assert (result.decision, result.object_version) == (Decision.ALLOW, 1)

        stranger = Actor(id="stranger", type=ActorType.SERVICE)
        result = runtime.execute(CreateObject("w-2", "example.widget", {"label": "second"}), actor=stranger)
        assert (result.decision, result.deny_reason) == (Decision.DENY, "ACTOR_CAPABILITY_MISSING")
    finally:
        store.close()
