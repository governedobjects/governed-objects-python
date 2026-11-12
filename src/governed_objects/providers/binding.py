# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Generic provider binding — links canonical identity to external implementation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProviderBinding(BaseModel):
    """Reusable binding between a Governed Object and an implementation provider."""

    provider_type: str = "generic"
    provider_name: str = "local"
    external_id: str | None = None
    metadata: dict = Field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.provider_name == "local"

    @classmethod
    def from_metadata(cls, metadata: dict) -> ProviderBinding | None:
        raw = metadata.get("provider_binding")
        if not raw:
            return None
        normalized = dict(raw)
        # Legacy provider fields
        if "provider" in normalized and "provider_name" not in normalized:
            normalized["provider_name"] = normalized.pop("provider")
        if "provider_id" in normalized and "external_id" not in normalized:
            normalized["external_id"] = normalized.pop("provider_id")
        if "provider_type" not in normalized:
            normalized["provider_type"] = "generic"
        return cls(**normalized)

    def to_metadata(self) -> dict:
        return {"provider_binding": self.model_dump(exclude_none=True)}

    @classmethod
    def local_binding(cls) -> ProviderBinding:
        return cls(provider_type="local", provider_name="local")
