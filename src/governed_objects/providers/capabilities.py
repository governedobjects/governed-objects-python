# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Provider write/projection capability metadata."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderCapabilities:
    """Which canonical dimensions a provider can observe/write."""

    provider_name: str
    writable_fields: frozenset[str] = field(default_factory=frozenset)
    representable_fields: frozenset[str] = field(default_factory=frozenset)
    supports_conditional_write: bool = False
    supports_webhooks: bool = False
    supports_bootstrap: bool = False
    supports_create_from_canonical: bool = False

    def projection_gaps(self, canonical_fields: frozenset[str]) -> frozenset[str]:
        return canonical_fields - self.representable_fields

    def can_write(self, field_name: str) -> bool:
        return field_name in self.writable_fields

