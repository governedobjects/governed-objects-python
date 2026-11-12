# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""EXPERIMENTAL: provisional Core interchange export.

The portable type and governance definitions below are copies of the interchange
experiment's fixture definitions (Draft 2.1 content) so these native tests do not
depend on another checkout. The adapter receives the authoritative definitions at run
time.
"""

from __future__ import annotations
from tests.generic_bootstrap import generic_dependencies

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from governed_objects.domain.command import (
    AddRelationship,
    Command,
    CreateObject,
    ReadObject,
    RemoveRelationship,
    TransitionState,
)
from governed_objects.domain.actors import ActorRegistry
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import (
    Actor, ActorType, Decision, GovernanceEvent, GovernedObject,
)
from governed_objects.interchange import (
    EXPORT_HISTORY_UNCLASSIFIABLE,
    EXPORT_ORDER_INDETERMINATE,
    UNSUPPORTED_SEMANTIC_FEATURE,
)
from tests.fixtures.interchange import GRANT_CAPABILITIES, STANDARD_REQUIREMENTS
from governed_objects.interchange.export import ExportRefused
from governed_objects.interchange.governance import TypeScopedGovernance
from governed_objects.interchange.types import (
    PortableTypeAdapter,
    PortableTypeError,
    build_type_definition,
)
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.registry import TypeRegistry

PO, SUP, CC = (
    "example.procurement/PurchaseOrder",
    "example.procurement/Supplier",
    "example.procurement/CostCenter",
)

TYPES = [
    {
        "type_identity": PO,
        "type_version": "2",
        "state_fields": {
            "description": "string",
            "amount": "decimal-string",
            "currency": "string",
            "lifecycle": "lifecycle",
        },
        "lifecycle": {
            "states": ["draft", "submitted", "approved", "cancelled"],
            "transitions": [
                ["draft", "submitted"],
                ["submitted", "approved"],
                ["draft", "cancelled"],
                ["submitted", "cancelled"],
            ],
        },
        "relationship_types": ["supplier", "charged_to", "references"],
        "actions": {
            "update": {"kind": "state"},
            "submit": {"kind": "lifecycle", "from": ["draft"], "to": "submitted"},
            "approve": {"kind": "lifecycle", "from": ["submitted"], "to": "approved"},
            "cancel": {"kind": "lifecycle", "from": ["draft", "submitted"], "to": "cancelled"},
        },
    },
    {
        "type_identity": SUP,
        "type_version": "1",
        "state_fields": {"name": "string", "lifecycle": "lifecycle"},
        "lifecycle": {
            "states": ["active", "suspended"],
            "transitions": [["active", "suspended"], ["suspended", "active"]],
        },
        "relationship_types": [],
        "actions": {
            "update": {"kind": "state"},
            "suspend": {"kind": "lifecycle", "from": ["active"], "to": "suspended"},
            "reactivate": {"kind": "lifecycle", "from": ["suspended"], "to": "active"},
        },
    },
    {
        "type_identity": CC,
        "type_version": "1",
        "state_fields": {"code": "string", "name": "string"},
        "relationship_types": [],
        "actions": {"update": {"kind": "state"}},
    },
]

CFG_PRIMARY = {
    (PO, "2"): "example.procurement/governance/purchase-order-standard",
    (SUP, "1"): "example.procurement/governance/supplier-standard",
    (CC, "1"): "example.procurement/governance/cost-center-standard",
}
CFG_ALT = {**CFG_PRIMARY, (PO, "2"): "example.procurement/governance/purchase-order-alt"}

GRANTS = {
    "writer": ["read", "create", "state and lifecycle actions", "relationship add", "relationship remove"],
    "reader": ["read"],
    "writer-restricted": ["read", "relationship add", "relationship remove"],
}

PO_STATE = {"description": "Laptops for field team", "amount": "12500.00", "currency": "USD"}


def _actor(name: str) -> Actor:
    caps = [c for g in GRANTS[name] for c in GRANT_CAPABILITIES[g]]
    return Actor(id=name, type=ActorType.SERVICE, capabilities=caps)


WRITER, READER, RESTRICTED = _actor("writer"), _actor("reader"), _actor("writer-restricted")


def _runtime(db: Path, governance: dict | None = CFG_PRIMARY) -> GovernedObjectRuntime:
    registry = TypeRegistry()
    for t in TYPES:
        registry.register(build_type_definition(t, STANDARD_REQUIREMENTS))
    return GovernedObjectRuntime(
        SQLiteStore(db),
        type_registry=registry,
        **generic_dependencies(),
        adapters=[PortableTypeAdapter(registry)],
        governance=None if governance is None else TypeScopedGovernance("CFG", governance),
    )


@pytest.fixture
def rt(tmp_path):
    return _runtime(tmp_path / "rt.db")


def _ctx(version: int) -> CommandContext:
    return CommandContext(expected_version=version)


def _create_po(rt, identity="PO-4821", relationships=None):
    return rt.execute(
        CreateObject(identity, PO, dict(PO_STATE), state="draft", relationships=relationships),
        actor=WRITER,
    )


def _export(rt, *ids, closed=False, **kw):
    return rt.export_interchange_bundle(list(ids), actor=WRITER, closed=closed, **kw)


def _po_fixture(rt):
    """PO-4821 accepted sequence plus its noise (fixture-script.json)."""
    for identity, t, data, state in (
        ("CC-42", CC, {"code": "42", "name": "IT Hardware"}, None),
        ("CC-43", CC, {"code": "43", "name": "Field Operations"}, None),
        ("SUP-17", SUP, {"name": "Northwind Components"}, "active"),
    ):
        assert rt.execute(CreateObject(identity, t, data, state=state), actor=WRITER).decision == Decision.ALLOW
    outcomes = []
    run = lambda cmd, actor=WRITER, ctx=None: outcomes.append(
        rt._finalize(rt.execute(cmd, actor=actor, command_context=ctx)).outcome_category
    )
    run(CreateObject("PO-4821", PO, dict(PO_STATE), state="draft"))
    run(ReadObject("PO-4821"), READER)  # NZ-01
    run(Command("update", "PO-4821", payload={"set": {}}), READER)  # NZ-02
    run(AddRelationship("PO-4821", "supplier", "SUP-17"))
    run(AddRelationship("PO-4821", "supplier", "SUP-17"), ctx=_ctx(2))  # NZ-03
    run(ReadObject("PO-4821"))  # NZ-04
    run(AddRelationship("PO-4821", "charged_to", "CC-42"))
    run(Command("update", "PO-4821", payload={"set": {}}), ctx=_ctx(2))  # NZ-05
    run(Command("update", "PO-4821", payload={"set": {"amount": "11800.00"}}), ctx=_ctx(3))
    run(AddRelationship("PO-4821", "charged_to", ""))  # NZ-06
    run(RemoveRelationship("PO-4821", "charged_to", "CC-42"))
    run(RemoveRelationship("PO-4821", "charged_to", "CC-42"))  # NZ-07
    run(AddRelationship("PO-4821", "charged_to", "CC-43"))
    run(ReadObject("PO-4821"), READER)  # NZ-08
    run(TransitionState("PO-4821", "submit"))
    run(TransitionState("PO-4821", "draft"))  # NZ-09: no declared action reaches draft
    run(ReadObject("PO-4821"))  # NZ-10
    return outcomes


# --- fixture types ----------------------------------------------------------


def test_fixture_types_keep_exact_identity_and_version():
    po = build_type_definition(TYPES[0], STANDARD_REQUIREMENTS)
    assert (po.kind, po.type_version) == (PO, "2")
    assert set(po.states) == {"draft", "submitted", "approved", "cancelled"}
    assert po.transition_target("draft", "submit") == "submitted"
    assert po.transition_target("submitted", "update") == "submitted"
    assert po.transition_target("submitted", "draft") is None
    assert set(po.fields) == {"description", "amount", "currency"}
    cc = build_type_definition(TYPES[2], STANDARD_REQUIREMENTS)
    assert (cc.kind, cc.type_version) == (CC, "1")
    assert cc.transition_target("active", "update") == "active"


def test_fixture_type_refuses_undeclared_transition_and_reserved_action():
    bad = json.loads(json.dumps(TYPES[0]))
    bad["actions"]["reopen"] = {"kind": "lifecycle", "from": ["submitted"], "to": "draft"}
    with pytest.raises(PortableTypeError):
        build_type_definition(bad, STANDARD_REQUIREMENTS)
    bad = json.loads(json.dumps(TYPES[2]))
    bad["actions"]["read"] = {"kind": "state"}
    with pytest.raises(PortableTypeError):
        build_type_definition(bad, STANDARD_REQUIREMENTS)


def test_create_validates_portable_state(rt):
    bad = rt.execute(
        CreateObject("PO-1", PO, {**PO_STATE, "amount": "12,500"}, state="draft"), actor=WRITER
    )
    assert bad.decision == Decision.DENY
    extra = rt.execute(
        CreateObject("PO-2", PO, {**PO_STATE, "internal_note": "x"}, state="draft"), actor=WRITER
    )
    assert extra.decision == Decision.DENY


# --- fixture creation and noise --------------------------------------------


def test_fixture_sequence_and_noise_outcomes(rt):
    outcomes = _po_fixture(rt)
    assert outcomes == [
        "ACCEPTED", "ACCEPTED", "AUTHORIZATION_DENIED", "ACCEPTED", "ACCEPTED", "ACCEPTED",
        "ACCEPTED", "CONFLICT", "ACCEPTED", "SEMANTICALLY_INVALID", "ACCEPTED", "ACCEPTED",
        "ACCEPTED", "ACCEPTED", "ACCEPTED", "AUTHORIZATION_DENIED", "ACCEPTED",
    ]
    obj = rt.get_object("PO-4821")
    assert obj.version == 7 and obj.state == "submitted"
    assert obj.data == {**PO_STATE, "amount": "11800.00"}


def test_accepted_history_excludes_reads_noops_conflicts_denials_and_invalid(rt):
    _po_fixture(rt)
    all_rows = rt.history("PO-4821")
    accepted = rt.accepted_history("PO-4821")
    assert [r.action for r in accepted] == [
        "create", "add_relationship", "add_relationship", "update",
        "remove_relationship", "add_relationship", "submit",
    ]
    assert [r.history_position for r in accepted] == [1, 2, 3, 4, 5, 6, 7]
    reads = [r for r in all_rows if r.action == "read" and r.decision == Decision.ALLOW]
    assert len(reads) == 4 and all(r.history_position is None for r in reads)
    refused = [r for r in all_rows if r.decision != Decision.ALLOW]
    assert {r.decision for r in refused} == {Decision.DENY, Decision.CONFLICT}
    assert len(refused) == 4 and all(r.history_position is None for r in refused)

    history = _export(rt, "PO-4821")["objects"][0]["history"]
    assert [e["effect"] for e in history] == [
        "create", "relationship_add", "relationship_add", "state_change",
        "relationship_remove", "relationship_add", "lifecycle_transition",
    ]
    assert [e["resulting_object_version"] for e in history] == ["1", "2", "3", "4", "5", "6", "7"]
    assert all(e.get("action") != "read" for e in history)


def test_identical_value_update_is_an_accepted_change(rt):
    _create_po(rt, "PO-4824")
    rt.execute(Command("update", "PO-4824", payload={"set": {"amount": "12500.00"}}), actor=WRITER)
    history = _export(rt, "PO-4824")["objects"][0]["history"]
    assert [e["effect"] for e in history] == ["create", "state_change"]
    assert history[1]["post_image"] == history[0]["post_image"]


# --- history ordering -------------------------------------------------------


def test_positions_are_per_object_and_ignore_timestamps(tmp_path):
    db = tmp_path / "rt.db"
    rt = _runtime(db)
    _create_po(rt, "PO-A")
    _create_po(rt, "PO-B")
    rt.execute(AddRelationship("PO-A", "supplier", "S"), actor=WRITER)
    rt.execute(AddRelationship("PO-B", "supplier", "S"), actor=WRITER)
    rt.execute(Command("update", "PO-A", payload={"set": {"amount": "1.00"}}), actor=WRITER)
    before = _export(rt, "PO-A", "PO-B")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE events SET timestamp = '2000-01-01T00:00:00+00:00' WHERE history_position = 3")
    conn.execute("UPDATE events SET timestamp = '2099-01-01T00:00:00+00:00' WHERE history_position = 1")
    conn.commit()
    conn.close()
    after = _export(_runtime(db), "PO-A", "PO-B")
    assert after == before
    assert [e["position"] for e in after["objects"][0]["history"]] == [1, 2, 3]
    assert [e["position"] for e in after["objects"][1]["history"]] == [1, 2]


def test_positions_survive_vacuum(tmp_path):
    db = tmp_path / "rt.db"
    rt = _runtime(db)
    _po_fixture(rt)
    before = _export(rt, "PO-4821")
    conn = sqlite3.connect(db)
    conn.execute("VACUUM")
    conn.close()
    assert _export(_runtime(db), "PO-4821") == before


def test_unpositioned_canonical_row_is_order_indeterminate(rt):
    _create_po(rt)
    obj = rt.get_object("PO-4821")
    rt._store.append(
        GovernanceEvent(
            object_id=obj.id, object_type=obj.type, type_version="2", actor_id="writer",
            actor_type="service", action="update", decision=Decision.ALLOW,
            event_type="object.update", object_version=2,
            details={"accepted_post_image": {"data": obj.data, "state": "draft"}},
        )
    )
    with pytest.raises(ExportRefused) as exc:
        _export(rt, "PO-4821")
    assert exc.value.code == EXPORT_ORDER_INDETERMINATE


def test_positioned_read_row_is_unclassifiable(rt):
    _create_po(rt)
    obj = rt.get_object("PO-4821")
    rt._store.append_accepted(
        GovernanceEvent(
            object_id=obj.id, object_type=obj.type, type_version="2", actor_id="writer",
            actor_type="service", action="read", decision=Decision.ALLOW,
            event_type="object.read", object_version=1,
        )
    )
    with pytest.raises(ExportRefused) as exc:
        _export(rt, "PO-4821")
    assert exc.value.code == EXPORT_HISTORY_UNCLASSIFIABLE


def test_legacy_database_gains_columns_without_backfilled_positions(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE objects (id TEXT PRIMARY KEY, type TEXT NOT NULL, version INTEGER NOT NULL,
            state TEXT NOT NULL, data_json TEXT NOT NULL, policy_json TEXT NOT NULL,
            contract_json TEXT NOT NULL, metadata_json TEXT NOT NULL, storage_ref TEXT);
        CREATE TABLE events (event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
            object_id TEXT NOT NULL, object_type TEXT NOT NULL, actor_id TEXT NOT NULL,
            actor_type TEXT NOT NULL, action TEXT NOT NULL, decision TEXT NOT NULL,
            event_type TEXT NOT NULL, previous_state TEXT, new_state TEXT, reason TEXT,
            object_version INTEGER, details_json TEXT);
        INSERT INTO events VALUES ('e1', '2026-01-01T00:00:00+00:00', 'X', 'T', 'a', 'human',
            'create', 'ALLOW', 'object.create', NULL, 'draft', '', 1, '{}');
        """
    )
    conn.commit()
    conn.close()
    store = SQLiteStore(db)
    (row,) = store.list_for_object("X")
    assert row.history_position is None
    assert store.list_accepted_for_object("X") == []


