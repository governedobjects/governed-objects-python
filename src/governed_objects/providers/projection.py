# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Supplied, exact-bound provider interpretation. Technical ability is not authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from governed_objects.providers.capabilities import ProviderCapabilities
from governed_objects.providers.errors import ProviderProjectionUnsupported


@dataclass(frozen=True)
class ProviderProjection:
    normalize: Callable[[dict[str, Any]], tuple[dict[str, Any], str]]
    provider_version: Callable[[dict[str, Any]], str | None]
    capabilities: ProviderCapabilities | None = None
    provider_type: str = "generic"

    def version(self, record: dict[str, Any]) -> str:
        token = self.provider_version(record)
        if not isinstance(token, str) or not token:
            raise ProviderProjectionUnsupported("PROVIDER_PRECONDITION_UNAVAILABLE")
        return token
