# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Minimal test-owned composition; no historical inputs or implicit types."""
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.types.extensions import TypeExtensionRegistry
from tests.fixtures.reducers import SyntheticReducer


def synthetic_extensions(*kinds):
    extensions = TypeExtensionRegistry()
    for kind in kinds:
        extensions.register_reducer(kind, SyntheticReducer())
    return extensions


def generic_dependencies(*, extensions=None):
    return dict(
        extension_registry=TypeExtensionRegistry() if extensions is None else extensions,
        providers=ProviderRegistry(), application_registry=ApplicationRegistry(),
    )