# --- Type Version, relationships, state ------------------------------------


def test_type_version_comes_from_recorded_rows_and_is_not_defaulted(tmp_path):
    db = tmp_path / "rt.db"
    rt = _runtime(db)
    _create_po(rt)
    rt.execute(AddRelationship("PO-4821", "supplier", "SUP-17"), actor=WRITER)
    doc = _export(rt, "PO-4821")["objects"][0]
    assert doc["type_version"] == "2"
    assert [e["type_version"] for e in doc["history"]] == ["2", "2"]

    conn = sqlite3.connect(db)
    conn.execute("UPDATE events SET type_version = '1' WHERE history_position = 2")
    conn.commit()
    doc = _export(_runtime(db), "PO-4821")["objects"][0]
    assert [e["type_version"] for e in doc["history"]] == ["2", "1"]  # exported, not repaired

    conn.execute("UPDATE events SET type_version = NULL WHERE history_position = 2")
    conn.commit()
    conn.close()
    with pytest.raises(ExportRefused) as exc:
        _export(_runtime(db), "PO-4821")
    assert exc.value.code == EXPORT_HISTORY_UNCLASSIFIABLE


def test_relationships_export_as_triples_only(rt):
    _create_po(rt, relationships=[{"relation": "supplier", "target_id": "SUP-17", "target_type": SUP}])
    rt.execute(AddRelationship("PO-4821", "charged_to", "CC-42", target_type=CC), actor=WRITER)
    rt.execute(AddRelationship("PO-4821", "references", "CTR-9"), actor=WRITER)
    rt.execute(RemoveRelationship("PO-4821", "charged_to", "CC-42"), actor=WRITER)
    doc = _export(rt, "PO-4821")["objects"][0]
    assert doc["relationships"] == [
        {"source": "PO-4821", "type": "references", "target": "CTR-9"},
        {"source": "PO-4821", "type": "supplier", "target": "SUP-17"},
    ]
    assert doc["history"][0]["relationships"] == [
        {"source": "PO-4821", "type": "supplier", "target": "SUP-17"}
    ]
    assert doc["history"][1]["relationship"] == {"source": "PO-4821", "type": "charged_to", "target": "CC-42"}
    assert doc["history"][3]["effect"] == "relationship_remove"


