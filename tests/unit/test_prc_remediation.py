# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""EXPERIMENTAL Draft 2.2 Portable Restoration Continuity: export authorization
order, export and restore revisions, and the native PRC declaration.

Every declared capability item is checked against the native behavior it
describes, not only against its declared value.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from governed_objects.domain.command import AddRelationship
from governed_objects.domain.models import Actor, ActorType
from governed_objects.interchange import (
    ARTIFACT_CONTRACTS,
    ARTIFACT_REVISION_V1,
    ARTIFACT_REVISION_V2,
    ARTIFACT_STATUS_V1,
    ARTIFACT_STATUS_V2,
    EXPORT_ARTIFACT_REVISION,
    EXPORT_SPEC_REVISION,
    IMPORT_ARTIFACT_REVISIONS,
    IMPORT_SPEC_REVISIONS,
    SPEC_REVISION,
)
from governed_objects.interchange import prc
from governed_objects.interchange.artifact import UNSUPPORTED_ARTIFACT_REVISION, decode_artifact
from governed_objects.interchange.export import (
    AUTHORIZATION_DENIED,
    EXPORT_READ_DENIED,
    NOT_FOUND,
    ExportRefused,
)
from governed_objects.interchange.governance import TypeScopedGovernance
from governed_objects.interchange.importer import (
    MALFORMED_ARTIFACT,
    NATIVE_TOKEN_GRAMMAR,
    UNSUPPORTED_OBJECT_VERSION_REPRESENTATION,
    ImportRefused,
    UNSUPPORTED_SEMANTIC_FEATURE,
    UNSUPPORTED_SPEC_REVISION,
    UNSUPPORTED_TYPE,
    UNSUPPORTED_TYPE_VERSION,
)
from governed_objects.runtime.runtime import INT64_MAX
from test_interchange_continuity import _run, _update
from test_interchange_export import _seed_unregistered_type_object, CC, CFG_PRIMARY, PO, SUP, TYPES, WRITER, _create_po
from test_interchange_import import (
    FULL,
    V1_IDENTITY,
    V2_IDENTITY,
    _export,
    _import,
    _reexported,
    _refused,
    _reorder_bundle,
    _runtime,
    _semantic,
    _source,
)
from test_interchange_import import source  # noqa: F401  (fixture)

NOBODY = Actor(id="nobody", type=ActorType.SERVICE)
NEAR_MISS_REVISIONS = (
    "v0alpha1/draft2",
    "v0alpha1/draft3",
    "v0alpha1/draft2.1.1",
    "v0alpha1/draft2.1 ",
    "V0ALPHA1/DRAFT2.1",
    "draft2.1",
    "v0alpha1/draft2.20",
    "v0alpha1/draft2.2 ",
    "V0ALPHA1/DRAFT2.2",
    "draft2.2",
)


@pytest.fixture
def receiver(tmp_path):
    return _runtime(tmp_path / "receiver.db")


def _caps(rt) -> dict:
    return rt.prc_declaration()["capabilities"]


def _denied(rt, ids, actor) -> ExportRefused:
    with pytest.raises(ExportRefused) as exc:
        rt.export_interchange_bundle(list(ids), actor=actor, closed=False)
    return exc.value


def _reader_of(rt, identity: str) -> Actor:
    type_def = rt._types.get(rt.get_object(identity).type)
    return Actor(id="type-reader", type=ActorType.SERVICE, capabilities=list(type_def.required_capabilities("read")))


def _provider_bound(rt, identity="PO-4821"):
    obj = rt.get_object(identity)
    binding = {"provider_binding": {"provider_type": "generic", "provider_name": "p", "external_id": "x"}}
    rt._store.save(obj.model_copy(update={"metadata": binding}))


# --- export authorization precedes exportability classification (E10-002) ------




def test_unregistered_type_is_denied_not_classified(tmp_path):
    rt = _runtime(tmp_path / "rt.db")
    _seed_unregistered_type_object(rt)
    for actor in (NOBODY, WRITER):
        err = _denied(rt, ["C-1"], actor)
        assert (err.code, err.message) == (AUTHORIZATION_DENIED, EXPORT_READ_DENIED)




