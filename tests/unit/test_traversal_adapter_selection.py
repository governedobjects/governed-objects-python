# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Direct constructor selection, independent of runtime factory diagnostics."""
import pytest

from governed_objects.adapters.base import TypeAdapter
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.query.traversal import GraphTraversalService
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.validation import SchemaValidator


class SelectionAdapter(TypeAdapter):
    def supports(self, object_type):
        return object_type == "traversal.test/Record"

    def validate_create(self, type_def, data):
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(self, obj, type_def, action, actor):
        valid, reason, check = LifecycleValidator().validate_transition(type_def, obj.state, action)
        return valid, [check], reason


class FalseyAdapterList(list):
    def __bool__(self):
        return False


@pytest.fixture
def dependencies(tmp_path):
    store = SQLiteStore(tmp_path / "traversal.db")
    yield store, TypeRegistry()
    store.close()


@pytest.mark.parametrize("selection", ["omitted", "none"])
def test_absent_adapters_create_independent_empty_lists(dependencies, selection):
    store, types = dependencies
    options = {} if selection == "omitted" else {"adapters": None}
    first = GraphTraversalService(store, types, **options)
    second = GraphTraversalService(store, types, **options)
    assert first._adapters == second._adapters == []
    assert type(first._adapters) is type(second._adapters) is list
    assert first._adapters is not second._adapters
    assert first._store is store and first._types is types


@pytest.mark.parametrize("list_class", [list, FalseyAdapterList])
@pytest.mark.parametrize("populated", [False, True])
def test_supplied_list_preserves_identity_and_contents(dependencies, list_class, populated):
    store, types = dependencies
    adapter = SelectionAdapter()
    supplied = list_class([adapter] if populated else [])
    service = GraphTraversalService(store, types, adapters=supplied)
    assert service._adapters is supplied
    assert len(service._adapters) == int(populated)
    if populated:
        assert service._adapters[0] is adapter
        assert service._adapters[0].supports("traversal.test/Record")
        assert not service._adapters[0].supports("traversal.test/Other")
    # Observing the original list after mutation distinguishes retention from a copy.
    extra = SelectionAdapter()
    supplied.append(extra)
    assert service._adapters[-1] is extra
    assert len(service._adapters) == int(populated) + 1