def test_canonical_state_excludes_runtime_bookkeeping(rt):
    rt.execute(CreateObject("CC-42", CC, {"code": "42", "name": "IT Hardware"}), actor=WRITER)
    doc = _export(rt, "CC-42")["objects"][0]
    assert rt.get_object("CC-42").state == "active"  # native bookkeeping state
    assert doc["accepted_state"] == {"code": "42", "name": "IT Hardware"}
    assert doc["history"][0]["post_image"] == {"code": "42", "name": "IT Hardware"}
    assert set(doc) == {
        "canonical_identity", "type_identity", "type_version", "governance_reference",
        "accepted_state", "relationships", "current_object_version", "history",
    }


def test_post_images_are_recorded_by_the_runtime(rt):
    _create_po(rt)
    rt.execute(Command("update", "PO-4821", payload={"set": {"amount": "1.00"}}), actor=WRITER)
    rt.execute(TransitionState("PO-4821", "submit"), actor=WRITER)
    rows = rt.accepted_history("PO-4821")
    assert rows[1].details["accepted_post_image"] == {"data": {**PO_STATE, "amount": "1.00"}, "state": "draft"}
    assert rows[2].details["accepted_post_image"]["state"] == "submitted"


# --- governance reference -------------------------------------------------


def test_governance_reference_recorded_at_creation_not_recomputed(tmp_path):
    db = tmp_path / "rt.db"
    rt = _runtime(db)
    _create_po(rt)
    assert rt.get_object("PO-4821").governance_reference == CFG_PRIMARY[(PO, "2")]
    assert rt.accepted_history("PO-4821")[0].details["governance_reference"] == CFG_PRIMARY[(PO, "2")]
    reopened = _runtime(db, governance=CFG_ALT)
    doc = _export(reopened, "PO-4821")
    assert doc["objects"][0]["governance_reference"] == CFG_PRIMARY[(PO, "2")]
    assert doc["manifest"]["governance_references"] == [CFG_PRIMARY[(PO, "2")]]