def test_read_authority_for_every_identity_is_checked_before_any_classification(tmp_path):
    rt = _runtime(tmp_path / "rt.db")
    _create_po(rt)
    _create_po(rt, "PO-9")
    _provider_bound(rt, "PO-9")
    _seed_unregistered_type_object(rt)
    # PO-9 is readable but not exportable and precedes the unreadable C-1.
    err = _denied(rt, ["PO-4821", "PO-9", "C-1"], WRITER)
    assert (err.code, err.identity, err.message) == (AUTHORIZATION_DENIED, "C-1", EXPORT_READ_DENIED)


def test_missing_identity_is_not_found(tmp_path):
    rt = _runtime(tmp_path / "rt.db")
    assert _denied(rt, ["PO-404"], NOBODY).code == NOT_FOUND


# --- artifact identity: export writes v2 + Draft 2.2, restore routes v1 and v2 -----


def _identity(doc) -> dict:
    return {k: doc["manifest"].get(k) for k in V2_IDENTITY}


def _refused_at_decode(receiver, doc, code) -> None:
    """Refused by the artifact decoder, before the runtime sees a bundle."""
    with pytest.raises(ImportRefused) as exc:
        decode_artifact(json.dumps(doc))
    assert exc.value.code == code, exc.value
    _refused(receiver, doc, code)
    assert receiver._store.list_all() == [] and receiver.interchange_imports() == []


def test_artifact_contracts_pair_each_revision_with_one_spec_revision():
    assert ARTIFACT_CONTRACTS == {
        "go-core-interchange-experiment/1": (
            "EXPERIMENTAL; NON-NORMATIVE; NOT A GOVERNED OBJECT WIRE STANDARD; NOT A DRAFT 2.1 REQUIREMENT",
            "v0alpha1/draft2.1",
        ),
        "go-core-interchange-experiment/2": (
            "EXPERIMENTAL; NON-NORMATIVE; NOT A GOVERNED OBJECT WIRE STANDARD; NOT A DRAFT 2.2 REQUIREMENT",
            "v0alpha1/draft2.2",
        ),
    }
    assert SPEC_REVISION == "v0alpha1/draft2.1"
    assert IMPORT_ARTIFACT_REVISIONS == (ARTIFACT_REVISION_V1, ARTIFACT_REVISION_V2)
    assert IMPORT_SPEC_REVISIONS == (SPEC_REVISION, EXPORT_SPEC_REVISION)


def test_native_export_is_artifact_v2_with_draft22_content(source):  # noqa: F811
    doc = _export(source)
    assert _identity(doc) == {
        "artifact_status": "EXPERIMENTAL; NON-NORMATIVE; NOT A GOVERNED OBJECT WIRE STANDARD; "
        "NOT A DRAFT 2.2 REQUIREMENT",
        "artifact_revision": "go-core-interchange-experiment/2",
        "spec_revision": "v0alpha1/draft2.2",
    }
    assert EXPORT_ARTIFACT_REVISION == ARTIFACT_REVISION_V2
    assert ARTIFACT_STATUS_V1 not in json.dumps(doc)


def test_restore_accepts_v1_draft21(source, receiver):  # noqa: F811
    doc = _source(source)
    assert _identity(doc) == V1_IDENTITY
    _import(receiver, doc)
    assert sorted(o.id for o in receiver._store.list_all()) == sorted(FULL)


def test_restore_accepts_v2_draft22(source, receiver):  # noqa: F811
    doc = _export(source)
    assert _identity(doc) == V2_IDENTITY
    _import(receiver, doc)
    assert sorted(o.id for o in receiver._store.list_all()) == sorted(FULL)


