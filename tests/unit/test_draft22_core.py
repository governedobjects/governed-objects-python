# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Draft 2.2 Core remediation: native Python regression tests.

Each test names the Draft 2.2 conformance-plan scenario
(CS-*) or the Python defect (D1-D10) it exercises. These are native unit
tests of this runtime; they are not the conformance harness and do not claim
Draft 2.2 conformance.
"""

from __future__ import annotations
from tests.generic_bootstrap import generic_dependencies, synthetic_extensions

import pytest
import yaml

from governed_objects.adapters.context import AdapterExecuteResult
from governed_objects.domain.command import (
    AddRelationship,
    Command,
    CreateObject,
    ReadObject,
    RemoveRelationship,
    TransitionState,
)
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType, Decision
from governed_objects.events.lineage import lineage_violations
from governed_objects.events.replay import LegacyHistoryRefused
from tests.fixtures.interchange import GRANT_CAPABILITIES
from governed_objects.observations.models import FieldChange
from governed_objects.persistence.sqlite import INT64_MAX, SQLiteStore
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.registry import TypeRegistry
from test_draft2_history_type_version import HistoryWorkItemAdapter
from test_interchange_continuity import SPARSE, _bootstrap, _enable_bootstrap_auto_accept, _observation, _review_case
from test_interchange_export import CC, PO, PO_STATE, SUP, WRITER, _runtime
from test_interchange_import import _import, _reorder_bundle


def _actor(name: str, *grants: str) -> Actor:
    caps = [c for g in grants for c in GRANT_CAPABILITIES[g]]
    return Actor(id=name, type=ActorType.SERVICE, capabilities=caps)


MUTATE = ("state and lifecycle actions", "relationship add", "relationship remove")
OUTSIDER = _actor("outsider")
READER = _actor("reader", "read")
WRITER_NO_READ = _actor("writer-no-read", *MUTATE)


def _ctx(version: int | None, **kw) -> CommandContext | None:
    if version is None and not kw:
        return None
    return CommandContext(expected_version=version, **kw)


def _canonical(rt: GovernedObjectRuntime, oid: str) -> dict:
    """Everything a non-accepted or no-op outcome must leave unchanged."""
    obj = rt._store.get(oid)
    return {
        "object": obj.model_dump() if obj else None,
        "high_water": rt._store.version_high_water(oid) if obj else None,
        "history": [e.model_dump() for e in rt.accepted_history(oid)],
        "relationships": sorted(
            (r.relation, r.target_id) for r in rt.canonical_relationships(oid)
        ),
    }


def _assert_no_protected_content(result) -> None:
    assert result.decision != Decision.ALLOW
    assert result.object_version is None
    assert result.actual_version is None
    assert result.previous_state is None and result.new_state is None
    assert result.data == {}
    assert result.events == []
    assert all(not c.message for c in result.validation_checks)


def _tokens(rt, oid) -> list[int]:
    return [e.object_version for e in rt.accepted_history(oid)]


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def po(tmp_path):
    """PO-1 at version 2: created, then charged_to CC-1."""
    rt = _runtime(tmp_path / "po.db")
    rt.execute(CreateObject("CC-1", CC, {"code": "1", "name": "x"}), actor=WRITER)
    rt.execute(CreateObject("PO-1", PO, dict(PO_STATE), state="draft"), actor=WRITER)
    accepted = rt.execute(
        AddRelationship("PO-1", "charged_to", "CC-1"), actor=WRITER, command_context=_ctx(1)
    )
    assert accepted.object_version == 2
    return rt


LC = "LegalContract"
LC_WRITER = Actor(id="lc-writer", type=ActorType.SERVICE, capabilities=["lc.read", "lc.mutate"])
LC_READER = Actor(id="lc-reader", type=ActorType.SERVICE, capabilities=["lc.read"])


@pytest.fixture
def lc(tmp_path):
    """A reducer-backed type whose lifecycle returns to a prior state."""
    d = tmp_path / "types"
    d.mkdir()
    (d / "lc.yaml").write_text(
        yaml.dump(
            {
                "kind": LC,
                "version": "1",
                "schema": {"title": {"type": "string", "required": True}},
                "read_requires": ["lc.read"],
                "actions": {
                    "create": {"requires": ["lc.mutate"]},
                    "activate": {"requires": ["lc.mutate"]},
                    "expire": {"requires": ["lc.mutate"]},
                    "add_relationship": {"requires": ["lc.mutate"]},
                    "remove_relationship": {"requires": ["lc.mutate"]},
                },
                "states": {
                    "draft": {"transitions": {"activate": "active"}},
                    "active": {"transitions": {"expire": "draft"}},
                },
                "initial_state": "draft",
            }
        )
    )
    types = TypeRegistry()
    types.load_directory(d)
    return GovernedObjectRuntime(
        SQLiteStore(tmp_path / "lc.db"), type_registry=types, adapters=[HistoryWorkItemAdapter()],
        **generic_dependencies(extensions=synthetic_extensions(LC)),
    )


def _lc_sequence(rt) -> None:
    """create(1) add(2) activate(3) expire(4) remove(5), with reads between."""
    steps = [
        CreateObject("C-1", LC, {"title": "MSA"}),
        AddRelationship("C-1", "party", "P-1"),
        TransitionState("C-1", "activate"),
        TransitionState("C-1", "expire"),
        RemoveRelationship("C-1", "party", "P-1"),
    ]
    for step in steps:
        assert rt.execute(step, actor=LC_WRITER).decision == Decision.ALLOW
        assert rt.execute(ReadObject("C-1"), actor=LC_READER).decision == Decision.ALLOW


# A plain single-version object (formerly a Container) used to exercise token,
# authorization-order and version-allocation behaviour through a mutating action.
# ``WRITER``, ``READER`` and ``WRITER_NO_READ`` play the former container actors.
C_WRITER, C_READER, C_WRITER_NO_READ = WRITER, READER, WRITER_NO_READ
PLAIN = "CC-9"


@pytest.fixture
def plain(tmp_path):
    """CC-9 at version 1: a plain object with one state-mutating action."""
    rt = _runtime(tmp_path / "plain.db")
    assert rt.execute(CreateObject(PLAIN, CC, {"code": "9", "name": "n"}), actor=WRITER).decision \
        == Decision.ALLOW
    return rt


def _mutate(name: str = "n2", oid: str = PLAIN) -> Command:
    return Command("update", oid, payload={"set": {"name": name}})


@pytest.fixture
def restored(tmp_path):
    rt = _runtime(tmp_path / "restored.db")
    _import(rt, _reorder_bundle(SPARSE))
    return rt


# --- CS-1: token not reused when state recurs ------------------------------------


def test_cs1_lifecycle_state_return_issues_distinct_tokens(tmp_path):
    rt = _runtime(tmp_path / "sup.db")
    rt.execute(CreateObject("SUP-1", SUP, {"name": "s"}, state="active"), actor=WRITER)
    suspended = rt.execute(TransitionState("SUP-1", "suspend"), actor=WRITER, command_context=_ctx(1))
    active = rt.execute(TransitionState("SUP-1", "reactivate"), actor=WRITER, command_context=_ctx(2))
    assert (suspended.object_version, active.object_version) == (2, 3)
    assert _tokens(rt, "SUP-1") == [1, 2, 3]
    stale = rt.execute(Command("update", "SUP-1", payload={"set": {"name": "t"}}), actor=WRITER,
                       command_context=_ctx(1))
    assert stale.outcome_category == "CONFLICT"
    assert lineage_violations(rt._store, "SUP-1") == []


def test_cs1_relationship_add_remove_issues_distinct_tokens(po):
    removed = po.execute(RemoveRelationship("PO-1", "charged_to", "CC-1"), actor=WRITER,
                         command_context=_ctx(2))
    assert removed.object_version == 3
    assert _tokens(po, "PO-1") == [1, 2, 3]
    assert po.execute(AddRelationship("PO-1", "charged_to", "CC-1"), actor=WRITER,
                      command_context=_ctx(1)).outcome_category == "CONFLICT"


# --- CS-2 / CS-3: requester with neither read nor mutation authority -------------


MUTATIONS = {
    "update": lambda: Command("update", "PO-1", payload={"set": {"amount": "1.00"}}),
    "transition-valid": lambda: TransitionState("PO-1", "submit"),
    "transition-invalid": lambda: TransitionState("PO-1", "approve"),
    "rel-add-present": lambda: AddRelationship("PO-1", "charged_to", "CC-1"),
    "rel-add-absent": lambda: AddRelationship("PO-1", "charged_to", "CC-9"),
    "rel-remove-present": lambda: RemoveRelationship("PO-1", "charged_to", "CC-1"),
    "rel-remove-absent": lambda: RemoveRelationship("PO-1", "charged_to", "CC-9"),
}
TOKENS = {"current": 2, "stale": 1, "never-issued": 999, "absent": None}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_cs2_cs3_outsider_mutation_is_token_and_state_independent(po, mutation):
    before = _canonical(po, "PO-1")
    outcomes = set()
    for token in TOKENS.values():
        result = po.execute(MUTATIONS[mutation](), actor=OUTSIDER, command_context=_ctx(token))
        _assert_no_protected_content(result)
        outcomes.add((result.outcome_category, result.deny_reason, result.reason))
        assert _canonical(po, "PO-1") == before
    assert len(outcomes) == 1
    [(category, deny_reason, reason)] = outcomes
    assert category == "AUTHORIZATION_DENIED" and deny_reason == "ACTOR_CAPABILITY_MISSING"
    assert "missing capabilities" in reason


def test_cs3_invalid_and_valid_transition_have_same_outsider_outcome(po):
    valid = po.execute(MUTATIONS["transition-valid"](), actor=OUTSIDER)
    invalid = po.execute(MUTATIONS["transition-invalid"](), actor=OUTSIDER)
    assert valid.outcome_category == invalid.outcome_category == "AUTHORIZATION_DENIED"


@pytest.mark.parametrize("token", sorted(TOKENS))
def test_cs2_outsider_plain_mutation_is_token_independent(plain, token):
    tokens = {"current": 1, "stale": 0, "never-issued": 999, "absent": None}
    before = _canonical(plain, PLAIN)
    result = plain.execute(_mutate(), actor=OUTSIDER, command_context=_ctx(tokens[token]))
    assert result.outcome_category == "AUTHORIZATION_DENIED"
    _assert_no_protected_content(result)
    assert _canonical(plain, PLAIN) == before


def test_cs2_internal_audit_row_is_kept(po):
    before = len(po.history("PO-1"))
    result = po.execute(MUTATIONS["update"](), actor=OUTSIDER, command_context=_ctx(2))
    assert result.events == []
    rows = po.history("PO-1")
    assert len(rows) == before + 1
    assert rows[-1].decision == Decision.DENY and rows[-1].history_position is None


# --- CS-2R: read denial ------------------------------------------------------------


@pytest.mark.parametrize("actor", [OUTSIDER, WRITER_NO_READ], ids=["outsider", "writer-no-read"])
def test_cs2r_read_denial_discloses_nothing(po, actor):
    result = po.execute(ReadObject("PO-1"), actor=actor)
    assert result.outcome_category == "AUTHORIZATION_DENIED"
    _assert_no_protected_content(result)


# --- CS-2S and readers ---------------------------------------------------------


def test_cs2s_reader_stale_token_is_authorization_denied(po, plain):
    for cmd in (MUTATIONS["update"](), MUTATIONS["rel-add-absent"]()):
        result = po.execute(cmd, actor=READER, command_context=_ctx(1))
        assert result.outcome_category == "AUTHORIZATION_DENIED"
    stale = plain.execute(_mutate(), actor=C_READER, command_context=_ctx(0))
    assert stale.outcome_category == "AUTHORIZATION_DENIED"


def test_reader_rejection_is_not_over_sanitized(po):
    result = po.execute(MUTATIONS["update"](), actor=READER, command_context=_ctx(2))
    assert result.outcome_category == "AUTHORIZATION_DENIED"
    assert result.object_version == 2 and result.previous_state == "draft"
    assert result.events


# --- writer without read authority ------------------------------------------------


@pytest.mark.parametrize("token", ["stale", "never-issued"])
def test_writer_no_read_conflict_discloses_no_current_version(po, token):
    before = _canonical(po, "PO-1")
    result = po.execute(MUTATIONS["update"](), actor=WRITER_NO_READ,
                        command_context=_ctx(TOKENS[token]))
    assert result.outcome_category == "CONFLICT"
    assert result.conflict_reason == "VERSION_MISMATCH"
    assert result.expected_version == TOKENS[token]
    _assert_no_protected_content(result)
    assert result.reason == "version conflict"
    assert _canonical(po, "PO-1") == before


def test_writer_no_read_current_token_is_accepted(po):
    assert po.execute(MUTATIONS["update"](), actor=WRITER_NO_READ,
                      command_context=_ctx(2)).object_version == 3


def test_writer_no_read_plain_mutation_conflict_discloses_nothing(plain):
    before = _canonical(plain, PLAIN)
    result = plain.execute(_mutate(), actor=C_WRITER_NO_READ, command_context=_ctx(7))
    assert result.outcome_category == "CONFLICT"
    _assert_no_protected_content(result)
    assert _canonical(plain, PLAIN) == before


# --- D1: duplicate create -----------------------------------------------------------


def test_d1_duplicate_create_is_invalid_and_changes_nothing(po):
    po.execute(MUTATIONS["update"](), actor=WRITER, command_context=_ctx(2))
    before = _canonical(po, "PO-1")
    result = po.execute(CreateObject("PO-1", PO, dict(PO_STATE), state="draft"), actor=WRITER)
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.deny_reason == "IDENTITY_EXISTS"
    assert _canonical(po, "PO-1") == before
    assert _tokens(po, "PO-1") == [1, 2, 3]
    assert po.execute(MUTATIONS["update"](), actor=WRITER,
                      command_context=_ctx(3)).object_version == 4


def test_d1_duplicate_create_checks_authorization_first(po):
    before = _canonical(po, "PO-1")
    result = po.execute(CreateObject("PO-1", PO, dict(PO_STATE), state="draft"), actor=OUTSIDER)
    assert result.outcome_category == "AUTHORIZATION_DENIED"
    _assert_no_protected_content(result)
    assert _canonical(po, "PO-1") == before


def test_d1_duplicate_create_on_restored_object_keeps_tokens(restored):
    before = _canonical(restored, "PO-4821")
    result = restored.execute(CreateObject("PO-4821", PO, dict(PO_STATE), state="draft"),
                              actor=WRITER)
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert _canonical(restored, "PO-4821") == before
    assert _tokens(restored, "PO-4821") == [int(t) for t in SPARSE]


def test_d1_duplicate_create_then_replay(lc):
    _lc_sequence(lc)
    stored = lc._store.get("C-1")
    assert lc.execute(CreateObject("C-1", LC, {"title": "other"}),
                      actor=LC_WRITER).outcome_category == "SEMANTICALLY_INVALID"
    replayed = lc.replay("C-1")
    assert (replayed.version, replayed.state, replayed.data) == (
        stored.version, stored.state, stored.data
    )


# --- D6: idempotency ---------------------------------------------------------------------


def test_d6_idempotency_cache_requires_authorization(po):
    first = po.execute(MUTATIONS["update"](), actor=WRITER,
                       command_context=_ctx(2, idempotency_key="k-1"))
    assert first.object_version == 3
    before = _canonical(po, "PO-1")
    for actor in (OUTSIDER, READER):
        replay = po.execute(MUTATIONS["update"](), actor=actor,
                            command_context=CommandContext(idempotency_key="k-1"))
        assert replay.outcome_category == "AUTHORIZATION_DENIED"
        assert replay.decision == Decision.DENY
    _assert_no_protected_content(
        po.execute(MUTATIONS["update"](), actor=OUTSIDER,
                   command_context=CommandContext(idempotency_key="k-1"))
    )
    retry = po.execute(MUTATIONS["update"](), actor=WRITER,
                       command_context=_ctx(2, idempotency_key="k-1"))
    assert retry.decision == Decision.ALLOW and retry.object_version == 3
    assert _canonical(po, "PO-1") == before


def test_duplicate_object_create_is_refused_and_changes_nothing(plain):
    """Generic identity protection (replaces the container-only registration test)."""
    assert plain._store.identities_present([PLAIN]) == [PLAIN]
    before = _canonical(plain, PLAIN)
    result = plain.execute(CreateObject(PLAIN, CC, {"code": "9", "name": "other"}), actor=WRITER)
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.decision != Decision.ALLOW
    assert _canonical(plain, PLAIN) == before


# --- D3 / D4: single-object accepted mutation ---------------------------------------------


def test_d4_mutation_records_an_accepted_revision(plain):
    assert plain._store.get(PLAIN).version == 1
    result = plain.execute(_mutate(), actor=C_WRITER, command_context=_ctx(1))
    assert result.outcome_category == "ACCEPTED" and result.object_version == 2
    rows = plain.accepted_history(PLAIN)
    assert [(r.action, r.history_position, r.object_version) for r in rows] == [
        ("create", 1, 1),
        ("update", 2, 2),
    ]
    assert rows[1].type_version == plain._store.get(PLAIN).type_version
    assert result.events[0].history_position == 2
    assert lineage_violations(plain._store, PLAIN) == []


def test_d4_mutation_then_replay(plain):
    plain.execute(_mutate(), actor=C_WRITER, command_context=_ctx(1))
    plain.execute(_mutate("n3"), actor=C_WRITER, command_context=_ctx(2))
    stored = plain._store.get(PLAIN)
    replayed = plain.replay(PLAIN)
    assert replayed.version == stored.version == 3
    assert _tokens(plain, PLAIN) == [1, 2, 3]


def test_d4_mutation_allocates_from_high_water(plain):
    conn = plain._store._conn
    conn.execute(f"UPDATE objects SET version_high_water = 40 WHERE id = '{PLAIN}'")
    conn.commit()
    result = plain.execute(_mutate(), actor=C_WRITER, command_context=_ctx(1))
    assert result.object_version == 41


def test_mutation_no_change_outcomes(plain):
    before = _canonical(plain, PLAIN)
    denied = plain.execute(_mutate(), actor=C_READER, command_context=_ctx(1))
    stale = plain.execute(_mutate(), actor=C_WRITER, command_context=_ctx(0))
    invalid = plain.execute(Command("update", PLAIN, payload={"set": {"no_such_field": "x"}}),
                            actor=C_WRITER, command_context=_ctx(1))
    assert denied.outcome_category == "AUTHORIZATION_DENIED"
    assert stale.outcome_category == "CONFLICT"
    assert invalid.outcome_category == "SEMANTICALLY_INVALID"
    assert _canonical(plain, PLAIN) == before


def test_mutation_refused_at_version_ceiling_changes_nothing(plain):
    obj = plain._store.get(PLAIN)
    plain._store.save(obj.model_copy(update={"version": INT64_MAX}))
    before = _canonical(plain, PLAIN)
    result = plain.execute(_mutate(), actor=C_WRITER, command_context=_ctx(INT64_MAX))
    assert result.outcome_category == "REFUSED"
    assert _canonical(plain, PLAIN) == before


# --- D5 / CS-7b: detach ------------------------------------------------------------------


def _bind(rt, oid):
    obj = rt._store.get(oid)
    binding = {"provider_binding": {"provider_type": "generic", "provider_name": "p", "external_id": "x"}}
    rt._store.save(obj.model_copy(update={"metadata": {**obj.metadata, **binding}}))


def test_cs7b_detach_leaves_version_and_history_unchanged(po):
    _bind(po, "PO-1")
    history = [e.model_dump() for e in po.accepted_history("PO-1")]
    detached = po.detach_provider("PO-1")
    assert detached.version == 2
    assert "provider_binding" not in detached.metadata
    assert [e.model_dump() for e in po.accepted_history("PO-1")] == history
    assert po._store.version_high_water("PO-1") == 2
    assert po.execute(MUTATIONS["update"](), actor=WRITER,
                      command_context=_ctx(2)).object_version == 3


def test_cs7b_detach_succeeds_at_version_ceiling(tmp_path):
    rt = _runtime(tmp_path / "max.db")
    _import(rt, _reorder_bundle(SPARSE[:-1] + (str(INT64_MAX),)))
    _bind(rt, "PO-4821")
    history = _tokens(rt, "PO-4821")
    assert rt.detach_provider("PO-4821").version == INT64_MAX
    assert _tokens(rt, "PO-4821") == history
    result = rt.execute(Command("update", "PO-4821", payload={"set": {"amount": "1.00"}}),
                        actor=WRITER, command_context=_ctx(INT64_MAX))
    assert result.outcome_category == "REFUSED"


# --- D7 / D8 / D9: replay -------------------------------------------------------------------


def test_replay_restores_tokens_and_order_not_timestamps(lc):
    _lc_sequence(lc)
    conn = lc._store._conn
    # Non-monotonic tokens in position order, and timestamps reversed.
    rewritten = {1: 40, 2: 77, 3: 12, 4: 97, 5: 3}
    for position, token in rewritten.items():
        conn.execute(
            "UPDATE events SET object_version = ?, timestamp = ? "
            "WHERE object_id = 'C-1' AND history_position = ?",
            (token, f"2030-01-0{6 - position}T00:00:00+00:00", position),
        )
    conn.execute("UPDATE objects SET version = 3, version_high_water = 97 WHERE id = 'C-1'")
    # A read audit row that a timestamp-ordered ALLOW replay would consume last.
    conn.execute(
        "UPDATE events SET object_version = 999, new_state = 'active', "
        "timestamp = '2031-01-01T00:00:00+00:00' WHERE object_id = 'C-1' AND action = 'read'"
    )
    conn.commit()
    assert lineage_violations(lc._store, "C-1") == []
    stored = lc._store.get("C-1")
    replayed = lc.replay("C-1")
    assert (replayed.version, replayed.state, replayed.data) == (3, "draft", stored.data)
    assert lc.canonical_relationships("C-1") == []


def test_replay_state_return_restores_each_token(lc):
    _lc_sequence(lc)
    assert _tokens(lc, "C-1") == [1, 2, 3, 4, 5]
    replayed = lc.replay("C-1")
    assert (replayed.version, replayed.state) == (5, "draft")


def test_replay_of_restored_object_is_exact(tmp_path):
    rt = _runtime(tmp_path / "r.db")
    stamps = [f"2030-01-0{9 - i}T00:00:00+00:00" for i in range(7)]
    _import(rt, _reorder_bundle(SPARSE, stamps=stamps))
    before = _canonical(rt, "PO-4821")
    replayed = rt.replay("PO-4821")
    assert replayed.version == 76
    assert _canonical(rt, "PO-4821") == before


def test_replay_refuses_legacy_history_unless_explicit(lc):
    _lc_sequence(lc)
    conn = lc._store._conn
    conn.execute(
        "UPDATE events SET history_position = NULL WHERE object_id = 'C-1' AND history_position = 5"
    )
    conn.commit()
    before = _canonical(lc, "C-1")
    with pytest.raises(LegacyHistoryRefused):
        lc.replay("C-1")
    assert _canonical(lc, "C-1") == before
    assert lc.replay("C-1", allow_legacy=True) is not None
    assert any("legacy" in v for v in lineage_violations(lc._store, "C-1"))


# --- D10: provider / observation paths ----------------------------------------------------


def test_d10_bootstrap_observation_is_an_accepted_revision(restored):
    _enable_bootstrap_auto_accept(restored)
    result = restored.ingest_observation(_bootstrap())
    assert (result.decision, result.object_version) == ("ACCEPT", 98)
    rows = restored.accepted_history("PO-4821")
    assert rows[-1].object_version == 98 and rows[-1].history_position == len(rows)
    assert lineage_violations(restored._store, "PO-4821") == []
    assert restored.replay("PO-4821").version == 98


def test_d10_review_resolution_is_an_accepted_revision(restored):
    case_id = _review_case(restored)
    result = restored.resolve_review(case_id, resolution="ACCEPT_EXTERNAL", resolved_by="u")
    assert (result.decision, result.object_version) == ("ACCEPT", 98)
    assert lineage_violations(restored._store, "PO-4821") == []


def test_d10_state_observation_is_an_accepted_revision(restored):
    obs = _observation(
        provider_event_id="state",
        state_change=FieldChange(previous_observed="submitted", observed="approved"),
    )
    result = restored.ingest_observation(obs)
    if result.decision == "REQUIRES_REVIEW":
        result = restored.resolve_review(
            result.review_case_id, resolution="ACCEPT_EXTERNAL", resolved_by="u"
        )
    assert (result.decision, result.object_version) == ("ACCEPT", 98)
    assert restored._store.get("PO-4821").state == "approved"
    assert _tokens(restored, "PO-4821")[-1] == 98
    assert lineage_violations(restored._store, "PO-4821") == []


def test_object_creation_records_a_baseline(plain):
    rows = plain.accepted_history(PLAIN)
    assert [(r.action, r.history_position, r.object_version) for r in rows] == [("create", 1, 1)]
    assert lineage_violations(plain._store, PLAIN) == []


# --- CS-5: baseline relationships ----------------------------------------------------------


def test_cs5_baseline_relationships_replay_without_synthesis(tmp_path):
    rt = _runtime(tmp_path / "b.db")
    rt.execute(CreateObject("CC-1", CC, {"code": "1", "name": "x"}), actor=WRITER)
    rels = [{"relation": "charged_to", "target_id": "CC-1"}]
    created = rt.execute(
        CreateObject("PO-1", PO, dict(PO_STATE), state="draft", relationships=rels), actor=WRITER
    )
    assert created.object_version == 1
    before = _canonical(rt, "PO-1")
    assert before["relationships"] == [("charged_to", "CC-1")]
    assert len(before["history"]) == 1
    rt.replay("PO-1")
    assert _canonical(rt, "PO-1") == before


# --- CS-6: historical actor ------------------------------------------------------------------


def test_cs6_historical_actor_confers_no_authority(restored):
    named = {e.actor_id for e in restored.accepted_history("PO-4821")}
    before = _canonical(restored, "PO-4821")
    for actor_id in named:
        result = restored.execute(
            Command("update", "PO-4821", payload={"set": {"amount": "1.00"}}),
            actor=Actor(id=actor_id, type=ActorType.SERVICE, capabilities=[]),
            command_context=_ctx(76),
        )
        assert result.outcome_category == "AUTHORIZATION_DENIED"
        _assert_no_protected_content(result)
    assert _canonical(restored, "PO-4821") == before


# --- CS-7: single lineage ---------------------------------------------------------------------


def test_cs7_single_lineage_across_accepted_rejected_and_no_op(tmp_path):
    db = tmp_path / "lineage.db"
    po = _runtime(db)
    po.execute(CreateObject("CC-1", CC, {"code": "1", "name": "x"}), actor=WRITER)
    po.execute(CreateObject("PO-1", PO, dict(PO_STATE), state="draft"), actor=WRITER)
    po.execute(AddRelationship("PO-1", "charged_to", "CC-1"), actor=WRITER, command_context=_ctx(1))
    po.execute(ReadObject("PO-1"), actor=READER)
    noop = po.execute(AddRelationship("PO-1", "charged_to", "CC-1"), actor=WRITER,
                      command_context=_ctx(2))
    assert noop.outcome_category == "ACCEPTED" and noop.object_version == 2
    for actor in (OUTSIDER, READER, WRITER_NO_READ):
        po.execute(MUTATIONS["update"](), actor=actor, command_context=_ctx(1))
    po.execute(TransitionState("PO-1", "approve"), actor=WRITER, command_context=_ctx(2))
    po.execute(CreateObject("PO-1", PO, dict(PO_STATE)), actor=WRITER)
    assert _tokens(po, "PO-1") == [1, 2]
    assert po._store.get("PO-1").version == 2
    assert lineage_violations(po._store, "PO-1") == []
    po._store.close()
    reopened = _runtime(db)
    assert reopened._store.get("PO-1").version == 2
    assert reopened.execute(MUTATIONS["update"](), actor=WRITER,
                            command_context=_ctx(2)).object_version == 3


# --- CS-9: never-issued token ---------------------------------------------------------------


def test_cs9_never_issued_token_is_never_accepted(po, restored):
    for rt, oid in ((po, "PO-1"), (restored, "PO-4821")):
        before = _canonical(rt, oid)
        for token in (999, 13, 78):
            result = rt.execute(
                Command("update", oid, payload={"set": {"amount": "1.00"}}), actor=WRITER,
                command_context=_ctx(token),
            )
            assert result.outcome_category == "CONFLICT"
        assert _canonical(rt, oid) == before


# --- restoration conditionality ---------------------------------------------------------------


def test_restored_tokens_are_exact_and_pre_restore_tokens_conflict(restored):
    assert _tokens(restored, "PO-4821") == [int(t) for t in SPARSE]
    assert restored._store.get("PO-4821").version == 76
    for earlier in SPARSE[:-1]:
        assert restored.execute(
            Command("update", "PO-4821", payload={"set": {"amount": "1.00"}}), actor=WRITER,
            command_context=_ctx(int(earlier)),
        ).outcome_category == "CONFLICT"
    assert restored.execute(
        Command("update", "PO-4821", payload={"set": {"amount": "1.00"}}), actor=WRITER,
        command_context=_ctx(76),
    ).object_version == 98
    assert lineage_violations(restored._store, "PO-4821") == []


# --- D11: the runtime owns transition Object Versions ----------------------------------------


class ProposingAdapter(HistoryWorkItemAdapter):
    """Returns, for update_title, whatever each test configures."""

    def __init__(self):
        self.propose = lambda obj, command: obj

    def adapter_execute(self, obj, command, context):
        if command.action != "update_title":
            return None
        return AdapterExecuteResult(success=True, object=self.propose(obj, command))


@pytest.fixture
def wi(tmp_path):
    d = tmp_path / "types"
    d.mkdir()
    (d / "wi.yaml").write_text(
        yaml.dump(
            {
                "kind": "HistoryWorkItem",
                "version": "1",
                "schema": {"title": {"type": "string", "required": True}},
                "read_requires": ["lc.read"],
                "actions": {
                    "create": {"requires": ["lc.mutate"]},
                    "update_title": {"requires": ["lc.mutate"]},
                },
                "states": {"draft": {"transitions": {"update_title": "draft"}}},
                "initial_state": "draft",
            }
        )
    )
    types = TypeRegistry()
    types.load_directory(d)
    adapter = ProposingAdapter()
    rt = GovernedObjectRuntime(SQLiteStore(tmp_path / "wi.db"), type_registry=types, adapters=[adapter],
                               **generic_dependencies())
    assert rt.execute(CreateObject("W-1", "HistoryWorkItem", {"title": "t1"}), actor=LC_WRITER).decision == Decision.ALLOW
    assert rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER).object_version == 2
    return rt, adapter


def _retitle(obj, title, version):
    return obj.model_copy(update={"data": {**obj.data, "title": title}, "version": version})


def _assert_one_fresh_revision(rt, before_tokens, result) -> None:
    assert result.decision == Decision.ALLOW
    tokens = _tokens(rt, "W-1")
    assert tokens[:-1] == before_tokens
    assert tokens[-1] == result.object_version == rt._store.get("W-1").version
    assert result.object_version not in before_tokens
    assert [e.history_position for e in rt.accepted_history("W-1")] == list(range(1, len(tokens) + 1))
    assert lineage_violations(rt._store, "W-1") == []


def test_d11_adapter_unchanged_version_cannot_reuse_token(wi):
    rt, adapter = wi
    adapter.propose = lambda obj, _: _retitle(obj, "t2", obj.version)
    before = _tokens(rt, "W-1")
    result = rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER, command_context=_ctx(2))
    _assert_one_fresh_revision(rt, before, result)
    assert rt._store.get("W-1").data["title"] == "t2"


@pytest.mark.parametrize("proposed", [1, 2, 3, 500, INT64_MAX])
def test_d11_adapter_proposed_version_is_discarded(wi, proposed):
    rt, adapter = wi
    adapter.propose = lambda obj, _: _retitle(obj, "t2", proposed)
    before = _tokens(rt, "W-1")
    result = rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER)
    _assert_one_fresh_revision(rt, before, result)
    assert result.object_version == 3


def test_d11_identical_values_are_still_a_distinct_revision(wi):
    """Same canonical values != same canonical revision: an accepted
    transition is a revision whatever its resulting values."""
    rt, adapter = wi
    adapter.propose = lambda obj, _: obj
    prior = rt._store.get("W-1")
    before = _tokens(rt, "W-1")
    result = rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER, command_context=_ctx(2))
    _assert_one_fresh_revision(rt, before, result)
    stored = rt._store.get("W-1")
    assert (stored.state, stored.data) == (prior.state, prior.data)
    replayed = rt.replay("W-1")
    assert (replayed.version, replayed.state, replayed.data) == (stored.version, prior.state, prior.data)


def test_d11_success_without_object_is_refused_and_changes_nothing(wi):
    rt, adapter = wi
    adapter.propose = lambda obj, _: None
    before = _canonical(rt, "W-1")
    result = rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER, command_context=_ctx(2))
    assert result.decision != Decision.ALLOW
    assert result.outcome_category == "REFUSED"
    assert result.deny_reason == "ADAPTER_RESULT_INVALID"
    assert _canonical(rt, "W-1") == before
    adapter.propose = lambda obj, _: obj
    assert rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER).object_version == 3


def test_d11_authorized_stale_version_is_not_accepted(wi):
    rt, adapter = wi
    adapter.propose = lambda obj, _: _retitle(obj, "t2", obj.version)
    before = _canonical(rt, "W-1")
    result = rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER, command_context=_ctx(1))
    assert result.outcome_category == "CONFLICT"
    assert _canonical(rt, "W-1") == before


def test_d11_replay_reproduces_state_and_token_after_transitions(wi):
    rt, adapter = wi
    for title, proposed in [("t2", 2), ("t2", 1), ("t3", 99)]:
        adapter.propose = lambda obj, _, t=title, v=proposed: _retitle(obj, t, v)
        assert rt.execute(TransitionState("W-1", "update_title"), actor=LC_WRITER).decision == Decision.ALLOW
    assert _tokens(rt, "W-1") == [1, 2, 3, 4, 5]
    stored = rt._store.get("W-1")
    replayed = rt.replay("W-1")
    assert (replayed.version, replayed.state, replayed.data) == (5, stored.state, stored.data)
    assert lineage_violations(rt._store, "W-1") == []


def test_d11_lineage_check_detects_adjacent_reused_token(wi):
    rt, _ = wi
    conn = rt._store._conn
    conn.execute("UPDATE events SET object_version = 1 WHERE object_id = 'W-1' AND history_position = 2")
    conn.execute("UPDATE objects SET version = 1 WHERE id = 'W-1'")
    conn.commit()
    assert "Object Version 1 names 2 accepted revisions" in lineage_violations(rt._store, "W-1")