def test_unmapped_portable_type_is_created_but_not_exported(tmp_path):
    rt = _runtime(tmp_path / "rt.db", governance={(SUP, "1"): "x"})
    assert _create_po(rt).decision == Decision.ALLOW
    obj = rt.get_object("PO-4821")
    assert obj.governance_reference is None
    assert "governance_reference" not in rt.accepted_history("PO-4821")[0].details
    with pytest.raises(ExportRefused) as exc:
        _export(rt, "PO-4821")
    assert exc.value.code == UNSUPPORTED_SEMANTIC_FEATURE
    assert "governance reference" in exc.value.message






def test_governance_reference_grants_nothing(rt):
    _create_po(rt)
    denied = rt._finalize(rt.execute(TransitionState("PO-4821", "submit"), actor=RESTRICTED))
    assert denied.outcome_category == "AUTHORIZATION_DENIED"
    with pytest.raises(ExportRefused) as exc:
        rt.export_interchange_bundle(
            ["PO-4821"], actor=Actor(id="nobody", type=ActorType.SERVICE), closed=False
        )
    assert exc.value.code == "AUTHORIZATION_DENIED"


def test_runtime_without_governance_keeps_draft21_behavior(tmp_path):
    rt = _runtime(tmp_path / "rt.db", governance=None)
    assert _create_po(rt).decision == Decision.ALLOW
    assert rt.get_object("PO-4821").governance_reference is None