@pytest.mark.parametrize(
    "identity, code",
    [
        ({**V1_IDENTITY, "spec_revision": EXPORT_SPEC_REVISION}, UNSUPPORTED_SPEC_REVISION),
        ({**V2_IDENTITY, "spec_revision": SPEC_REVISION}, UNSUPPORTED_SPEC_REVISION),
        ({**V1_IDENTITY, "spec_revision": None}, UNSUPPORTED_SPEC_REVISION),
        ({**V2_IDENTITY, "spec_revision": None}, UNSUPPORTED_SPEC_REVISION),
        ({**V2_IDENTITY, "spec_revision": [EXPORT_SPEC_REVISION]}, MALFORMED_ARTIFACT),
        ({**V2_IDENTITY, "artifact_status": ARTIFACT_STATUS_V1}, MALFORMED_ARTIFACT),
        ({**V1_IDENTITY, "artifact_status": ARTIFACT_STATUS_V2}, MALFORMED_ARTIFACT),
        ({**V2_IDENTITY, "artifact_status": ARTIFACT_STATUS_V2.lower()}, MALFORMED_ARTIFACT),
        ({**V2_IDENTITY, "artifact_revision": None}, UNSUPPORTED_ARTIFACT_REVISION),
        ({**V2_IDENTITY, "artifact_revision": "go-core-interchange-experiment/3"}, UNSUPPORTED_ARTIFACT_REVISION),
        ({**V2_IDENTITY, "artifact_revision": "go-core-interchange-experiment/2.0"}, UNSUPPORTED_ARTIFACT_REVISION),
        ({**V2_IDENTITY, "artifact_revision": "go-core-interchange-experiment/02"}, UNSUPPORTED_ARTIFACT_REVISION),
        ({**V2_IDENTITY, "artifact_revision": ARTIFACT_REVISION_V2 + " "}, UNSUPPORTED_ARTIFACT_REVISION),
        ({**V2_IDENTITY, "artifact_revision": [ARTIFACT_REVISION_V2]}, UNSUPPORTED_ARTIFACT_REVISION),
    ],
    ids=[
        "v1+D22", "v2+D21", "v1-no-spec", "v2-no-spec", "v2-spec-list", "v2-v1-marker", "v1-v2-marker",
        "v2-marker-recased", "missing-artifact", "unknown-artifact", "artifact-2.0", "artifact-02",
        "artifact-trailing-space", "artifact-list",
    ],
)
def test_invalid_artifact_identities_are_refused_before_restoration(source, receiver, identity, code):  # noqa: F811
    doc = _export(source)
    for name, value in identity.items():
        if value is None:
            doc["manifest"].pop(name)
        else:
            doc["manifest"][name] = value
    _refused_at_decode(receiver, doc, code)


@pytest.mark.parametrize("identity", [V1_IDENTITY, V2_IDENTITY], ids=["v1", "v2"])
@pytest.mark.parametrize("revision", NEAR_MISS_REVISIONS)
def test_other_revisions_are_refused_with_nothing_restored(source, receiver, identity, revision):  # noqa: F811
    doc = _export(source)
    doc["manifest"].update({**identity, "spec_revision": revision})
    _refused_at_decode(receiver, doc, UNSUPPORTED_SPEC_REVISION)


def test_v2_refuses_a_replay_baseline_start_it_cannot_carry(source, receiver):  # noqa: F811
    doc = _export(source)
    doc["manifest"]["history_completeness"] = "complete-from-replay-baseline"
    _refused_at_decode(receiver, doc, UNSUPPORTED_SEMANTIC_FEATURE)
    v1 = _source(source)
    v1["manifest"]["history_completeness"] = "complete-from-replay-baseline"
    _refused(receiver, v1, "HISTORY_INCOMPLETE")


# --- Python Draft 2.2 self-import through artifact v2 ---------------------------


def test_self_import_through_v2_restores_everything_and_continues(source, receiver):  # noqa: F811
    doc = _export(source)
    assert _identity(doc) == V2_IDENTITY
    assert _import(receiver, doc).identities == tuple(FULL)
    for src in doc["objects"]:
        ident = src["canonical_identity"]
        obj, orig = receiver.get_object(ident), source.get_object(ident)
        assert (obj.id, obj.type, obj.type_version) == (ident, src["type_identity"], src["type_version"])
        assert (obj.type, obj.type_version, obj.governance_reference) == (
            orig.type, orig.type_version, orig.governance_reference
        )
        assert (obj.data, obj.state, obj.version) == (orig.data, orig.state, orig.version)
        assert str(obj.version) == src["current_object_version"]
        rels = sorted((r.relation, r.target_id) for r in receiver.canonical_relationships(ident))
        assert rels == sorted((r.relation, r.target_id) for r in source.canonical_relationships(ident))
        rows, orig_rows = receiver.accepted_history(ident), source.accepted_history(ident)
        assert [(r.history_position, r.object_version, r.actor_id, r.action) for r in rows] == [
            (r.history_position, r.object_version, r.actor_id, r.action) for r in orig_rows
        ]
    assert _semantic(_export(receiver)) == _semantic(doc)
    assert _run(receiver, _update("12000.00"), expected=7).object_version == 8
    rows = receiver.accepted_history("PO-4821")
    assert [r.history_position for r in rows] == list(range(1, len(rows) + 1))
    assert rows[-1].object_version == 8


