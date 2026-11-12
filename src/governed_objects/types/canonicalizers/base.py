# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Type-specific canonicalization at the Governed Object boundary."""

from __future__ import annotations

from typing import Any, Protocol


class TypeCanonicalizer(Protocol):
    """Normalize type-specific aliases into canonical object data."""

    def canonicalize_create(self, data: dict[str, Any]) -> dict[str, Any]: ...

    def canonicalize_update(self, data: dict[str, Any]) -> dict[str, Any]: ...
