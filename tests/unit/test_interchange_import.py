# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""EXPERIMENTAL: privileged new-target import.

Source bundles come from the ordinary exporter of a separate runtime,
or are written by hand for histories no ordinary command sequence here
produces (a valid alternative order, skewed timestamps, sparse tokens).

The exporter writes artifact v2 with Draft 2.2 content. These tests exercise
the predecessor path, so exporter output is used as a source through
``_source``, which presents the same content as an artifact v1, Draft 2.1
bundle. A re-export carries the export identity (``_reexported``).
"""

from __future__ import annotations
from tests.generic_bootstrap import generic_dependencies

import copy
import json
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
from governed_objects.domain.models import Actor, ActorType, Decision
from governed_objects.interchange import (
    ARTIFACT_REVISION_V1,
    ARTIFACT_REVISION_V2,
    ARTIFACT_STATUS_V1,
    ARTIFACT_STATUS_V2,
    EXPORT_SPEC_REVISION,
    SPEC_REVISION,
)
from tests.fixtures.interchange import GRANT_CAPABILITIES, STANDARD_REQUIREMENTS
from governed_objects.interchange.artifact import UNSUPPORTED_ARTIFACT_REVISION, decode_artifact
from governed_objects.interchange.governance import TypeScopedGovernance
from governed_objects.interchange.importer import (
    AUTHORIZATION_DENIED,
    HISTORY_INCONSISTENT,
    HISTORY_NOT_VALID_FOR_TYPE,
    HISTORY_SNAPSHOT_MISMATCH,
    IDENTITY_ALREADY_EXISTS,
    MALFORMED_ARTIFACT,
    MULTISET_HISTORY,
    STATE_NOT_VALID_FOR_TYPE,
    UNRESOLVED_GOVERNANCE_REFERENCE,
    UNSUPPORTED_OBJECT_VERSION_REPRESENTATION,
    UNSUPPORTED_SEMANTIC_FEATURE,
    UNSUPPORTED_TYPE,
    UNSUPPORTED_TYPE_VERSION,
    VERSION_TOKEN_REUSE,
    ImportRefused,
)
from governed_objects.interchange.types import PortableTypeAdapter, build_type_definition
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.registry import TypeRegistry
from test_interchange_export import (
    CC,
    CFG_ALT,
    CFG_PRIMARY,
    PO,
    PO_STATE,
    READER,
    SUP,
    TYPES,
    WRITER,
    _po_fixture,
)

IMPORTER = Actor(id="importer", type=ActorType.SERVICE, capabilities=list(GRANT_CAPABILITIES["import"]))
PO_REF = CFG_PRIMARY[(PO, "2")]
FULL = ["PO-4821", "SUP-17", "CC-42", "CC-43"]


def _runtime(db: Path, governance: dict | None = CFG_PRIMARY, types=TYPES) -> GovernedObjectRuntime:
    registry = TypeRegistry()
    for t in types:
        registry.register(build_type_definition(t, STANDARD_REQUIREMENTS))
    return GovernedObjectRuntime(
        SQLiteStore(db),
        type_registry=registry,
        **generic_dependencies(),
        adapters=[PortableTypeAdapter(registry)],
        governance=None if governance is None else TypeScopedGovernance("CFG", governance),
    )


@pytest.fixture
def source(tmp_path):
    rt = _runtime(tmp_path / "source.db")
    _po_fixture(rt)
    return rt


@pytest.fixture
def receiver(tmp_path):
    return _runtime(tmp_path / "receiver.db")


def _export(rt, ids=FULL, closed=True, **kw) -> dict:
    return rt.export_interchange_bundle(list(ids), actor=WRITER, closed=closed, **kw)


V1_IDENTITY = {"artifact_status": ARTIFACT_STATUS_V1, "artifact_revision": ARTIFACT_REVISION_V1,
               "spec_revision": SPEC_REVISION}
V2_IDENTITY = {"artifact_status": ARTIFACT_STATUS_V2, "artifact_revision": ARTIFACT_REVISION_V2,
               "spec_revision": EXPORT_SPEC_REVISION}


def _source(rt, ids=FULL, closed=True, **kw) -> dict:
    doc = _export(rt, ids, closed, **kw)
    assert {k: doc["manifest"][k] for k in V2_IDENTITY} == V2_IDENTITY
    doc["manifest"].update(V1_IDENTITY)
    return doc


def _reexported(doc: dict) -> dict:
    out = copy.deepcopy(doc)
    out["manifest"].update(V2_IDENTITY)
    return out


def _import(rt, doc, actor=IMPORTER):
    return rt.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor)


def _refused(rt, doc, code, actor=IMPORTER) -> ImportRefused:
    before = rt._store.dump_tables()
    with pytest.raises(ImportRefused) as exc:
        _import(rt, doc, actor)
    assert exc.value.code == code, exc.value
    assert rt._store.dump_tables() == before, "a refused import changed the store"
    return exc.value


def _semantic(doc: dict) -> dict:
    """The bundle without transfer provenance (Operational Metadata)."""
    out = copy.deepcopy(doc)
    out["manifest"].pop("provenance", None)
    return out


def _entry(position, effect, token, **content):
    e = {"position": position, "effect": effect, "type_identity": PO, "type_version": "2",
         "resulting_object_version": token}
    e.update(content)
    e.setdefault("actor", "writer")
    return e


def _rel(rtype, target):
    return {"source": "PO-4821", "type": rtype, "target": target}


def _reorder_bundle(tokens=("1", "2", "3", "5", "4", "6", "7"), stamps=None) -> dict:
    """B-REORDER shape: the amount update precedes the CC-42 removal, and the
    tokens are not monotonic in position order."""
    after = {**PO_STATE, "amount": "11800.00"}
    history = [
        _entry(1, "create", tokens[0], post_image={**PO_STATE, "lifecycle": "draft"}, relationships=[], action="create"),
        _entry(2, "relationship_add", tokens[1], relationship=_rel("supplier", "SUP-17"), action="add_relationship"),
        _entry(3, "relationship_add", tokens[2], relationship=_rel("charged_to", "CC-42"), action="add_relationship"),
        _entry(4, "state_change", tokens[3], post_image={**after, "lifecycle": "draft"}, action="update"),
        _entry(5, "relationship_remove", tokens[4], relationship=_rel("charged_to", "CC-42"), action="remove_relationship"),
        _entry(6, "relationship_add", tokens[5], relationship=_rel("charged_to", "CC-43"), action="add_relationship"),
        _entry(7, "lifecycle_transition", tokens[6], post_image={**after, "lifecycle": "submitted"}, action="submit"),
    ]
    for e, stamp in zip(history, stamps or []):
        e["accepted_at"] = stamp
    return {
        "manifest": {
            "artifact_status": "EXPERIMENTAL; NON-NORMATIVE; NOT A GOVERNED OBJECT WIRE STANDARD; "
            "NOT A DRAFT 2.1 REQUIREMENT",
            "artifact_revision": "go-core-interchange-experiment/1",
            "spec_revision": "v0alpha1/draft2.1",
            "history_completeness": "complete-from-creation",
            "history_position_model": "explicit-per-object-contiguous-from-1",
            "governance_reference_model": "type-scoped",
            "profiles": [],
            "identities": ["PO-4821"],
            "type_bindings": [{"type_identity": PO, "type_version": "2"}],
            "governance_references": [PO_REF],
            "closed": False,
        },
        "objects": [
            {
                "canonical_identity": "PO-4821",
                "type_identity": PO,
                "type_version": "2",
                "governance_reference": PO_REF,
                "accepted_state": {**after, "lifecycle": "submitted"},
                "relationships": [_rel("charged_to", "CC-43"), _rel("supplier", "SUP-17")],
                "current_object_version": tokens[6],
                "history": history,
            }
        ],
    }


# --- successful restoration ---------------------------------------------------


def test_import_restores_every_semantic_concept_natively(source, receiver):
    doc = _source(source)
    result = _import(receiver, doc)
    assert result.identities == tuple(FULL)
    for src in doc["objects"]:
        ident = src["canonical_identity"]
        obj = receiver.get_object(ident)
        assert (obj.id, obj.type, obj.type_version) == (ident, src["type_identity"], src["type_version"])
        assert obj.governance_reference == src["governance_reference"]
        assert str(obj.version) == src["current_object_version"]
        state = dict(obj.data)
        if "lifecycle" in src["accepted_state"]:
            state["lifecycle"] = obj.state
        assert state == src["accepted_state"]
        rels = sorted((r.relation, r.target_id) for r in receiver.canonical_relationships(ident))
        assert rels == sorted((r["type"], r["target"]) for r in src["relationships"])
        rows = receiver.accepted_history(ident)
        assert [r.history_position for r in rows] == [e["position"] for e in src["history"]]
        assert [str(r.object_version) for r in rows] == [e["resulting_object_version"] for e in src["history"]]
        assert [r.actor_id for r in rows] == [e["actor"] for e in src["history"]]


def test_governed_read_serves_imported_object(source, receiver):
    _import(receiver, _source(source))
    result = receiver.execute(ReadObject("PO-4821"), actor=READER)
    assert result.decision == Decision.ALLOW
    assert result.data["amount"] == "11800.00" and str(result.object_version) == "7"


def test_re_export_after_import_matches_source(source, receiver):
    doc = _source(source)
    _import(receiver, doc)
    assert _semantic(_export(receiver)) == _semantic(_reexported(doc))


def test_restored_state_survives_reopen_without_the_artifact(source, tmp_path):
    doc = _source(source)
    db = tmp_path / "reopen.db"
    first = _runtime(db)
    _import(first, doc)
    first._store.close()
    del doc
    reopened = _runtime(db)
    again = _export(reopened)
    assert [o["canonical_identity"] for o in again["objects"]] == FULL
    assert _semantic(again) == _semantic(_export(source))


def test_no_import_event_in_accepted_history(source, receiver):
    doc = _source(source)
    _import(receiver, doc)
    for src in doc["objects"]:
        ident = src["canonical_identity"]
        assert len(receiver.history(ident)) == len(src["history"])
        assert all(r.action != "import" and r.actor_id != "importer" for r in receiver.history(ident))
    records = receiver.interchange_imports()
    assert len(records) == 1 and records[0]["identities"] == FULL
    assert records[0]["importing_actor"] == "importer"


def test_import_does_not_depend_on_later_input_mutation(receiver):
    doc = _reorder_bundle()
    bundle = decode_artifact(json.dumps(doc))
    receiver.import_interchange_bundle(bundle, actor=IMPORTER)
    bundle.objects[0].accepted_state["amount"] = "0.00"
    assert receiver.get_object("PO-4821").data["amount"] == "11800.00"


def test_b_reorder_preserved_exactly(receiver):
    doc = _reorder_bundle()
    _import(receiver, doc)
    rows = receiver.accepted_history("PO-4821")
    assert [r.object_version for r in rows] == [1, 2, 3, 5, 4, 6, 7]
    assert [r.action for r in rows] == [
        "create", "add_relationship", "add_relationship", "update",
        "remove_relationship", "add_relationship", "submit",
    ]
    assert _semantic(_export(receiver, ["PO-4821"], closed=False)) == _reexported(_reorder_bundle())


def test_sparse_tokens_preserved(receiver):
    tokens = ("40", "77", "12", "97", "3", "58", "76")
    _import(receiver, _reorder_bundle(tokens))
    assert [str(r.object_version) for r in receiver.accepted_history("PO-4821")] == list(tokens)
    assert receiver.get_object("PO-4821").version == 76


def test_timestamp_skew_keeps_position_order(receiver):
    stamps = [f"2026-01-0{9 - i}T10:00:00Z" for i in range(7)]  # strictly decreasing
    doc = _reorder_bundle(stamps=stamps)
    _import(receiver, doc)
    rows = receiver.accepted_history("PO-4821")
    assert [r.history_position for r in rows] == list(range(1, 8))
    out = _export(receiver, ["PO-4821"], closed=False, include_timestamps=True)
    got = [e["accepted_at"] for e in out["objects"][0]["history"]]
    assert got == [s.replace("Z", "+00:00") for s in stamps]
    assert [e["position"] for e in out["objects"][0]["history"]] == list(range(1, 8))


def test_absent_timestamps_are_not_invented(receiver):
    _import(receiver, _reorder_bundle())
    out = _export(receiver, ["PO-4821"], closed=False, include_timestamps=True)
    assert all("accepted_at" not in e for e in out["objects"][0]["history"])


def test_non_monotonic_unique_tokens_import_and_re_export_unchanged(receiver):
    # Only token equality is semantic (analysis P6); order comes from position.
    tokens = ("10", "4", "99", "2", "50", "8", "31")
    doc = _reorder_bundle(tokens)
    _import(receiver, doc)
    rows = receiver.accepted_history("PO-4821")
    assert [r.history_position for r in rows] == list(range(1, 8))
    assert [r.object_version for r in rows] == [int(t) for t in tokens]
    out = _export(receiver, ["PO-4821"], closed=False)
    assert [e["resulting_object_version"] for e in out["objects"][0]["history"]] == list(tokens)
    assert _semantic(out) == _reexported(doc)


def test_canonical_token_survives_native_storage_exactly(receiver):
    tokens = ("1", "2", "3", "5", "4", "6", "17")
    _import(receiver, _reorder_bundle(tokens))
    assert receiver.get_object("PO-4821").version == 17
    out = _export(receiver, ["PO-4821"], closed=False)
    assert out["objects"][0]["current_object_version"] == "17"
    assert out["objects"][0]["history"][-1]["resulting_object_version"] == "17"


def test_max_native_token_is_accepted(receiver):
    tokens = ("1", "2", "3", "5", "4", "6", str(2**63 - 1))
    _import(receiver, _reorder_bundle(tokens))
    assert receiver.get_object("PO-4821").version == 2**63 - 1


# --- refusals, each with zero mutation ---------------------------------------


def test_identity_collision_refuses_whole_bundle(source, receiver):
    assert receiver.execute(
        CreateObject("SUP-17", SUP, {"name": "Local Supplier"}, state="active"), actor=WRITER
    ).decision == Decision.ALLOW
    before = receiver.get_object("SUP-17")
    _refused(receiver, _source(source), IDENTITY_ALREADY_EXISTS)
    assert receiver.get_object("SUP-17") == before
    assert [receiver.get_object(i) for i in ("PO-4821", "CC-42", "CC-43")] == [None, None, None]


def test_governance_mismatch_refuses_then_primary_imports(source, tmp_path):
    doc = _source(source)
    alt = _runtime(tmp_path / "alt.db", CFG_ALT)
    err = _refused(alt, doc, UNRESOLVED_GOVERNANCE_REFERENCE)
    assert err.identity == "PO-4821"
    assert alt.get_object("SUP-17") is None
    primary = _runtime(tmp_path / "primary.db", CFG_PRIMARY)
    _import(primary, doc)
    assert primary.get_object("PO-4821").governance_reference == PO_REF


def test_non_po_bundle_imports_under_alt(source, tmp_path):
    alt = _runtime(tmp_path / "alt.db", CFG_ALT)
    _import(alt, _source(source, ["SUP-17"], closed=True))
    assert alt.get_object("SUP-17") is not None


def test_no_governance_configured_refuses(source, tmp_path):
    _refused(_runtime(tmp_path / "none.db", None), _source(source), UNRESOLVED_GOVERNANCE_REFERENCE)


def test_unsupported_type_refuses(source, tmp_path):
    rt = _runtime(tmp_path / "no-cc.db", {k: v for k, v in CFG_PRIMARY.items() if k[0] != CC},
                  [t for t in TYPES if t["type_identity"] != CC])
    _refused(rt, _source(source), UNSUPPORTED_TYPE)


def test_unsupported_type_version_refuses(receiver):
    doc = _reorder_bundle()
    obj = doc["objects"][0]
    obj["type_version"] = "3"
    for e in obj["history"]:
        e["type_version"] = "3"
    doc["manifest"]["type_bindings"] = [{"type_identity": PO, "type_version": "3"}]
    _refused(receiver, doc, UNSUPPORTED_TYPE_VERSION)


def test_writer_without_import_grant_is_denied(source, receiver):
    _refused(receiver, _source(source), AUTHORIZATION_DENIED, actor=WRITER)


@pytest.mark.parametrize(
    "token",
    ["01", "017", "+1", "1 ", " 1", "1.0", "rev-alpha", "0", "-1", "9223372036854775808", " 7", "1e3"],
)
def test_unrepresentable_token_refused(receiver, token):
    tokens = ("1", "2", "3", "5", "4", "6", token)
    err = _refused(receiver, _reorder_bundle(tokens), UNSUPPORTED_OBJECT_VERSION_REPRESENTATION)
    assert err.identity == "PO-4821"


def test_non_string_token_is_malformed(receiver):
    doc = _reorder_bundle()
    doc["objects"][0]["current_object_version"] = 7
    _refused(receiver, doc, MALFORMED_ARTIFACT)


def test_token_reuse_refused(receiver):
    _refused(receiver, _reorder_bundle(("1", "2", "3", "5", "3", "6", "7")), VERSION_TOKEN_REUSE)


def test_mid_bundle_receiver_failure_leaves_nothing(source, receiver):
    doc = _source(source)
    last = doc["objects"][-1]
    last["history"][-1]["resulting_object_version"] = "rev-alpha"
    last["current_object_version"] = "rev-alpha"
    err = _refused(receiver, doc, UNSUPPORTED_OBJECT_VERSION_REPRESENTATION)
    assert err.identity == doc["objects"][-1]["canonical_identity"]
    assert receiver._store.list_all() == [] and receiver.interchange_imports() == []


def test_failure_inside_the_transaction_rolls_back(source, receiver, monkeypatch):
    doc = _source(source)
    calls = []

    def boom(rel):
        calls.append(rel)
        if len(calls) == 2:
            raise RuntimeError("injected write failure")
        SQLiteStore._insert_restored_relationship(receiver._store, rel)

    monkeypatch.setattr(receiver._store, "_insert_restored_relationship", boom)
    before = receiver._store.dump_tables()
    with pytest.raises(RuntimeError, match="injected"):
        _import(receiver, doc)
    assert len(calls) == 2
    assert receiver._store.dump_tables() == before
    monkeypatch.undo()
    _import(receiver, doc)
    assert _semantic(_export(receiver)) == _semantic(_reexported(doc))


def test_history_snapshot_mismatch_refused(receiver):
    doc = _reorder_bundle()
    doc["objects"][0]["accepted_state"]["amount"] = "1.00"
    _refused(receiver, doc, HISTORY_SNAPSHOT_MISMATCH)


def test_history_inconsistent_removal_refused(receiver):
    doc = _reorder_bundle()
    doc["objects"][0]["history"][4]["relationship"] = _rel("charged_to", "CC-99")
    _refused(receiver, doc, HISTORY_INCONSISTENT)


def test_multiset_history_refused(receiver):
    doc = _reorder_bundle()
    doc["objects"][0]["history"][2]["relationship"] = _rel("supplier", "SUP-17")
    _refused(receiver, doc, MULTISET_HISTORY)


def test_undeclared_lifecycle_transition_refused(receiver):
    doc = _reorder_bundle()
    doc["objects"][0]["history"][6]["post_image"]["lifecycle"] = "approved"
    doc["objects"][0]["accepted_state"]["lifecycle"] = "approved"
    _refused(receiver, doc, HISTORY_NOT_VALID_FOR_TYPE)


def test_invalid_state_refused(receiver):
    doc = _reorder_bundle()
    doc["objects"][0]["accepted_state"]["amount"] = 11800
    _refused(receiver, doc, STATE_NOT_VALID_FOR_TYPE)


def test_sub_microsecond_evidence_refused_not_truncated(receiver):
    stamps = ["2026-01-01T10:00:00.123456789Z"] * 7
    _refused(receiver, _reorder_bundle(stamps=stamps), UNSUPPORTED_SEMANTIC_FEATURE)


def test_missing_actor_evidence_refused(receiver):
    doc = _reorder_bundle()
    del doc["objects"][0]["history"][3]["actor"]
    _refused(receiver, doc, UNSUPPORTED_SEMANTIC_FEATURE)


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda d: d["manifest"].__setitem__("artifact_revision", "go-core-interchange-experiment/3"),
         UNSUPPORTED_ARTIFACT_REVISION),
        (lambda d: d["manifest"].__setitem__("spec_revision", "v0alpha1/draft3"), "UNSUPPORTED_SPEC_REVISION"),
        (lambda d: d["objects"][0].__setitem__("policy", {"allow": "*"}), UNSUPPORTED_SEMANTIC_FEATURE),
        (lambda d: d["objects"][0].__setitem__("edge_ids", []), MALFORMED_ARTIFACT),
        (lambda d: d["manifest"].__setitem__("profiles", ["p"]), UNSUPPORTED_SEMANTIC_FEATURE),
        (lambda d: d["manifest"].__setitem__("history_completeness", "partial"), "HISTORY_INCOMPLETE"),
        (lambda d: d["manifest"].__setitem__("identities", ["PO-4821", "X"]), MALFORMED_ARTIFACT),
    ],
)
def test_artifact_level_refusals(receiver, mutate, code):
    doc = _reorder_bundle()
    mutate(doc)
    _refused(receiver, doc, code)


def test_duplicate_json_member_refused(receiver):
    text = json.dumps(_reorder_bundle())
    text = text.replace('"closed": false', '"closed": false, "closed": true', 1)
    with pytest.raises(ImportRefused) as exc:
        decode_artifact(text)
    assert exc.value.code == MALFORMED_ARTIFACT


def test_historical_actor_gains_no_authority(receiver):
    doc = _reorder_bundle()
    for e in doc["objects"][0]["history"]:
        e["actor"] = "ghost"
    _import(receiver, doc)
    ghost = Actor(id="ghost", type=ActorType.SERVICE, capabilities=[])
    assert receiver.execute(ReadObject("PO-4821"), actor=ghost).decision != Decision.ALLOW
    assert {r.actor_type for r in receiver.accepted_history("PO-4821")} == {"historical"}


def test_import_grant_confers_no_read_or_mutation(receiver):
    _import(receiver, _reorder_bundle())
    before = receiver._store.dump_tables()
    assert receiver.execute(ReadObject("PO-4821"), actor=IMPORTER).decision == Decision.DENY
    for cmd in (
        Command("update", "PO-4821", payload={"set": {"amount": "1.00"}}),
        AddRelationship("PO-4821", "charged_to", "CC-42"),
        RemoveRelationship("PO-4821", "supplier", "SUP-17"),
        TransitionState("PO-4821", "approve"),
    ):
        assert receiver.execute(cmd, actor=IMPORTER).decision == Decision.DENY, cmd.action
    after = receiver._store.dump_tables()
    assert receiver.get_object("PO-4821").version == 7
    assert [r.history_position for r in receiver.accepted_history("PO-4821")] == list(range(1, 8))
    assert {k: v for k, v in after.items() if k != "events"} == {k: v for k, v in before.items() if k != "events"}


def test_interchange_readback_does_not_use_the_timestamp_ordered_replayer(receiver, monkeypatch):
    # The legacy EventReplayer orders by timestamp; export and readback must not reach it.
    from governed_objects.events import replay

    stamps = [f"2026-01-0{9 - i}T10:00:00Z" for i in range(7)]
    doc = _reorder_bundle(stamps=stamps)
    _import(receiver, doc)

    def forbidden(*_a, **_k):
        raise AssertionError("timestamp-ordered replay reached from interchange readback")

    for name in ("reconstruct", "reconstruct_relationships", "_accepted_events"):
        monkeypatch.setattr(replay.EventReplayer, name, forbidden)
    out = _export(receiver, ["PO-4821"], closed=False, include_timestamps=True)
    assert [e["position"] for e in out["objects"][0]["history"]] == list(range(1, 8))
    assert [e["resulting_object_version"] for e in out["objects"][0]["history"]] == [
        "1", "2", "3", "5", "4", "6", "7"]


# --- adapter ------------------------------------------------------------------