# --- PRC-CLAIM-001 / PRC-HANDOFF-001 ------------------------------------------


def test_declaration_is_the_native_module_view(receiver):
    decl = receiver.prc_declaration()
    assert decl == prc.prc_declaration(receiver._types)
    assert decl["profile"] == prc.PRC_PROFILE
    assert decl["roles"] == ["exporter", "restorer"]
    assert decl["export_spec_revision"] == EXPORT_SPEC_REVISION
    assert set(decl["capabilities"]) == {
        "spec_revisions", "type_bindings", "object_version_forms", "baseline_relationships",
        "time_precision", "required_provenance", "evidence_level", "provenance_not_retained",
        "all_or_nothing",
    }


def test_handoff_is_declared_as_an_unenforced_assumption(receiver):
    handoff = receiver.prc_declaration()["handoff"]
    assert handoff["model"] == "operator-procedure"
    assert handoff["enforced_by_runtime"] is False and handoff["verified_by_runtime"] is False
    assert "source accepts no canonical change" in handoff["declaration"]


# --- PRC-CAP-001: each capability item against native behavior -----------------


def test_declared_spec_revisions_are_the_restorable_ones(source, tmp_path):  # noqa: F811
    declared = _caps(_runtime(tmp_path / "decl.db"))["spec_revisions"]
    assert declared == ["v0alpha1/draft2.1", "v0alpha1/draft2.2"] == list(IMPORT_SPEC_REVISIONS)
    for revision in declared:
        (artifact,) = [a for a, (_, spec) in ARTIFACT_CONTRACTS.items() if spec == revision]
        doc = _export(source)
        doc["manifest"].update(artifact_revision=artifact, artifact_status=ARTIFACT_CONTRACTS[artifact][0],
                               spec_revision=revision)
        receiver = _runtime(tmp_path / f"restore-{artifact[-1]}.db")
        _import(receiver, doc)
        assert sorted(o.id for o in receiver._store.list_all()) == sorted(FULL)


def test_declared_type_bindings_are_exactly_the_restorable_ones(source, tmp_path):  # noqa: F811
    receiver = _runtime(tmp_path / "receiver.db")
    assert _caps(receiver)["type_bindings"] == sorted([f"{PO}@2", f"{SUP}@1", f"{CC}@1"])
    _import(receiver, _source(source))
    bumped = _reorder_bundle()
    bumped["manifest"]["type_bindings"] = [{"type_identity": PO, "type_version": "3"}]
    bumped["objects"][0]["type_version"] = "3"
    for e in bumped["objects"][0]["history"]:
        e["type_version"] = "3"
    _refused(_runtime(tmp_path / "r2.db"), bumped, UNSUPPORTED_TYPE_VERSION)
    po_only = _runtime(tmp_path / "po.db", types=[t for t in TYPES if t["type_identity"] == PO])
    assert _caps(po_only)["type_bindings"] == [f"{PO}@2"]
    _refused(po_only, _source(source), UNSUPPORTED_TYPE)




def test_declared_token_forms_match_the_importer(tmp_path):
    assert _caps(_runtime(tmp_path / "a.db"))["object_version_forms"] == NATIVE_TOKEN_GRAMMAR
    tokens = ("1", "2", "3", "5", "4", "6", str(INT64_MAX))
    _import(_runtime(tmp_path / "ok.db"), _reorder_bundle(tokens))
    for i, bad in enumerate(("0", "01", str(INT64_MAX + 1), "+7", "rev-7")):
        doc = _reorder_bundle(("1", "2", "3", "5", "4", "6", bad))
        _refused(_runtime(tmp_path / f"bad{i}.db"), doc, UNSUPPORTED_OBJECT_VERSION_REPRESENTATION)


def test_declared_baseline_relationships_are_restored(receiver):
    assert _caps(receiver)["baseline_relationships"] is True
    _import(receiver, _reorder_bundle())
    out = _export(receiver, ["PO-4821"], closed=False)["objects"][0]
    assert out["relationships"] == _reorder_bundle()["objects"][0]["relationships"]


