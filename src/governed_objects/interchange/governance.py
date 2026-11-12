# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Type-scoped governance references (EXPERIMENTAL).

A governance configuration maps each supported ``(Type Identity, Type
Version)`` to one opaque reference. The runtime records the configured
reference on an object when it is created; it is never recomputed later and
never consulted for authorization. Authorization stays with the type's
capability requirements and the actor's capabilities.
"""

from __future__ import annotations

from collections.abc import Mapping


class TypeScopedGovernance:
    def __init__(self, name: str, references: Mapping[tuple[str, str], str]) -> None:
        self.name = name
        self._references = dict(references)

    def reference_for(self, type_identity: str, type_version: str) -> str | None:
        return self._references.get((type_identity, type_version))

    def references(self) -> dict[tuple[str, str], str]:
        return dict(self._references)
