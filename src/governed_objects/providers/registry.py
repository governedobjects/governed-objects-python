# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Provider registry — maps provider names to implementations."""

from __future__ import annotations

from governed_objects.providers.base import ObjectProvider
from governed_objects.providers.errors import ProviderProjectionUnsupported
from governed_objects.providers.projection import ProviderProjection
from uuid import uuid4


class ProviderRegistry:
    """Maps provider names to external/local implementations."""

    def __init__(self) -> None:
        self._providers: dict[str, ObjectProvider] = {}
        self._projections: dict[tuple[str, str, str], tuple[ProviderProjection, str]] = {}

    def register(self, provider: ObjectProvider) -> None:
        if provider.name in self._providers and self._providers[provider.name] is not provider:
            self._projections = {key: value for key, value in self._projections.items()
                                 if key[0] != provider.name}
        self._providers[provider.name] = provider

    def register_projection(
        self, provider_name: str, type_identity: str, type_version: str,
        projection: ProviderProjection,
    ) -> None:
        self.get(provider_name)
        if not callable(projection.normalize) or not callable(projection.provider_version):
            raise ValueError("projection requires explicit normalization and provider precondition extraction")
        if not all(isinstance(value, str) and value for value in
                   (provider_name, type_identity, type_version)):
            raise ValueError("projection requires exact provider, type identity and type version")
        if projection.capabilities is not None and projection.capabilities.provider_name != provider_name:
            raise ValueError("projection capability provider name mismatch")
        key = (provider_name, type_identity, type_version)
        if key in self._projections:
            if self._projections[key][0] != projection:
                raise ValueError("conflicting provider projection registration")
            return
        self._projections[key] = (projection, str(uuid4()))

    def remove_projection(self, provider_name: str, type_identity: str, type_version: str) -> None:
        self._projections.pop((provider_name, type_identity, type_version), None)

    def resolve_projection(
        self, provider_name: str, type_identity: str, type_version: str,
    ) -> tuple[ProviderProjection, str]:
        try:
            return self._projections[(provider_name, type_identity, type_version)]
        except KeyError as exc:
            raise ProviderProjectionUnsupported("PROVIDER_PROJECTION_NOT_CONFIGURED") from exc

    def get(self, name: str) -> ObjectProvider:
        if name not in self._providers:
            raise KeyError(f"unknown provider: {name}")
        return self._providers[name]
