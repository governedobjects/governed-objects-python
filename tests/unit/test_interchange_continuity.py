# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""EXPERIMENTAL: behavioral continuity after import.

An imported object is governed and mutated through the ordinary command path.
Version allocation is implementation-defined: one above the highest version
ever issued for the identity, imported tokens included.
"""

from __future__ import annotations

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
    RemoveRelationship,
    TransitionState,
)
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType
from governed_objects.observations.models import ExternalObservation, FieldChange
from governed_objects.runtime.runtime import INT64_MAX
from test_interchange_export import CFG_PRIMARY, PO, PO_STATE, READER, RESTRICTED, TYPES, WRITER
from test_interchange_import import (
    IMPORTER,
    _export,
    _import,
    _reexported,
    _reorder_bundle,
    _runtime,
    _semantic,
)

SPARSE = ("40", "77", "12", "97", "3", "58", "76")
REORDER = ("1", "2", "3", "5", "4", "6", "7")


@pytest.fixture
def receiver(tmp_path):
    return _runtime(tmp_path / "receiver.db")


def _ctx(version: int) -> CommandContext:
    return CommandContext(expected_version=version)


def _run(rt, cmd, actor=WRITER, expected=None):
    return rt._finalize(rt.execute(cmd, actor=actor, command_context=None if expected is None else _ctx(expected)))


def _update(amount="1.00"):
    return Command("update", "PO-4821", payload={"set": {"amount": amount}})


def _snapshot(rt) -> dict:
    """Everything a refused, conflicting or no-op command must leave untouched."""
    tables = rt._store.dump_tables()
    return {
        "objects": tables["objects"],
        "relationships": tables["relationships"],
        "imports": tables["interchange_imports"],
        "history": [e.model_dump() for e in rt.accepted_history("PO-4821")],
    }


def _tokens(rt) -> list[int]:
    return [e.object_version for e in rt.accepted_history("PO-4821")]


def _high_water(rt) -> int:
    return rt._store.version_high_water("PO-4821")


# --- allocation ---------------------------------------------------------------


def test_native_sequence_is_unchanged(receiver):
    receiver.execute(CreateObject("CC-42", "example.procurement/CostCenter", {"code": "42", "name": "x"}), actor=WRITER)
    assert _run(receiver, CreateObject("PO-4821", PO, dict(PO_STATE), state="draft")).object_version == 1
    assert _run(receiver, _update(), expected=1).object_version == 2
    assert _run(receiver, AddRelationship("PO-4821", "charged_to", "CC-42"), expected=2).object_version == 3
    assert _run(receiver, RemoveRelationship("PO-4821", "charged_to", "CC-42"), expected=3).object_version == 4
    assert _run(receiver, TransitionState("PO-4821", "submit"), expected=4).object_version == 5
    assert _tokens(receiver) == [1, 2, 3, 4, 5]
    assert _high_water(receiver) == 5


def test_import_initializes_high_water_from_every_token_without_changing_versions(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    assert _tokens(receiver) == [int(t) for t in SPARSE]
    assert receiver.get_object("PO-4821").version == 76
    assert _high_water(receiver) == 97


def test_import_high_water_when_current_is_the_maximum(receiver):
    _import(receiver, _reorder_bundle(REORDER))
    assert _high_water(receiver) == 7
    assert _run(receiver, _update(), expected=7).object_version == 8


def test_b_sparse_repeated_mutations_never_reuse_a_token(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    prior = {int(t) for t in SPARSE}
    first = _run(receiver, _update("1.00"), expected=76)
    assert first.outcome_category == "ACCEPTED"
    assert first.object_version == 98 and first.object_version not in prior
    prior.add(first.object_version)
    second = _run(receiver, AddRelationship("PO-4821", "references", "SUP-17"), expected=98)
    assert second.outcome_category == "ACCEPTED"
    assert second.object_version == 99 and second.object_version not in prior
    rows = receiver.accepted_history("PO-4821")
    assert [r.history_position for r in rows] == list(range(1, 10))
    assert _tokens(receiver) == [int(t) for t in SPARSE] + [98, 99]
    out = _export(receiver, ["PO-4821"], closed=False)["objects"][0]
    assert out["current_object_version"] == "99"
    assert [e["resulting_object_version"] for e in out["history"]] == list(SPARSE) + ["98", "99"]


def test_current_plus_one_would_have_collided(receiver):
    # The pre-Step-2C rule (current + 1) would issue 77, the token at position 2.
    _import(receiver, _reorder_bundle(SPARSE))
    assert 76 + 1 in {int(t) for t in SPARSE}
    assert _run(receiver, _update(), expected=76).object_version != 77


def test_high_water_survives_restart(tmp_path):
    db = tmp_path / "receiver.db"
    _import(_runtime(db), _reorder_bundle(SPARSE))
    reopened = _runtime(db)
    assert _run(reopened, _update(), expected=76).object_version == 98


def test_legacy_row_without_high_water_allocates_from_version(tmp_path):
    db = tmp_path / "rt.db"
    rt = _runtime(db)
    _run(rt, CreateObject("PO-4821", PO, dict(PO_STATE), state="draft"))
    _run(rt, _update(), expected=1)
    rt._store.close()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE objects SET version_high_water = NULL")
    rt = _runtime(db)
    assert _run(rt, _update("2.00"), expected=2).object_version == 3


def _observation(**kwargs) -> ExternalObservation:
    return ExternalObservation(object_id="PO-4821", provider_name="p", external_id="x", **kwargs)


def _bootstrap() -> ExternalObservation:
    return _observation(
        provider_event_id="bootstrap", raw_metadata={"bootstrap": True}, changes={"amount": FieldChange(observed="9.99")}
    )


def _enable_bootstrap_auto_accept(rt) -> None:
    """Explicit test-local authority; the bootstrap marker confers none."""
    obj = rt._store.get("PO-4821")
    definition = rt._types.get(obj.type).model_copy(deep=True)
    authority = definition.extra.setdefault("authority", {})
    authority.setdefault("fields", {})["amount"] = "PROVIDER_AUTO_ACCEPT"
    rt._types.register(definition)


def _review_case(rt) -> str:
    result = rt.ingest_observation(_observation(changes={"amount": FieldChange(observed="9.99")}))
    assert result.decision == "REQUIRES_REVIEW"
    return result.review_case_id


# Draft 2.2 (D5): detach changes operational metadata only. It previously
# allocated a token (98 here) with no accepted revision; it now issues none, and
# the next accepted change still allocates from the high-water mark.
def test_detach_issues_no_object_version(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    before = _tokens(receiver)
    assert receiver.detach_provider("PO-4821").version == 76
    assert _tokens(receiver) == before
    accepted = _run(receiver, _update(), expected=76)
    assert (accepted.outcome_category, accepted.object_version) == ("ACCEPTED", 98)


def test_bootstrap_observation_allocates_from_high_water(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    _enable_bootstrap_auto_accept(receiver)
    result = receiver.ingest_observation(_bootstrap())
    assert (result.decision, result.object_version) == ("ACCEPT", 98)
    assert receiver.get_object("PO-4821").version == 98


def test_accepted_review_allocates_from_high_water(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    result = receiver.resolve_review(_review_case(receiver), resolution="ACCEPT_EXTERNAL", resolved_by="u")
    assert (result.decision, result.object_version) == ("ACCEPT", 98)


def test_non_command_paths_refuse_at_max_with_zero_change(receiver):
    _import(receiver, _reorder_bundle(REORDER[:-1] + (str(INT64_MAX),)))
    case_id = _review_case(receiver)
    _enable_bootstrap_auto_accept(receiver)
    before = _snapshot(receiver)
    bootstrap = receiver.ingest_observation(_bootstrap())
    assert (bootstrap.decision, bootstrap.reason) == ("REJECT", "OBJECT_VERSION_SPACE_EXHAUSTED")
    review = receiver.resolve_review(case_id, resolution="ACCEPT_EXTERNAL", resolved_by="u")
    assert (review.decision, review.reason) == ("REJECT", "OBJECT_VERSION_SPACE_EXHAUSTED")
    assert _snapshot(receiver) == before


# Draft 2.2 (D5): detach needs no token, so it succeeds at the ceiling instead of
# raising ObjectVersionSpaceExhausted, and V remains the current Object Version.
def test_detach_succeeds_at_max_without_changing_version(receiver):
    _import(receiver, _reorder_bundle(REORDER[:-1] + (str(INT64_MAX),)))
    history = _tokens(receiver)
    assert receiver.detach_provider("PO-4821").version == INT64_MAX
    assert _tokens(receiver) == history
    assert receiver._store.get("PO-4821").version == INT64_MAX
    refused = _run(receiver, _update(), expected=INT64_MAX)
    assert refused.outcome_category == "REFUSED"
    assert refused.conflict_reason is None


def test_high_water_is_not_exported(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    _run(receiver, _update(), expected=76)
    text = repr(_export(receiver, ["PO-4821"], closed=False, include_timestamps=True))
    assert "high_water" not in text and "97" in text


# --- exhaustion ---------------------------------------------------------------


def test_near_max_allows_exactly_one_mutation_then_refuses(receiver):
    tokens = REORDER[:-1] + (str(INT64_MAX - 1),)
    _import(receiver, _reorder_bundle(tokens))
    last = _run(receiver, _update(), expected=INT64_MAX - 1)
    assert last.outcome_category == "ACCEPTED" and last.object_version == INT64_MAX
    before = _snapshot(receiver)
    refused = _run(receiver, _update("2.00"), expected=INT64_MAX)
    assert refused.outcome_category == "REFUSED"
    assert refused.deny_reason == "OBJECT_VERSION_SPACE_EXHAUSTED"
    assert _snapshot(receiver) == before


@pytest.mark.parametrize("cmd", [
    _update(),
    TransitionState("PO-4821", "approve"),
    AddRelationship("PO-4821", "references", "SUP-17"),
    RemoveRelationship("PO-4821", "supplier", "SUP-17"),
], ids=["state", "lifecycle", "relationship_add", "relationship_remove"])
def test_max_current_version_refuses_every_mutation_with_zero_change(receiver, cmd):
    _import(receiver, _reorder_bundle(REORDER[:-1] + (str(INT64_MAX),)))
    before = _snapshot(receiver)
    result = _run(receiver, cmd, expected=INT64_MAX)
    assert (result.outcome_category, result.deny_reason) == ("REFUSED", "OBJECT_VERSION_SPACE_EXHAUSTED")
    assert _snapshot(receiver) == before
    assert _high_water(receiver) == INT64_MAX


@pytest.mark.parametrize("cmd", [
    _update(),
    TransitionState("PO-4821", "approve"),
    AddRelationship("PO-4821", "references", "SUP-17"),
    RemoveRelationship("PO-4821", "supplier", "SUP-17"),
], ids=["state", "lifecycle", "relationship_add", "relationship_remove"])
def test_max_historical_token_exhausts_even_when_current_is_small(receiver, cmd):
    tokens = (str(INT64_MAX),) + REORDER[1:]
    _import(receiver, _reorder_bundle(tokens))
    before = _snapshot(receiver)
    result = _run(receiver, cmd, expected=7)
    assert (result.outcome_category, result.deny_reason) == ("REFUSED", "OBJECT_VERSION_SPACE_EXHAUSTED")
    assert _snapshot(receiver) == before
    assert _high_water(receiver) == INT64_MAX


def test_stale_token_at_max_conflicts_rather_than_refuses(receiver):
    _import(receiver, _reorder_bundle(REORDER[:-1] + (str(INT64_MAX),)))
    before = _snapshot(receiver)
    assert _run(receiver, _update(), expected=6).outcome_category == "CONFLICT"
    assert _snapshot(receiver) == before


def test_no_op_needs_no_version_and_is_accepted_at_max(receiver):
    _import(receiver, _reorder_bundle(REORDER[:-1] + (str(INT64_MAX),)))
    before = _snapshot(receiver)
    result = _run(receiver, AddRelationship("PO-4821", "supplier", "SUP-17"), expected=INT64_MAX)
    assert result.outcome_category == "ACCEPTED" and result.object_version == INT64_MAX
    assert _snapshot(receiver) == before


# --- expected_version continuity ----------------------------------------------


def test_every_historical_and_unknown_token_conflicts_without_change(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    before = _snapshot(receiver)
    stale = [int(t) for t in SPARSE[:-1]]
    unknown = [1, 98, 1000, INT64_MAX]
    reasons = set()
    for token in stale + unknown:
        for cmd in (_update(), AddRelationship("PO-4821", "references", "SUP-17"),
                    RemoveRelationship("PO-4821", "supplier", "SUP-17"), TransitionState("PO-4821", "approve")):
            result = _run(receiver, cmd, expected=token)
            assert result.outcome_category == "CONFLICT", (token, cmd.action)
            reasons.add((result.conflict_reason, result.actual_version))
    assert reasons == {("VERSION_MISMATCH", 76)}, "stale and unknown tokens must be indistinguishable"
    assert _snapshot(receiver) == before
    assert _high_water(receiver) == 97
    assert _run(receiver, _update(), expected=76).object_version == 98


def test_pre_transfer_command_is_accepted_once_then_conflicts(receiver):
    # A consequence of the provisional exact-preservation model, not a Draft 2.1
    # portability requirement.
    _import(receiver, _reorder_bundle(SPARSE))
    command = _update("9.99")
    assert _run(receiver, command, expected=76).outcome_category == "ACCEPTED"
    assert _run(receiver, command, expected=76).outcome_category == "CONFLICT"


# --- mutations ----------------------------------------------------------------


def test_post_import_mutations_append_native_entries_at_n_plus_1(receiver):
    _import(receiver, _reorder_bundle())
    imported = [e.model_dump() for e in receiver.accepted_history("PO-4821")]
    provenance = receiver._store.list_imports()
    steps = [
        (_update("11000.00"), 8),
        (AddRelationship("PO-4821", "references", "SUP-17"), 9),
        (RemoveRelationship("PO-4821", "references", "SUP-17"), 10),
        (TransitionState("PO-4821", "approve"), 11),
    ]
    for cmd, token in steps:
        result = _run(receiver, cmd, expected=token - 1)
        assert result.outcome_category == "ACCEPTED" and result.object_version == token
    rows = receiver.accepted_history("PO-4821")
    assert [e.model_dump() for e in rows[:7]] == imported
    assert [r.history_position for r in rows] == list(range(1, 12))
    assert [r.object_version for r in rows] == [1, 2, 3, 5, 4, 6, 7, 8, 9, 10, 11]
    for r in rows[7:]:
        assert (r.actor_id, r.actor_type) == ("writer", WRITER.type.value)
        assert "import" not in repr(r.details).lower()
    obj = receiver.get_object("PO-4821")
    assert (obj.type, obj.type_version, obj.state) == (PO, "2", "approved")
    assert obj.governance_reference == _reorder_bundle()["objects"][0]["governance_reference"]
    assert receiver._store.list_imports() == provenance


def test_new_entries_match_a_native_object_entry_shape(tmp_path):
    native = _runtime(tmp_path / "native.db")
    _run(native, CreateObject("PO-4821", PO, dict(PO_STATE), state="draft"))
    _run(native, _update(), expected=1)
    imported = _runtime(tmp_path / "imported.db")
    _import(imported, _reorder_bundle())
    _run(imported, _update(), expected=7)
    a, b = native.accepted_history("PO-4821")[-1], imported.accepted_history("PO-4821")[-1]
    skip = {"event_id", "timestamp", "object_version", "history_position", "details", "previous_state", "new_state"}
    assert {k: v for k, v in a.model_dump().items() if k not in skip} == {
        k: v for k, v in b.model_dump().items() if k not in skip}
    assert a.details.keys() == b.details.keys()


def test_no_ops_leave_version_and_history_unchanged(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    before = _snapshot(receiver)
    dup = _run(receiver, AddRelationship("PO-4821", "supplier", "SUP-17"), expected=76)
    absent = _run(receiver, RemoveRelationship("PO-4821", "charged_to", "CC-42"), expected=76)
    for result in (dup, absent):
        assert result.outcome_category == "ACCEPTED" and result.object_version == 76
    assert _snapshot(receiver) == before


def test_invalid_mutation_is_semantically_invalid_with_no_change(receiver):
    _import(receiver, _reorder_bundle(SPARSE))
    before = _snapshot(receiver)
    result = _run(receiver, TransitionState("PO-4821", "submit"), expected=76)
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.deny_reason == "LIFECYCLE_INVALID"
    assert _snapshot(receiver) == before


# --- authorization ------------------------------------------------------------


def test_authorization_is_local_and_ignores_import_and_history(receiver):
    doc = _reorder_bundle(SPARSE)
    for e in doc["objects"][0]["history"]:
        e["actor"] = "ghost"
    _import(receiver, doc)
    ghost = Actor(id="ghost", type=ActorType.SERVICE, capabilities=[])
    before = _snapshot(receiver)
    mutations = [_update(), TransitionState("PO-4821", "approve"),
                 AddRelationship("PO-4821", "references", "SUP-17"),
                 RemoveRelationship("PO-4821", "supplier", "SUP-17")]
    for actor in (IMPORTER, READER, ghost):
        for cmd in mutations:
            result = _run(receiver, cmd, actor=actor, expected=76)
            assert result.outcome_category == "AUTHORIZATION_DENIED", (actor.id, cmd.action)
    restricted = _run(receiver, TransitionState("PO-4821", "approve"), actor=RESTRICTED, expected=76)
    assert restricted.outcome_category == "AUTHORIZATION_DENIED"
    assert _snapshot(receiver) == before
    assert _run(receiver, AddRelationship("PO-4821", "references", "SUP-17"),
                actor=RESTRICTED, expected=76).object_version == 98
    assert _run(receiver, TransitionState("PO-4821", "approve"), expected=98).object_version == 99


# --- ordering -----------------------------------------------------------------


def test_b_reorder_history_is_not_normalized_by_mutation(receiver):
    _import(receiver, _reorder_bundle())
    _run(receiver, _update(), expected=7)
    rows = receiver.accepted_history("PO-4821")
    assert [r.object_version for r in rows] == [1, 2, 3, 5, 4, 6, 7, 8]
    assert [r.action for r in rows][:7] == [
        "create", "add_relationship", "add_relationship", "update",
        "remove_relationship", "add_relationship", "submit"]
    assert rows[-1].history_position == 8


@pytest.mark.parametrize("stamps", [
    ["2026-01-05T10:00:00Z"] * 7,
    [f"2099-01-0{i + 1}T10:00:00Z" for i in range(7)],
    [f"2026-01-0{9 - i}T10:00:00Z" for i in range(7)],
], ids=["equal", "future", "reversed"])
def test_timestamp_skew_does_not_move_the_new_entry(receiver, stamps):
    _import(receiver, _reorder_bundle(stamps=stamps))
    _run(receiver, _update(), expected=7)
    rows = receiver.accepted_history("PO-4821")
    assert [r.history_position for r in rows] == list(range(1, 9))
    assert rows[-1].object_version == 8
    out = _export(receiver, ["PO-4821"], closed=False, include_timestamps=True)["objects"][0]["history"]
    assert [e["accepted_at"] for e in out[:7]] == [s.replace("Z", "+00:00") for s in stamps]


# --- re-export ----------------------------------------------------------------


def test_re_export_differs_only_by_the_mutation_and_imports_elsewhere(receiver, tmp_path):
    doc = _reorder_bundle(SPARSE)
    _import(receiver, doc)
    _run(receiver, _update("11000.00"), expected=76)
    out = _semantic(_export(receiver, ["PO-4821"], closed=False))
    expected = _reexported(_reorder_bundle(SPARSE))
    obj = expected["objects"][0]
    obj["accepted_state"] = {**obj["accepted_state"], "amount": "11000.00"}
    obj["current_object_version"] = "98"
    obj["history"].append({
        "position": 8, "effect": "state_change", "type_identity": PO, "type_version": "2",
        "resulting_object_version": "98", "action": "update", "actor": "writer",
        "post_image": dict(obj["accepted_state"]),
    })
    assert out == expected
    third = _runtime(tmp_path / "third.db")
    _import(third, out)
    assert _run(third, _update("1.00"), expected=98).object_version == 99