# --- Object Version ---------------------------------------------------------


def test_object_version_exported_as_string_of_native_int(rt):
    _po_fixture(rt)
    obj = rt.get_object("PO-4821")
    assert type(obj.version) is int
    doc = _export(rt, "PO-4821")["objects"][0]
    assert doc["current_object_version"] == str(obj.version) == "7"
    assert all(type(e["resulting_object_version"]) is str for e in doc["history"])


def test_export_is_stable_across_reads(rt):
    _po_fixture(rt)
    first = _export(rt, "PO-4821", "SUP-17", "CC-42", "CC-43", closed=True)
    for _ in range(3):
        rt.execute(ReadObject("PO-4821"), actor=READER)
    assert _export(rt, "PO-4821", "SUP-17", "CC-42", "CC-43", closed=True) == first


def _seed_unregistered_type_object(rt, identity: str = "C-1") -> None:
    """Store an object whose type has no registered type definition.

    Replaces the removed ``register_container``: the object is stored as the
    runtime would have stored it (object row plus an accepted ``create``
    event) so read authority cannot be established for it.
    """
    obj = GovernedObject(
        id=identity, type="UnregisteredType", type_version="1", state="active",
        data={"path": "/nonexistent"}, storage_ref="/nonexistent",
    )
    rt._store.save(obj)
    rt._store.append_accepted(
        GovernanceEvent(
            object_id=identity, object_type=obj.type, action="create", actor_id="system",
            actor_type=ActorType.SERVICE.value, decision=Decision.ALLOW,
            event_type="object.created", reason="seeded for test", new_state=obj.state,
            object_version=obj.version, type_version=obj.type_version,
            details={"data": dict(obj.data)},
        )
    )


def test_non_portable_type_is_refused(rt):
    _seed_unregistered_type_object(rt)
    with pytest.raises(ExportRefused) as exc:
        _export(rt, "C-1")
    # No type definition is registered for this object here, so read authority cannot be
    # established and is refused before exportability is classified.
    assert exc.value.code == "AUTHORIZATION_DENIED"


def test_timestamps_are_optional_evidence(rt):
    _create_po(rt)
    plain = _export(rt, "PO-4821")
    stamped = _export(rt, "PO-4821", include_timestamps=True)
    assert "accepted_at" not in plain["objects"][0]["history"][0]
    assert stamped["objects"][0]["history"][0]["accepted_at"].endswith("+00:00")
    assert "exported_at" in stamped["manifest"]["provenance"]


# --- adapter executable -----------------------------------------------------

