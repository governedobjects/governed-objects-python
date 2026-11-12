# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Domain adapters for type-specific validation and side effects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from governed_objects.adapters.context import AdapterContext, AdapterExecuteResult, HydratedObject
from governed_objects.domain.command import Command
from governed_objects.domain.models import Actor, CommandResult, GovernedObject, ValidationCheck
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.observations.validation import CandidateImage, CandidateMode, CandidateVerdict


class TypeAdapter(ABC):
    """Hook for domain-specific validation, hydration, and command handling."""

    @abstractmethod
    def supports(self, object_type: str) -> bool: ...

    def validate_observation_candidate(
        self, *, type_def: ObjectTypeDefinition, current: CandidateImage | None,
        candidate: CandidateImage, mode: CandidateMode,
    ) -> CandidateVerdict:
        """Pure full-state/data invariant check; no create-validator fallback."""
        return CandidateVerdict.UNAVAILABLE

    @abstractmethod
    def validate_create(
        self,
        type_def: ObjectTypeDefinition,
        data: dict[str, Any],
    ) -> tuple[bool, list[ValidationCheck], str]: ...

    @abstractmethod
    def validate_transition(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        action: str,
        actor: Actor,
    ) -> tuple[bool, list[ValidationCheck], str]: ...

    def load(
        self,
        obj: GovernedObject,
        context: AdapterContext,
    ) -> HydratedObject | None:
        """Hydrate canonical view from backing implementation. None = use stored object."""
        return None

    def adapter_execute(
        self,
        obj: GovernedObject,
        command: Command,
        context: AdapterContext,
    ) -> AdapterExecuteResult | None:
        """Execute mutation via backing implementation. None = local runtime handles it."""
        return None

    def execute(
        self,
        command: Command,
        obj: GovernedObject | None,
        type_def: ObjectTypeDefinition,
        actor: Actor,
    ) -> CommandResult | None:
        """Optional override for non-standard commands (e.g. a type-specific command)."""
        return None

    def filter_read_data(
        self,
        obj: GovernedObject,
        type_def: ObjectTypeDefinition,
        actor: Actor,
        fields: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        return obj.data, []
