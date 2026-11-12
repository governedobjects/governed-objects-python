# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Draft 2.2 OP-OUTCOME-001: every completed operation result carries its
outcome category, and a non-accepted result changes nothing canonical."""

from __future__ import annotations
from tests.generic_bootstrap import generic_dependencies

from pathlib import Path

import pytest

from governed_objects.adapters.context import AdapterExecuteResult, AdapterFailure
from governed_objects.domain.command import Command, CreateObject, ReadObject, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType, Decision
from tests.fixtures.interchange import GRANT_CAPABILITIES, STANDARD_REQUIREMENTS
from governed_objects.interchange.types import PortableTypeAdapter, build_type_definition
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.errors import ProviderUnavailable
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.registry import TypeRegistry

RECORD = "example.outcome/Record"
TYPE = {
    "type_identity": RECORD,
    "type_version": "1",
    "state_fields": {"title": "string", "status": "lifecycle"},
    "lifecycle": {"states": ["draft", "active"], "transitions": [["draft", "active"]]},
    "relationship_types": [],
    "actions": {
        "update": {"kind": "state"},
        "activate": {"kind": "lifecycle", "from": ["draft"], "to": "active"},
    },
}


def _actor(name: str, grants: list[str]) -> Actor:
    caps = [c for g in grants for c in GRANT_CAPABILITIES[g]]
    return Actor(id=name, type=ActorType.SERVICE, capabilities=caps)


WRITER = _actor("writer", ["read", "create", "state and lifecycle actions"])
WRITER_NO_READ = _actor("writer-no-read", ["state and lifecycle actions"])
OUTSIDER = _actor("outsider", [])


class ScriptedAdapter(PortableTypeAdapter):
    """PortableTypeAdapter whose load or execute can be made to fail."""

    def __init__(self, types: TypeRegistry) -> None:
        super().__init__(types)
        self.load_error: Exception | None = None
        self.execute_result: AdapterExecuteResult | None = None
        self.loads = 0

    def load(self, obj, context):
        self.loads += 1
        if self.load_error is not None:
            raise self.load_error
        return super().load(obj, context)

    def adapter_execute(self, obj, command, context):
        if self.execute_result is not None:
            return self.execute_result
        return super().adapter_execute(obj, command, context)


@pytest.fixture
def env(tmp_path: Path):
    registry = TypeRegistry()
    registry.register(build_type_definition(TYPE, STANDARD_REQUIREMENTS))
    adapter = ScriptedAdapter(registry)
    rt = GovernedObjectRuntime(SQLiteStore(tmp_path / "rt.db"), type_registry=registry, adapters=[adapter],
                               **generic_dependencies())
    created = rt.execute(CreateObject("R-1", RECORD, {"title": "a"}, state="draft"), actor=WRITER)
    assert created.outcome_category == "ACCEPTED"
    return rt, adapter


def _canonical(rt: GovernedObjectRuntime, object_id: str = "R-1"):
    obj = rt.get_object(object_id, raw=True)
    return (
        obj.state,
        dict(obj.data),
        obj.version,
        rt._store.version_high_water(object_id),
        [(e.action, e.object_version) for e in rt.accepted_history(object_id)],
        rt.canonical_relationships(object_id),
    )


def _update(changes: dict) -> Command:
    return Command("update", "R-1", payload={"set": changes})


def _run(rt, command, actor=WRITER, expected: int | None = 1, **ctx):
    context = CommandContext(expected_version=expected, **ctx)
    return rt.execute(command, actor=actor, command_context=context)


@pytest.mark.parametrize(
    "changes",
    [{"undeclared": "x"}, {"title": 7}, {}],
    ids=["unknown-field", "non-string", "empty-set"],
)
def test_o1_invalid_update_is_semantically_invalid_and_changes_nothing(env, changes):
    rt, _ = env
    before = _canonical(rt)
    result = _run(rt, _update(changes))
    assert result.decision == Decision.DENY
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.reason == "STATE_INVALID"
    assert result.deny_reason == "STATE_INVALID"
    assert _canonical(rt) == before
    audit = rt.history("R-1")[-1]
    assert audit.event_type == "object.update.denied"
    assert audit.details["adapter_error"] == "STATE_INVALID"
    assert audit.details["message"]


def test_o1_invalid_update_does_not_consume_a_version(env):
    rt, _ = env
    _run(rt, _update({"undeclared": "x"}))
    accepted = _run(rt, _update({"title": "b"}))
    assert accepted.outcome_category == "ACCEPTED"
    assert accepted.object_version == 2


def test_o2_invalid_transition_is_semantically_invalid_and_changes_nothing(env):
    rt, _ = env
    assert _run(rt, TransitionState("R-1", "activate")).outcome_category == "ACCEPTED"
    before = _canonical(rt)
    result = _run(rt, TransitionState("R-1", "activate"), expected=2)
    assert result.decision == Decision.DENY
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.deny_reason == "LIFECYCLE_INVALID"
    assert _canonical(rt) == before


def test_o3_portable_type_adapter_classifies_its_own_rejection(tmp_path):
    registry = TypeRegistry()
    registry.register(build_type_definition(TYPE, STANDARD_REQUIREMENTS))
    rt = GovernedObjectRuntime(
        SQLiteStore(tmp_path / "plain.db"), type_registry=registry, adapters=[PortableTypeAdapter(registry)],
        **generic_dependencies(),
    )
    rt.execute(CreateObject("R-1", RECORD, {"title": "a"}, state="draft"), actor=WRITER)
    before = _canonical(rt)
    result = rt.execute(_update({"undeclared": "x"}), actor=WRITER)
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert _canonical(rt) == before


def test_o4_invalid_update_by_writer_without_read_discloses_nothing(env):
    rt, _ = env
    before = _canonical(rt)
    result = _run(rt, _update({"undeclared": "x"}), actor=WRITER_NO_READ)
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.object_version is None and result.actual_version is None
    assert result.previous_state is None and result.new_state is None
    assert result.data == {} and result.events == []
    assert all(c.message == "" for c in result.validation_checks)
    assert result.reason == "command rejected"
    assert _canonical(rt) == before


def test_o4_invalid_update_by_outsider_is_authorization_denied(env):
    rt, _ = env
    valid = _run(rt, _update({"title": "b"}), actor=OUTSIDER)
    invalid = _run(rt, _update({"undeclared": "x"}), actor=OUTSIDER)
    for result in (valid, invalid):
        assert result.outcome_category == "AUTHORIZATION_DENIED"
        assert result.object_version is None and result.data == {} and result.events == []
    assert (valid.reason, valid.deny_reason) == (invalid.reason, invalid.deny_reason)


def _bind_to_external_provider(rt: GovernedObjectRuntime) -> None:
    obj = rt.get_object("R-1", raw=True)
    rt._store.save(obj.model_copy(update={"metadata": {"provider_binding": {"provider_name": "ext"}}}))


def test_r1a_explicit_invalid_failure_is_semantically_invalid(env):
    rt, adapter = env
    adapter.execute_result = AdapterExecuteResult(
        success=False,
        error_code="STATE_INVALID",
        error_message="title must not be empty",
        failure=AdapterFailure.INVALID,
    )
    before = _canonical(rt)
    result = _run(rt, _update({"title": ""}))
    assert result.decision == Decision.DENY
    assert result.outcome_category == "SEMANTICALLY_INVALID"
    assert (result.reason, result.deny_reason) == ("STATE_INVALID", "STATE_INVALID")
    assert rt.history("R-1")[-1].event_type == "object.update.denied"
    assert _canonical(rt) == before


def test_r1b_explicit_provider_failure_is_refused(env):
    rt, adapter = env
    adapter.execute_result = AdapterExecuteResult(
        success=False,
        error_code=ProviderUnavailable.code,
        error_message="down",
        failure=AdapterFailure.PROVIDER,
    )
    before = _canonical(rt)
    result = _run(rt, _update({"title": "b"}))
    assert result.decision == Decision.DENY
    assert result.outcome_category == "REFUSED"
    assert (result.reason, result.deny_reason) == ("PROVIDER_UNAVAILABLE", "PROVIDER_FAILED")
    assert rt.history("R-1")[-1].event_type == "object.update.provider_failed"
    assert _canonical(rt) == before


@pytest.mark.parametrize("bound", [False, True], ids=["r1c-local", "r1d-provider-bound"])
def test_r1cd_unclassified_failure_is_refused_regardless_of_binding(env, bound):
    rt, adapter = env
    if bound:
        _bind_to_external_provider(rt)
    adapter.execute_result = AdapterExecuteResult(
        success=False, error_code="SOMETHING", error_message="title must not be empty"
    )
    before = _canonical(rt)
    result = _run(rt, _update({"title": ""}))
    assert result.decision == Decision.DENY
    assert result.outcome_category == "REFUSED"
    assert result.deny_reason == "ADAPTER_RESULT_INVALID"
    audit = rt.history("R-1")[-1]
    assert audit.event_type == "object.update.denied"
    assert audit.details["adapter_error"] == "SOMETHING"
    assert audit.details["adapter_failure"] is None
    assert _canonical(rt) == before


def test_successful_result_carrying_a_failure_class_is_refused(env):
    rt, adapter = env
    obj = rt.get_object("R-1", raw=True)
    adapter.execute_result = AdapterExecuteResult(
        success=True,
        object=obj.model_copy(update={"data": {"title": "b"}}),
        failure=AdapterFailure.INVALID,
    )
    before = _canonical(rt)
    result = _run(rt, _update({"title": "b"}))
    assert result.outcome_category == "REFUSED"
    assert result.deny_reason == "ADAPTER_RESULT_INVALID"
    assert _canonical(rt) == before


def test_unclassified_failure_discloses_nothing_to_non_readers(env):
    rt, adapter = env
    adapter.execute_result = AdapterExecuteResult(
        success=False, error_code="SOMETHING", error_message="state is draft, title a"
    )
    before = _canonical(rt)
    wnr = _run(rt, _update({"title": ""}), actor=WRITER_NO_READ)
    assert wnr.outcome_category == "REFUSED"
    assert wnr.reason == "command refused"
    outsider = _run(rt, _update({"title": ""}), actor=OUTSIDER)
    assert outsider.outcome_category == "AUTHORIZATION_DENIED"
    for result in (wnr, outsider):
        assert result.object_version is None and result.actual_version is None
        assert result.previous_state is None and result.new_state is None
        assert result.data == {} and result.events == []
        assert all(c.message == "" for c in result.validation_checks)
        assert "draft" not in result.model_dump_json()
    assert _canonical(rt) == before


def test_o5_provider_failure_while_loading_is_refused(env):
    rt, adapter = env
    adapter.load_error = ProviderUnavailable("provider offline")
    before = _canonical(rt)
    transition = _run(rt, _update({"title": "b"}))
    read = rt.execute(ReadObject("R-1"), actor=WRITER)
    for result in (transition, read):
        assert result.outcome_category == "REFUSED"
        assert result.reason == "PROVIDER_UNAVAILABLE"
    assert _canonical(rt) == before


@pytest.mark.parametrize("provider_down", [False, True], ids=["provider-up", "provider-down"])
def test_read_is_authorized_before_any_provider_interaction(env, provider_down):
    rt, adapter = env
    adapter.load_error = ProviderUnavailable("provider offline") if provider_down else None
    adapter.loads = 0
    for actor in (OUTSIDER, WRITER_NO_READ):
        result = rt.execute(ReadObject("R-1"), actor=actor)
        assert result.outcome_category == "AUTHORIZATION_DENIED"
        assert result.object_version is None and result.data == {} and result.events == []
    assert adapter.loads == 0


def test_o5_provider_failure_to_writer_without_read_discloses_nothing(env):
    rt, adapter = env
    adapter.load_error = ProviderUnavailable("provider offline")
    result = _run(rt, _update({"title": "b"}), actor=WRITER_NO_READ)
    assert result.outcome_category == "REFUSED"
    assert result.reason == "command refused"
    assert result.events == [] and result.object_version is None


def test_completed_reads_and_transitions_carry_a_category(env):
    rt, _ = env
    assert rt.execute(ReadObject("R-1"), actor=WRITER).outcome_category == "ACCEPTED"
    assert rt.execute(ReadObject("R-404"), actor=WRITER).outcome_category == "SEMANTICALLY_INVALID"
    missing = rt.execute(Command("update", "R-404", payload={"set": {"title": "b"}}), actor=WRITER)
    assert missing.outcome_category == "SEMANTICALLY_INVALID"
    assert _run(rt, TransitionState("R-1", "activate")).outcome_category == "ACCEPTED"


def test_unknown_mandate_and_application_are_authorization_denied(env):
    rt, _ = env
    for ctx in ({"mandate_id": "nope"}, {"application_id": "nope"}):
        result = _run(rt, _update({"title": "b"}), **ctx)
        assert result.outcome_category == "AUTHORIZATION_DENIED"


def test_idempotent_replay_keeps_its_category_and_rejections_are_not_cached(env):
    rt, _ = env
    rejected = _run(rt, _update({"undeclared": "x"}), idempotency_key="k1")
    assert rejected.outcome_category == "SEMANTICALLY_INVALID"
    accepted = _run(rt, _update({"title": "b"}), idempotency_key="k1")
    assert accepted.outcome_category == "ACCEPTED" and accepted.object_version == 2
    replay = _run(rt, _update({"title": "b"}), idempotency_key="k1")
    assert replay.outcome_category == "ACCEPTED" and replay.object_version == 2
    other = _run(rt, _update({"title": "b"}), actor=OUTSIDER, idempotency_key="k1")
    assert other.outcome_category == "AUTHORIZATION_DENIED" and other.object_version is None
