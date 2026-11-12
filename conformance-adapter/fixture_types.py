# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Suite-owned fixtures, implemented through the Python runtime.

The conformance suite defines every fixture type, actor and fixture rule the scenarios
depend on (``fixtures/fixtures.json`` in the suite) and sends it in ``define_fixture_types``.
This module only maps those definitions onto the Python runtime: it defines no fixture
rule of its own. Native action and kind names below are derived from the definitions."""

from __future__ import annotations

import re
from typing import Any

from governed_objects.types.schema import ActionDef, FieldSchema, ObjectTypeDefinition, StateDef

# Fixture types this adapter implements through the runtime's portable-type path
# (the rest use declarative type definitions plus ConformanceWorkItemAdapter).
PORTABLE_IDS = frozenset({"conformance.record"})

# Suite permission -> capability string used by the Python runtime's policy.
PERMISSION_CAPABILITY = {
    "read": "conformance.read",
    "create": "conformance.create",
    "update": "conformance.mutate",
    "add_relationship": "conformance.mutate",
    "remove_relationship": "conformance.mutate",
    "transition": "conformance.transition",
}


def native_kind(type_id: str) -> str:
    """conformance.work-item -> ConformanceWorkItem; portable types keep their identity."""
    if type_id in PORTABLE_IDS:
        return type_id
    return "".join(part.capitalize() for part in re.split(r"[.\-]", type_id))


def lifecycle_field(spec: dict[str, Any]) -> str | None:
    return (spec.get("lifecycle") or {}).get("field")


def data_fields(spec: dict[str, Any]) -> list[str]:
    lc = lifecycle_field(spec)
    return [f for f in spec["fields"] if f != lc]


def update_action(field: str) -> str:
    return f"update_{field}"


def target_actions(spec: dict[str, Any]) -> dict[str, str]:
    """Lifecycle target state -> native action name. A target no declared lifecycle action
    reaches gets an action ``reach_<state>`` that no state permits, so a request to enter
    it is evaluated by the runtime and refused."""
    mapping: dict[str, str] = {}
    for name, action in spec.get("actions", {}).items():
        if action["kind"] == "lifecycle":
            mapping[action["to"]] = name
    for state in (spec.get("lifecycle") or {}).get("states", []):
        mapping.setdefault(state, f"reach_{state}")
    return mapping


def effects(spec: dict[str, Any]) -> dict[str, str]:
    """Native action -> canonical history effect."""
    out = {
        "create": "object_created",
        "add_relationship": "relationship_added",
        "remove_relationship": "relationship_removed",
    }
    for field in data_fields(spec):
        out[update_action(field)] = "state_updated"
    for name, action in spec.get("actions", {}).items():
        out[name] = "state_updated" if action["kind"] == "state" else "lifecycle_transition"
    for action in target_actions(spec).values():
        out.setdefault(action, "lifecycle_transition")
    return out


def blank_rules(spec: dict[str, Any], op: str) -> list[str]:
    """Fields that must not be blank for ``op`` (create | update)."""
    return [
        r["field"]
        for r in spec.get("rules", [])
        if r["rule"] == "non-blank-string" and op in r["applies_to"]
    ]


def legacy_definition(type_id: str, spec: dict[str, Any]) -> ObjectTypeDefinition:
    """A declarative type definition built from a suite fixture definition."""
    lifecycle = spec["lifecycle"]
    required = set(spec.get("required_on_create", []))
    fields = {f: FieldSchema(type="string", required=f in required) for f in data_fields(spec)}
    cap = PERMISSION_CAPABILITY
    actions = {
        "create": ActionDef(requires=[cap["create"]]),
        "add_relationship": ActionDef(requires=[cap["add_relationship"]]),
        "remove_relationship": ActionDef(requires=[cap["remove_relationship"]]),
    }
    outgoing: dict[str, dict[str, str]] = {s: {} for s in lifecycle["states"]}
    for name, action in spec.get("actions", {}).items():
        if action["kind"] == "lifecycle":
            actions[name] = ActionDef(requires=[cap["transition"]])
            for source in action["from"]:
                outgoing[source][name] = action["to"]
    for action in target_actions(spec).values():
        actions.setdefault(action, ActionDef(requires=[cap["transition"]]))
    if any(a["kind"] == "state" for a in spec.get("actions", {}).values()):
        for field in data_fields(spec):
            actions[update_action(field)] = ActionDef(requires=[cap["update"]])
            for state, moves in outgoing.items():
                if moves:  # states with no outgoing lifecycle move are terminal and take no update
                    moves[update_action(field)] = state
    states = {s: StateDef(transitions=moves, terminal=not moves) for s, moves in outgoing.items()}
    return ObjectTypeDefinition(
        kind=native_kind(type_id),
        type_version=spec["version"],
        fields=fields,
        states=states,
        actions=actions,
        initial_state=lifecycle.get("initial") or lifecycle["states"][0],
        read_requires=[cap["read"]],
    )


def portable_description(type_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Input for ``build_type_definition`` (the runtime's portable-type path)."""
    lc = spec["lifecycle"]
    return {
        "type_identity": type_id,
        "type_version": spec["version"],
        "state_fields": dict(spec["fields"]),
        "lifecycle": {"states": list(lc["states"]), "transitions": [list(t) for t in lc["transitions"]]},
        "relationship_types": list(spec.get("relationship_types", [])),
        "actions": {
            name: ({"kind": "state"} if a["kind"] == "state" else {"kind": "lifecycle", "from": a["from"], "to": a["to"]})
            for name, a in spec["actions"].items()
        },
    }


def portable_requirements() -> dict[str, list[str]]:
    cap = PERMISSION_CAPABILITY
    return {
        "read": [cap["read"]],
        "create": [cap["create"]],
        "state": [cap["update"]],
        "lifecycle": [cap["transition"]],
        "relationship_add": [cap["add_relationship"]],
        "relationship_remove": [cap["remove_relationship"]],
    }
