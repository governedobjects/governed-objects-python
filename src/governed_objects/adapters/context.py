# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Adapter execution context and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import GovernedObject, Relationship
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.types.registry import TypeRegistry


@dataclass
class AdapterContext:
    store: SQLiteStore
    providers: ProviderRegistry
    type_registry: TypeRegistry
    command_context: CommandContext | None = None


@dataclass
class HydratedObject:
    """Provider-observed view — not accepted canonical truth until ingested."""

    object: GovernedObject
    relationships: list[Relationship] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    provider_observation: dict[str, Any] | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "local"


class AdapterFailure(StrEnum):
    """Why an adapter did not execute a mutation."""

    INVALID = "invalid"  # the requested change violates the type's declared rules
    PROVIDER = "provider"  # the backing provider failed on the request


@dataclass
class AdapterExecuteResult:
    """``failure`` is None when ``success`` is true and MUST classify the
    failure when it is false. The runtime refuses a result that breaks this
    (``ADAPTER_RESULT_INVALID``) rather than guessing the failure class."""

    success: bool
    object: GovernedObject | None = None
    relationships: list[Relationship] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    failure: AdapterFailure | None = None
