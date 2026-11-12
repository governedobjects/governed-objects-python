# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Declarative portable Core types (EXPERIMENTAL).

Builds native ``ObjectTypeDefinition`` values from a portable type definition
(the shape of the experiment's portable type-definition JSON)
so the fixture runs under its exact Type Identity and Type Version strings,
with no type mapping table.

Native mapping:

- non-lifecycle state fields become required string fields in ``data``;
- the lifecycle field (kind ``lifecycle``) is the native ``state``;
- a lifecycle action becomes a native transition from each declared source
  state; a state action becomes a self-transition in every state and is
  applied by :class:`PortableTypeAdapter`;
- a type without a lifecycle gets one native bookkeeping state,
  ``NO_LIFECYCLE_STATE``. It is not canonical state and is never exported.

The portable definition is kept in ``extra[PORTABLE_TYPE_KEY]``; the exporter
reads the lifecycle field and action kinds from there.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import AdapterContext, AdapterExecuteResult, AdapterFailure
from governed_objects.domain.command import Command
from governed_objects.domain.models import Actor, GovernedObject, ValidationCheck
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ActionDef, FieldSchema, ObjectTypeDefinition, StateDef
from governed_objects.observations.validation import CandidateImage, CandidateMode, CandidateVerdict

PORTABLE_TYPE_KEY = "interchange_portable_type"
NO_LIFECYCLE_STATE = "active"

FIELD_STRING = "string"
FIELD_DECIMAL = "decimal-string"
FIELD_LIFECYCLE = "lifecycle"
ACTION_STATE = "state"
ACTION_LIFECYCLE = "lifecycle"

REQUIREMENT_KEYS = ("read", "create", "state", "lifecycle", "relationship_add", "relationship_remove")

_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


class PortableTypeError(ValueError):
    pass


def lifecycle_field(portable: Mapping[str, Any]) -> str | None:
    for name, kind in portable["state_fields"].items():
        if kind == FIELD_LIFECYCLE:
            return name
    return None


def portable_type_of(type_def: ObjectTypeDefinition) -> Mapping[str, Any] | None:
    return type_def.extra.get(PORTABLE_TYPE_KEY)


def build_type_definition(
    portable: Mapping[str, Any],
    requirements: Mapping[str, list[str]],
    *,
    open_keys: Collection[str] = (),
) -> ObjectTypeDefinition:
    """``requirements`` holds the capability requirements selected by the
    local governance for this binding, keyed by ``REQUIREMENT_KEYS``.

    An empty requirement list for a key denies that action/read to every
    actor, the same as the native runtime default. ``open_keys`` names the
    ``REQUIREMENT_KEYS`` local governance has deliberately chosen to leave
    open to every actor regardless of declared capabilities; pass a key
    there only when openness is the intended behavior, not merely because
    its requirement list happens to be empty."""
    missing = [k for k in REQUIREMENT_KEYS if k not in requirements]
    if missing:
        raise PortableTypeError(f"governance requirements missing {missing}")
    fields = dict(portable["state_fields"])
    for name, kind in fields.items():
        if kind not in (FIELD_STRING, FIELD_DECIMAL, FIELD_LIFECYCLE):
            raise PortableTypeError(f"unsupported field kind {kind!r} for {name!r}")
    lc_fields = [n for n, k in fields.items() if k == FIELD_LIFECYCLE]
    if len(lc_fields) > 1:
        raise PortableTypeError("more than one lifecycle field")
    actions = dict(portable.get("actions", {}))
    for reserved in ("create", "read", "add_relationship", "remove_relationship"):
        if reserved in actions:
            raise PortableTypeError(f"type action {reserved!r} collides with a runtime operation")

    if lc_fields:
        lifecycle = portable["lifecycle"]
        state_names = list(lifecycle["states"])
        permitted = {(a, b) for a, b in lifecycle["transitions"]}
    else:
        state_names = [NO_LIFECYCLE_STATE]
        permitted = set()

    def _action_def(requirement_key: str) -> ActionDef:
        """An empty requirement list denies, unless ``requirement_key`` is
        named in ``open_keys``, in which case the action is explicitly
        opened to every actor."""
        caps = list(requirements[requirement_key])
        return ActionDef(requires=caps, open=requirement_key in open_keys)

    native_actions: dict[str, ActionDef] = {
        "create": _action_def("create"),
        "add_relationship": _action_def("relationship_add"),
        "remove_relationship": _action_def("relationship_remove"),
    }
    transitions: dict[str, dict[str, str]] = {s: {} for s in state_names}
    for name, spec in actions.items():
        kind = spec["kind"]
        if kind == ACTION_STATE:
            native_actions[name] = _action_def("state")
            for s in state_names:
                transitions[s][name] = s
        elif kind == ACTION_LIFECYCLE:
            if not lc_fields:
                raise PortableTypeError(f"lifecycle action {name!r} on a type without lifecycle")
            native_actions[name] = _action_def("lifecycle")
            for source in spec["from"]:
                if (source, spec["to"]) not in permitted:
                    raise PortableTypeError(
                        f"action {name!r} {source}->{spec['to']} is not a declared transition"
                    )
                transitions[source][name] = spec["to"]
        else:
            raise PortableTypeError(f"unsupported action kind {kind!r} for {name!r}")

    return ObjectTypeDefinition(
        kind=portable["type_identity"],
        type_version=str(portable["type_version"]),
        fields={
            n: FieldSchema(type="string", required=True)
            for n, k in fields.items()
            if k != FIELD_LIFECYCLE
        },
        states={s: StateDef(transitions=t) for s, t in transitions.items()},
        actions=native_actions,
        read_requires=list(requirements["read"]),
        read_open="read" in open_keys,
        initial_state=state_names[0],
        extra={PORTABLE_TYPE_KEY: dict(portable)},
    )


def data_problems(portable: Mapping[str, Any], data: Mapping[str, Any], *, partial: bool) -> list[str]:
    """Problems with non-lifecycle field values. ``partial`` allows a subset
    (a state action's ``set``); otherwise every declared field is required."""
    declared = {n: k for n, k in portable["state_fields"].items() if k != FIELD_LIFECYCLE}
    problems = [f"unknown or non-state field {n!r}" for n in sorted(set(data) - set(declared))]
    if not partial:
        problems += [f"missing declared field {n!r}" for n in sorted(set(declared) - set(data))]
    for name in sorted(set(declared) & set(data)):
        value = data[name]
        if not isinstance(value, str):
            problems.append(f"field {name!r} must be a string")
        elif declared[name] == FIELD_DECIMAL and not _DECIMAL.match(value):
            problems.append(f"field {name!r} is not a decimal string")
    return problems


class PortableTypeAdapter(TypeAdapter):
    """Validation and state-action handling for types built by
    :func:`build_type_definition`."""

    def __init__(self, types: TypeRegistry) -> None:
        self._types = types

    def validate_observation_candidate(
        self, *, type_def: ObjectTypeDefinition, current: CandidateImage | None,
        candidate: CandidateImage, mode: CandidateMode,
    ) -> CandidateVerdict:
        portable = portable_type_of(type_def)
        if portable is None:
            return CandidateVerdict.UNAVAILABLE
        return (CandidateVerdict.INVALID if data_problems(portable, candidate.data, partial=False)
                else CandidateVerdict.VALID)

    def supports(self, object_type: str) -> bool:
        try:
            return portable_type_of(self._types.get(object_type)) is not None
        except KeyError:
            return False

    def validate_create(
        self, type_def: ObjectTypeDefinition, data: dict[str, Any]
    ) -> tuple[bool, list[ValidationCheck], str]:
        problems = data_problems(portable_type_of(type_def), data, partial=False)
        checks = [ValidationCheck(name="portable_state", passed=not problems, message="; ".join(problems))]
        return not problems, checks, "; ".join(problems)

    def validate_transition(
        self, obj: GovernedObject, type_def: ObjectTypeDefinition, action: str, actor: Actor
    ) -> tuple[bool, list[ValidationCheck], str]:
        return True, [], ""

    def adapter_execute(
        self, obj: GovernedObject, command: Command, context: AdapterContext
    ) -> AdapterExecuteResult | None:
        portable = portable_type_of(self._types.get(obj.type))
        spec = portable.get("actions", {}).get(command.action)
        if spec is None or spec["kind"] != ACTION_STATE:
            return None
        changes = dict(command.payload.get("set") or {})
        problems = data_problems(portable, changes, partial=True)
        if not changes:
            problems.append("state action requires at least one field")
        if problems:
            return AdapterExecuteResult(
                success=False,
                error_code="STATE_INVALID",
                error_message="; ".join(problems),
                failure=AdapterFailure.INVALID,
            )
        return AdapterExecuteResult(
            success=True,
            object=obj.model_copy(
                update={"data": {**obj.data, **changes}, "version": obj.version + 1}
            ),
        )
