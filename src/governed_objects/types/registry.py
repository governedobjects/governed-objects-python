# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Type registry — loads declarative object type definitions."""

from __future__ import annotations

from pathlib import Path

import yaml

from governed_objects.types.schema import ActionDef, FieldAccessRule, FieldSchema, ObjectTypeDefinition, StateDef

# Python reference-runtime compatibility default — not normative for the specification.
REFERENCE_RUNTIME_DEFAULT_TYPE_VERSION = "1"


class TypeRegistry:
    """Registry of governed object type definitions with optional inheritance."""

    def __init__(self) -> None:
        self._types: dict[str, ObjectTypeDefinition] = {}
        self._raw: dict[str, dict] = {}

    def register(self, type_def: ObjectTypeDefinition) -> None:
        self._types[type_def.kind] = type_def

    def get(self, kind: str) -> ObjectTypeDefinition:
        if kind not in self._types:
            raise KeyError(f"unknown object type: {kind}")
        return self._types[kind]

    def list_types(self) -> list[str]:
        return sorted(self._types)

    def is_subtype(self, child_kind: str, ancestor_kind: str) -> bool:
        if child_kind == ancestor_kind:
            return True
        try:
            child = self.get(child_kind)
        except KeyError:
            return False
        if not child.extends:
            return False
        if child.extends == ancestor_kind:
            return True
        return self.is_subtype(child.extends, ancestor_kind)

    def subtypes_of(self, ancestor_kind: str) -> list[str]:
        return sorted(
            kind for kind in self._types if kind != ancestor_kind and self.is_subtype(kind, ancestor_kind)
        )

    def kinds_matching(self, kind: str, *, include_subtypes: bool = False) -> list[str]:
        if include_subtypes:
            return [kind] + self.subtypes_of(kind)
        return [kind]

    def supports_type_version(self, kind: str, type_version: str) -> bool:
        try:
            type_def = self.get(kind)
        except KeyError:
            return False
        return type_def.type_version == type_version

    def canonical_state(self, object_type: str, domain_state: str) -> str:
        type_def = self.get(object_type)
        current = domain_state
        visited: set[str] = set()
        while type_def.kind not in visited:
            visited.add(type_def.kind)
            mapped = type_def.canonical_state(current)
            if mapped != current:
                return mapped
            if not type_def.extends:
                break
            type_def = self.get(type_def.extends)
        return current

    def load_directory(self, types_dir: Path) -> None:
        paths = sorted(types_dir.glob("*.yaml"))
        self._raw = {p.stem: (yaml.safe_load(p.read_text()) or {}) for p in paths}
        for path in paths:
            raw = self._raw[path.stem]
            self.register(_resolve_type(raw["kind"], self._raw))

def load_type_definition(path: Path) -> ObjectTypeDefinition:
    raw = yaml.safe_load(path.read_text()) or {}
    types_dir = path.parent
    all_raw = {p.stem: (yaml.safe_load(p.read_text()) or {}) for p in types_dir.glob("*.yaml")}
    return _resolve_type(raw["kind"], all_raw)


def _resolve_type(kind: str, all_raw: dict[str, dict]) -> ObjectTypeDefinition:
    raw = _find_raw_by_kind(kind, all_raw)
    parent_kind = raw.get("extends")
    if parent_kind:
        parent = _resolve_type(parent_kind, all_raw)
        fields = {**parent.fields, **_parse_schema(raw.get("schema") or {})}
        states = {**parent.states, **_parse_states(raw.get("states") or {})}
        actions = {**parent.actions, **_parse_actions(raw.get("actions") or {})}
        field_access = {**parent.field_access, **_parse_field_access(raw.get("field_access") or {})}
        if "read_requires" in raw:
            read_requires = raw.get("read_requires") or []
        else:
            read_requires = list(parent.read_requires)
        read_open = raw.get("read_open") if "read_open" in raw else parent.read_open
        canonical_state_map = {**parent.canonical_state_map, **(raw.get("canonical_state_map") or {})}
        initial_state = raw.get("initial_state") or parent.initial_state
    else:
        fields = _parse_schema(raw.get("schema") or {})
        states = _parse_states(raw.get("states") or {})
        actions = _parse_actions(raw.get("actions") or {})
        field_access = _parse_field_access(raw.get("field_access") or {})
        read_requires = raw.get("read_requires") or []
        read_open = bool(raw.get("read_open") or False)
        canonical_state_map = raw.get("canonical_state_map") or {}
        initial_state = raw.get("initial_state")

    consumed = {
        "kind", "extends", "schema", "states", "actions", "field_access",
        "initial_state", "read_requires", "read_open", "canonical_state_map", "version",
    }
    type_version = str(raw.get("version") or REFERENCE_RUNTIME_DEFAULT_TYPE_VERSION)
    if parent_kind and "version" not in raw:
        type_version = parent.type_version
    return ObjectTypeDefinition(
        kind=kind,
        type_version=type_version,
        extends=parent_kind,
        fields=fields,
        states=states,
        actions=actions,
        field_access=field_access,
        initial_state=initial_state,
        read_requires=read_requires,
        read_open=read_open,
        canonical_state_map=canonical_state_map,
        extra={k: v for k, v in raw.items() if k not in consumed},
    )


def _find_raw_by_kind(kind: str, all_raw: dict[str, dict]) -> dict:
    for raw in all_raw.values():
        if raw.get("kind") == kind:
            return raw
    raise KeyError(f"type definition not found: {kind}")


def _parse_schema(raw_schema: dict) -> dict[str, FieldSchema]:
    return {
        name: FieldSchema(**spec) if isinstance(spec, dict) else FieldSchema(type=str(spec))
        for name, spec in raw_schema.items()
    }


def _parse_states(raw_states: dict) -> dict[str, StateDef]:
    return {
        name: StateDef(**spec) if isinstance(spec, dict) else StateDef()
        for name, spec in raw_states.items()
    }


def _parse_actions(raw_actions: dict) -> dict[str, ActionDef]:
    return {
        name: ActionDef(**spec) if isinstance(spec, dict) else ActionDef()
        for name, spec in raw_actions.items()
    }


def _parse_field_access(raw_fa: dict) -> dict[str, FieldAccessRule]:
    return {name: FieldAccessRule(**spec) for name, spec in raw_fa.items()}