def test_declared_time_precision_is_exact_to_the_microsecond(tmp_path):
    assert _caps(_runtime(tmp_path / "a.db"))["time_precision"] == "microsecond"
    stamps = [f"2026-01-01T10:00:0{i}.12345{i}Z" for i in range(7)]
    rt = _runtime(tmp_path / "us.db")
    _import(rt, _reorder_bundle(stamps=stamps))
    got = [e["accepted_at"] for e in _export(rt, ["PO-4821"], closed=False, include_timestamps=True)["objects"][0]["history"]]
    assert [datetime.fromisoformat(g) for g in got] == [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps]
    finer = list(stamps)
    finer[3] = "2026-01-01T10:00:03.1234567Z"
    _refused(_runtime(tmp_path / "ns.db"), _reorder_bundle(stamps=finer), UNSUPPORTED_SEMANTIC_FEATURE)


def test_declared_required_provenance_is_enforced(receiver):
    assert _caps(receiver)["required_provenance"] == ["actor"]
    doc = _reorder_bundle()
    del doc["objects"][0]["history"][2]["actor"]
    _refused(receiver, doc, UNSUPPORTED_SEMANTIC_FEATURE)


def test_evidence_level_is_effect_equivalent_and_nothing_is_invented(receiver):
    caps = _caps(receiver)
    assert caps["evidence_level"] == prc.EFFECT_EQUIVALENT
    assert "source_event_id" in caps["provenance_not_retained"]
    doc = _reorder_bundle()
    history = doc["objects"][0]["history"]
    history[0]["source_event_id"] = "evt-1"
    history[3]["action"] = "legacy-edit"  # not the native action encoding a state_change
    del history[6]["action"]
    history[1]["relationship"] = {**history[1]["relationship"], "target_type": SUP, "metadata": {"k": "v"}}
    _import(receiver, doc)
    out = _export(receiver, ["PO-4821"], closed=False)["objects"][0]["history"]
    assert all("source_event_id" not in e for e in out)
    assert "action" not in out[3] and "action" not in out[6]
    assert out[1]["relationship"] == {"source": "PO-4821", "type": "supplier", "target": "SUP-17"}
    kept = [0, 1, 2, 4, 5]
    assert [out[i]["action"] for i in kept] == [history[i]["action"] for i in kept]
    assert [e["actor"] for e in out] == [e["actor"] for e in history]


def test_all_or_nothing_is_request_wide(source, receiver):  # noqa: F811
    assert _caps(receiver)["all_or_nothing"] is True
    doc = _source(source)
    doc["objects"][-1]["history"][0]["actor"] = ""
    _refused(receiver, doc, UNSUPPORTED_SEMANTIC_FEATURE)
    assert receiver._store.list_all() == [] and receiver.interchange_imports() == []


# --- re-export continuity after a restored Draft 2.1 lineage (C9-001) -----------


def test_re_export_keeps_restored_lineage_under_the_export_claim(receiver):
    tokens = ("40", "77", "12", "97", "3", "58", "76")
    doc = _reorder_bundle(tokens)
    _import(receiver, doc)
    out = _semantic(_export(receiver, ["PO-4821"], closed=False))
    assert out == _reexported(doc)
    assert out["manifest"]["spec_revision"] == EXPORT_SPEC_REVISION


def test_re_export_after_continued_operation_appends_without_rewriting_history(receiver):
    tokens = ("40", "77", "12", "97", "3", "58", "76")
    doc = _reorder_bundle(tokens)
    _import(receiver, doc)
    _run(receiver, _update("11000.00"), expected=76)
    _run(receiver, AddRelationship("PO-4821", "references", "SUP-17"), expected=98)
    out = _semantic(_export(receiver, ["PO-4821"], closed=False))
    assert out["manifest"]["spec_revision"] == EXPORT_SPEC_REVISION
    obj, restored = out["objects"][0], doc["objects"][0]
    assert obj["canonical_identity"] == restored["canonical_identity"]
    assert obj["history"][:7] == restored["history"]
    appended = obj["history"][7:]
    assert [(e["position"], e["effect"], e["resulting_object_version"]) for e in appended] == [
        (8, "state_change", "98"),
        (9, "relationship_add", "99"),
    ]
    assert obj["current_object_version"] == "99"
    added = {"source": "PO-4821", "type": "references", "target": "SUP-17"}
    assert added in obj["relationships"]
    assert [r for r in obj["relationships"] if r != added] == restored["relationships"]
