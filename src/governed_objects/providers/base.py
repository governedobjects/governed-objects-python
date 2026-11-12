# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Generic external object provider protocol."""

from __future__ import annotations

from typing import Any, Protocol


class ObjectProvider(Protocol):
    """Provider-native operations — domain mappers live in adapters."""

    @property
    def name(self) -> str: ...

    def fetch(self, external_id: str) -> dict[str, Any]: ...

    def apply_action(
        self,
        external_id: str,
        action: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    def create(self, external_id: str, record: dict[str, Any]) -> dict[str, Any]: ...
