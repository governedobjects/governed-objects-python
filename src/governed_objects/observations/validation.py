# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Python candidate admission mechanics, not provider or authority operations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
import math
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING

from governed_objects.domain.models import GovernedObject
from governed_objects.types.schema import ObjectTypeDefinition

if TYPE_CHECKING:
    from governed_objects.adapters.base import TypeAdapter
    from governed_objects.adapters.registry import AdapterRegistry
    from governed_objects.types.registry import TypeRegistry


class CandidateVerdict(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class CandidateMode(StrEnum):
    REGISTRATION_INITIAL = "REGISTRATION_INITIAL"
    OBSERVATION = "OBSERVATION"
    REVIEW_ACCEPT = "REVIEW_ACCEPT"
    REPAIR_EXISTING = "REPAIR_EXISTING"


@dataclass(frozen=True)
class CandidateImage:
    identity: str
    type_identity: str
    type_version: str
    state: str
    data: Mapping[str, Any]


def candidate_image(obj: GovernedObject) -> CandidateImage:
    return CandidateImage(obj.id, obj.type, obj.type_version, obj.state, deepcopy(obj.data))


def _json_value(value: Any, active: set[int]) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in (dict, list) or id(value) in active:
        return False
    active.add(id(value))
    try:
        if type(value) is dict:
            return all(type(k) is str and _json_value(v, active) for k, v in value.items())
        return all(_json_value(v, active) for v in value)
    finally:
        active.remove(id(value))


def _readonly(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _readonly(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_readonly(v) for v in value)
    return value


def _hook_image(image: CandidateImage) -> CandidateImage:
    return CandidateImage(image.identity, image.type_identity, image.type_version,
                          image.state, _readonly(deepcopy(image.data)))


def validate_candidate(
    *, type_def: ObjectTypeDefinition, current: CandidateImage | None,
    candidate: CandidateImage, mode: CandidateMode, adapter: TypeAdapter | None,
) -> CandidateVerdict:
    """Check a detached full candidate without allocation, I/O or coercion.

    Callers establish exact support and capture definition/adapter identity first.
    The domain hook sees neither tokens nor operational/provider metadata.
    """
    try:
        if (not candidate.identity or not type_def.type_version or
            (candidate.type_identity, candidate.type_version) != (type_def.kind, type_def.type_version) or
            (current is not None and (candidate.identity, candidate.type_identity, candidate.type_version) !=
             (current.identity, current.type_identity, current.type_version))):
            return CandidateVerdict.INVALID
        if not isinstance(candidate.state, str) or not candidate.state or not _json_value(candidate.data, set()):
            return CandidateVerdict.INVALID
        predicates = {
            "string": lambda v: type(v) is str,
            "integer": lambda v: type(v) is int,
            "number": lambda v: type(v) in (int, float),
            "float": lambda v: type(v) in (int, float),
            "double": lambda v: type(v) in (int, float),
            "boolean": lambda v: type(v) is bool,
            "array": lambda v: type(v) is list,
            "object": lambda v: type(v) is dict,
            "date": lambda v: type(v) is str,
            "datetime": lambda v: type(v) is str,
        }
        for name, field in type_def.fields.items():
            if field.type not in predicates:
                return CandidateVerdict.UNAVAILABLE
            value = candidate.data.get(name)
            if field.required and (value is None or value == ""):
                return CandidateVerdict.INVALID
            if value is not None and not predicates[field.type](value):
                return CandidateVerdict.INVALID
        if type_def.states:
            if candidate.state not in type_def.states:
                return CandidateVerdict.INVALID
            if current is not None and candidate.state != current.state:
                state = type_def.states.get(current.state)
                if state is None or state.terminal or candidate.state not in state.transitions.values():
                    return CandidateVerdict.INVALID
        elif current is None:
            if not type_def.initial_state:
                return CandidateVerdict.UNAVAILABLE
            if candidate.state != type_def.initial_state:
                return CandidateVerdict.INVALID
        elif candidate.state != current.state:
            return CandidateVerdict.UNAVAILABLE
        if adapter is None:
            return CandidateVerdict.UNAVAILABLE
        definition = type_def.model_copy(deep=True)
        original = definition.model_copy(deep=True)
        result = adapter.validate_observation_candidate(
            type_def=definition, current=None if current is None else _hook_image(current),
            candidate=_hook_image(candidate), mode=mode,
        )
        if definition != original or not isinstance(result, CandidateVerdict):
            return CandidateVerdict.UNAVAILABLE
        return result
    except Exception:
        return CandidateVerdict.UNAVAILABLE


@dataclass(frozen=True)
class AdmissionSnapshot:
    """Per-call captured meaning; not a multi-version support registry."""
    definitions: tuple[ObjectTypeDefinition, ...]
    originals: tuple[ObjectTypeDefinition, ...]
    adapter: TypeAdapter | None
    adapter_order: tuple[TypeAdapter, ...]

    @property
    def definition(self) -> ObjectTypeDefinition:
        return self.definitions[-1]

    def matches(self, types: TypeRegistry, adapters: AdapterRegistry | None, obj: GovernedObject) -> bool:
        try:
            for original, captured in zip(self.originals, self.definitions):
                if types.get(captured.kind) is not original or original != captured:
                    return False
            order = () if adapters is None else tuple(adapters._adapters)
            return len(order) == len(self.adapter_order) and all(
                a is b for a, b in zip(order, self.adapter_order)
            )
        except Exception:
            return False


def capture_admission(types: TypeRegistry, adapters: AdapterRegistry | None,
                      obj: GovernedObject) -> AdmissionSnapshot:
    """Capture explicit supported meaning outside the pure validator/transaction."""
    from governed_objects.events.replay import bound_type_definition
    definition = bound_type_definition(types, obj)
    if definition is None:
        raise ValueError("unsupported binding")
    chain = []
    while definition is not None:
        if definition.kind in {d.kind for d in chain}:
            raise ValueError("unavailable ancestry")
        chain.append(definition)
        definition = types.get(definition.extends) if definition.extends else None
    try:
        adapter = None if adapters is None else adapters.resolve(obj)
    except KeyError:
        adapter = None
    originals = tuple(reversed(chain))
    order = () if adapters is None else tuple(adapters._adapters)
    return AdmissionSnapshot(tuple(d.model_copy(deep=True) for d in originals), originals, adapter, order)
