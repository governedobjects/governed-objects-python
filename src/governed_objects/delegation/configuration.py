# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Explicit local binding for the supported optional mandate interpretation.

No resource discovery or installation. The definition fingerprint denotes the
existing effective version-1 representation, not a normative profile standard.
Actor resolution is supplied local integration trust, not authentication.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING

from governed_objects.domain.models import Actor, GovernedObject
from governed_objects.types.schema import ObjectTypeDefinition

if TYPE_CHECKING:
    from governed_objects.adapters.mandate import MandateAdapter
    from governed_objects.events.reducers.mandate import MandateEventReducer


class DelegationConfigurationError(ValueError):
    def __init__(self):
        super().__init__('delegation configuration unavailable')


def _identity(definition):
    return hashlib.sha256(json.dumps(definition.model_dump(mode='json'), sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class DelegationConfiguration:
    definition: ObjectTypeDefinition
    adapter: MandateAdapter
    reducer: MandateEventReducer
    scope_decoder: Callable
    actor_source: Callable[[str], Actor]

    def __post_init__(self):
        from governed_objects.adapters.mandate import MandateAdapter
        from governed_objects.events.reducers.mandate import MandateEventReducer

        if (not isinstance(self.definition, ObjectTypeDefinition)
                or _identity(self.definition) != '7ad9425265abc99d9488be22483037c37fcd1815a7a74c6544c8d9c19d8800cb'
                or type(self.adapter) is not MandateAdapter
                or type(self.reducer) is not MandateEventReducer
                or not callable(self.scope_decoder) or not callable(self.actor_source)):
            raise DelegationConfigurationError()

    def validate(self, types, adapters, extensions, obj: GovernedObject | None = None):
        self.__post_init__()
        try:
            current = types.get(self.definition.kind)
        except KeyError:
            raise DelegationConfigurationError() from None
        if (current is not self.definition or _identity(current) != _identity(self.definition)
                or adapters.for_type(self.definition.kind) is not self.adapter
                or extensions.reducer_for(self.definition.kind) is not self.reducer
                or (obj is not None and (obj.type != self.definition.kind
                                        or obj.type_version != self.definition.type_version))):
            raise DelegationConfigurationError()

    def actor(self, identity: str) -> Actor:
        try:
            actor = self.actor_source(identity)
        except Exception:
            raise DelegationConfigurationError() from None
        if not isinstance(actor, Actor) or actor.id != identity:
            raise DelegationConfigurationError()
        return actor
